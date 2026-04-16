#!/usr/bin/env python3
"""
Ingest strategy-kb documents into the cortex knowledge layer (cortex-vectors.db).

Sources ingested:
  - ~/Projects/strategy-kb/knowledge/*.md
  - ~/Projects/strategy-kb/transcripts/**/*.txt
  - ~/Projects/MCP-Servers/brokerbridge/research/strategy_ideas/*.md
    (excludes .claude/worktrees/ duplicates)

Each file is stored in the `knowledge` collection as a knowledge entry keyed by
a stable ID derived from the file path relative to its source root.  Idempotent:
re-running skips files whose content hash has not changed (ON CONFLICT + dedup
check inside UnifiedVectorStore._upsert).

Usage:
    python3 scripts/ingest_strategy_kb.py --dry-run   # report what would be ingested
    python3 scripts/ingest_strategy_kb.py              # ingest for real

Hard exclusions:
  - venv/, .venv/, node_modules/, .git/
  - .env*, *.key, *.pem, secrets/, credentials/
  - Binary files (.mp3, .jpg, .png, .pdf, etc.)
  - The .claude/worktrees/ tree inside brokerbridge (duplicate worktrees)
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Ensure src/ is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent / "src"
sys.path.insert(0, str(_SRC_DIR))

from unified_vector_store import get_vector_store, DB_PATH  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest-strategy-kb")

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

HOME = Path.home()

SOURCES = [
    {
        "root": HOME / "Projects" / "strategy-kb" / "knowledge",
        "glob": "*.md",
        "source_label": "strategy-kb/knowledge",
        "strategy_tag": None,  # derive from filename
        "recurse": False,
    },
    {
        "root": HOME / "Projects" / "strategy-kb" / "transcripts",
        "glob": "**/*.txt",
        "source_label": "strategy-kb/transcripts",
        "strategy_tag": None,  # derive from parent dir
        "recurse": True,
    },
    {
        "root": HOME / "Projects" / "MCP-Servers" / "brokerbridge" / "research" / "strategy_ideas",
        "glob": "*.md",
        "source_label": "brokerbridge/strategy_ideas",
        "strategy_tag": None,
        "recurse": False,
        "exclude_parts": [".claude", "worktrees"],
    },
]

# File extensions to ingest (text only)
TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".text", ".json", ".yaml", ".yml"}

# Paths/names to always skip
SKIP_NAMES = {".env", "secrets", "credentials", ".git", "venv", ".venv", "node_modules"}
SKIP_EXTENSIONS = {".key", ".pem", ".cert", ".mp3", ".jpg", ".jpeg", ".png", ".pdf", ".gif", ".svg"}

# Max file size: 500KB
MAX_FILE_BYTES = 500 * 1024


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _should_skip_path(path: Path) -> bool:
    """Return True if any path component is in the skip list."""
    for part in path.parts:
        if part in SKIP_NAMES:
            return True
        if part.startswith(".env"):
            return True
    return False


def _stable_doc_id(source_label: str, rel_path: str) -> str:
    """Generate a stable, deterministic doc ID for a file."""
    slug = rel_path.replace("/", "--").replace("\\", "--").replace(" ", "_").replace(".", "-")
    return f"strategy-kb--{source_label.replace('/', '-')}--{slug}"


def _strategy_tag_from_path(path: Path, source_def: dict) -> str:
    """Extract a strategy tag from the file path."""
    if source_def.get("strategy_tag"):
        return source_def["strategy_tag"]
    # Use parent directory name or file stem
    parent = path.parent.name
    stem = path.stem
    if parent not in {"knowledge", "transcripts", "strategy_ideas", "neelsalami"}:
        return parent.replace(" ", "_").lower()
    return stem.replace(" ", "_").lower()[:40]


def discover_files(source_def: dict) -> Iterator[tuple[Path, dict]]:
    """Yield (file_path, metadata_dict) for each file matching the source definition."""
    root: Path = source_def["root"]
    if not root.exists():
        logger.warning(f"Source root not found, skipping: {root}")
        return

    glob_pattern = source_def["glob"]
    exclude_parts = set(source_def.get("exclude_parts", []))

    for path in sorted(root.glob(glob_pattern)):
        if not path.is_file():
            continue

        # Check exclusion parts
        if exclude_parts:
            if any(part in exclude_parts for part in path.parts):
                continue

        # Skip hidden/sensitive paths
        if _should_skip_path(path):
            continue

        # Skip wrong extensions
        if path.suffix.lower() in SKIP_EXTENSIONS:
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue

        # Skip oversized files
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            logger.warning(f"Skipping oversized file ({size} bytes): {path}")
            continue
        if size == 0:
            logger.warning(f"Skipping empty file: {path}")
            continue

        rel_path = str(path.relative_to(root))
        doc_id = _stable_doc_id(source_def["source_label"], rel_path)
        strategy_tag = _strategy_tag_from_path(path, source_def)

        metadata = {
            "source": "strategy-kb",
            "source_label": source_def["source_label"],
            "tags": ["strategy-kb", strategy_tag],
            "file_path": str(path),
            "rel_path": rel_path,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

        yield path, doc_id, metadata


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def ingest(dry_run: bool = False) -> dict:
    """Run ingestion. Returns summary stats."""
    store = get_vector_store(DB_PATH)

    rows_before = _count_knowledge_rows(store)

    total_files = 0
    total_chunks_estimate = 0
    ingested = 0
    skipped = 0
    errors = 0

    files_to_ingest: list[tuple[Path, str, dict]] = []

    # Collect all files first (for dry-run count)
    for source_def in SOURCES:
        for path, doc_id, metadata in discover_files(source_def):
            files_to_ingest.append((path, doc_id, metadata))

    total_files = len(files_to_ingest)

    if dry_run:
        print(f"\n=== DRY RUN: would ingest {total_files} files ===")
        for path, doc_id, metadata in files_to_ingest:
            size = path.stat().st_size
            text = path.read_text(encoding="utf-8", errors="replace")
            # Estimate chunks using the same logic as UnifiedVectorStore
            from unified_vector_store import DEFAULT_CHUNK_MAX_CHARS
            n_chunks = max(1, (len(text) + DEFAULT_CHUNK_MAX_CHARS - 1) // DEFAULT_CHUNK_MAX_CHARS)
            total_chunks_estimate += n_chunks
            print(f"  [FILE] {path.name}")
            print(f"         id={doc_id}")
            print(f"         size={size} bytes, ~{n_chunks} chunk(s)")
            print(f"         tags={metadata['tags']}")
        print(f"\nTotal: {total_files} file(s), ~{total_chunks_estimate} chunk(s)\n")
        return {"files": total_files, "chunks_estimate": total_chunks_estimate, "dry_run": True}

    # Real ingest
    for path, doc_id, metadata in files_to_ingest:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            store.add_knowledge(doc_id, text, metadata)
            logger.info(f"[OK] Ingested: {path.name} → {doc_id}")
            ingested += 1
        except Exception as exc:
            logger.error(f"[ERROR] Failed to ingest {path}: {exc}")
            errors += 1

    rows_after = _count_knowledge_rows(store)
    delta = rows_after - rows_before

    summary = {
        "files_attempted": total_files,
        "ingested": ingested,
        "errors": errors,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "row_delta": delta,
        "dry_run": False,
    }

    print(f"\n=== INGEST COMPLETE ===")
    print(f"  Files attempted : {total_files}")
    print(f"  Ingested        : {ingested}")
    print(f"  Errors          : {errors}")
    print(f"  Rows before     : {rows_before}")
    print(f"  Rows after      : {rows_after}")
    print(f"  Row delta       : {delta}")
    print()

    return summary


def _count_knowledge_rows(store) -> int:
    """Count rows in the knowledge collection."""
    try:
        row = store._safe_execute(
            "SELECT COUNT(*) as cnt FROM documents WHERE collection = 'knowledge'"
        ).fetchone()
        return row["cnt"] if row else 0
    except Exception as exc:
        logger.warning(f"Could not count rows: {exc}")
        return -1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest strategy-kb documents into cortex knowledge layer"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be ingested without writing to DB",
    )
    args = parser.parse_args()

    if not args.dry_run:
        bak_path = DB_PATH.parent / (DB_PATH.name + ".bak-pass8")
        if not bak_path.exists():
            import shutil
            logger.info(f"Creating backup: {bak_path}")
            shutil.copy2(str(DB_PATH), str(bak_path))
            logger.info("Backup created.")
        else:
            logger.info(f"Backup already exists: {bak_path}")

    result = ingest(dry_run=args.dry_run)

    if not args.dry_run:
        # Write ingestion record
        record_path = DB_PATH.parent / "strategy-kb-ingestion-record.json"
        record = {
            "pass": "pass8",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "db_path": str(DB_PATH),
            **result,
        }
        record_path.write_text(json.dumps(record, indent=2))
        logger.info(f"Ingestion record written: {record_path}")


if __name__ == "__main__":
    main()
