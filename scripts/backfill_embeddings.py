#!/usr/bin/env python3
"""Backfill embeddings for documents missing them in cortex-vectors.db.

Finds all documents with has_embedding=0 and generates embeddings using the
configured provider (default: local sentence-transformers/all-MiniLM-L6-v2).

Uses the UnifiedVectorStore's batch embedding for efficiency — multiple texts
are encoded in a single forward pass on the GPU/CPU.

Safe to re-run: only processes documents where has_embedding=0, commits after
each batch so progress survives interruptions.

Usage:
    python backfill_embeddings.py [--batch-size 50] [--db-path PATH] [--max-docs N] [--status]

Examples:
    # Auto-detect DB and backfill all missing embeddings
    python backfill_embeddings.py

    # Use a specific DB path
    python backfill_embeddings.py --db-path ~/clawd/data/cortex-vectors.db

    # Process in smaller batches (less memory)
    python backfill_embeddings.py --batch-size 25

    # Only process first 500 documents
    python backfill_embeddings.py --max-docs 500

    # Check status without processing
    python backfill_embeddings.py --status
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill-embeddings")


# ---------------------------------------------------------------------------
# Auto-detect DB path
# ---------------------------------------------------------------------------

# Known locations where cortex-vectors.db may live
_CANDIDATE_PATHS = [
    Path.home() / "clawd" / "data" / "cortex-vectors.db",
    Path.home() / ".cortex" / "data" / "cortex-vectors.db",
]


def _detect_db_path() -> Path:
    """Auto-detect the best cortex-vectors.db path.

    Priority:
      1. CORTEX_DATA_DIR env var (if set, use <dir>/cortex-vectors.db)
      2. Largest existing file among known candidate paths (more data = primary)
      3. Default ~/.cortex/data/cortex-vectors.db (even if it doesn't exist yet)
    """
    # Check env var first
    env_dir = os.environ.get("CORTEX_DATA_DIR", "").strip()
    if env_dir:
        env_path = Path(env_dir).expanduser() / "cortex-vectors.db"
        if env_path.exists():
            return env_path
        log.warning(
            f"CORTEX_DATA_DIR={env_dir} set but {env_path} does not exist; "
            f"falling back to auto-detection."
        )

    # Pick the largest existing candidate (more documents = primary DB)
    best: Path | None = None
    best_size = -1
    for candidate in _CANDIDATE_PATHS:
        if candidate.exists():
            size = candidate.stat().st_size
            log.info(f"  Found DB: {candidate} ({size / 1024 / 1024:.1f} MB)")
            if size > best_size:
                best = candidate
                best_size = size

    if best is not None:
        return best

    # Default fallback
    return _CANDIDATE_PATHS[-1]


# ---------------------------------------------------------------------------
# Import UnifiedVectorStore from sibling src/ directory
# ---------------------------------------------------------------------------

def _setup_import_path():
    """Add the llm-cortex/src directory to sys.path so we can import the store."""
    script_dir = Path(__file__).resolve().parent
    src_dir = script_dir.parent / "src"
    if src_dir.is_dir() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


def _load_store(db_path: Path):
    """Import and instantiate UnifiedVectorStore.

    Handles missing dependencies gracefully with helpful error messages.
    """
    _setup_import_path()

    try:
        from unified_vector_store import UnifiedVectorStore
    except ImportError as e:
        log.error(
            f"Failed to import UnifiedVectorStore: {e}\n"
            f"Make sure you're running from the llm-cortex project directory "
            f"and that the src/ directory contains unified_vector_store.py."
        )
        sys.exit(1)

    # Check for sentence-transformers before creating the store (avoids
    # confusing errors deep in the embedding pipeline)
    embedding_provider = os.environ.get("CORTEX_EMBEDDING_PROVIDER", "local")
    if embedding_provider == "local":
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            log.error(
                "sentence-transformers is not installed but is required for "
                "local embeddings (the default provider).\n\n"
                "Install it with:\n"
                "    pip install sentence-transformers\n\n"
                "Or switch to OpenAI embeddings:\n"
                "    export CORTEX_EMBEDDING_PROVIDER=openai\n"
                "    export OPENAI_API_KEY=sk-..."
            )
            sys.exit(1)

    store = UnifiedVectorStore(db_path)
    return store


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

def print_status(store) -> dict:
    """Print current embedding status and return the status dict."""
    status = store.get_backfill_status()

    if "error" in status:
        log.error(f"Failed to get status: {status['error']}")
        return status

    total = status.get("total_docs", 0)
    unembedded = status.get("unembedded_docs", 0)
    embedded = status.get("embedded_docs", 0)
    pct = status.get("embed_pct", 0)

    print(f"\n{'='*60}")
    print(f"  Cortex Vector Store — Embedding Status")
    print(f"{'='*60}")
    print(f"  DB Path:        {store.db_path}")
    print(f"  Vec available:  {store.vec_available}")
    print(f"  Total docs:     {total:,}")
    print(f"  Embedded:       {embedded:,} ({pct}%)")
    print(f"  Unembedded:     {unembedded:,}")
    print(f"{'='*60}")

    last = status.get("last_backfill")
    if last:
        print(f"\n  Last backfill run:")
        print(f"    Processed:    {last.get('processed', '?')}")
        print(f"    Failed:       {last.get('failed', '?')}")
        print(f"    Elapsed:      {last.get('elapsed_seconds', '?')}s")
        print(f"    Rate:         {last.get('rate_docs_per_sec', '?')} docs/s")

    print()
    return status


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------

def run_backfill(store, batch_size: int, max_docs: int | None) -> dict:
    """Run the embedding backfill using the store's built-in method.

    If the batch method fails for a particular batch, falls back to
    individual embed_document() calls for each document in that batch.
    """
    # Pre-flight: check vec_available
    if not store.vec_available:
        log.error(
            "Vector search is not available. This means sqlite_vec could not "
            "be loaded. Possible causes:\n"
            "  - sqlite_vec is not installed: pip install sqlite-vec\n"
            "  - Dimension mismatch between existing embeddings and current "
            "provider configuration\n"
            "  - SQLite extension loading is disabled\n\n"
            "Cannot generate embeddings without sqlite_vec."
        )
        return {"processed": 0, "failed": 0, "error": "vec_not_available"}

    # Print pre-backfill status
    status = print_status(store)
    unembedded = status.get("unembedded_docs", 0)

    if unembedded == 0:
        log.info("All documents already have embeddings. Nothing to do.")
        return {"processed": 0, "failed": 0, "skipped": 0, "elapsed_seconds": 0}

    effective_count = min(unembedded, max_docs) if max_docs else unembedded
    log.info(
        f"Starting backfill: {effective_count:,} documents "
        f"(batch_size={batch_size})"
    )

    # Progress callback
    last_report_time = [time.monotonic()]

    def progress_callback(processed: int, total: int):
        now = time.monotonic()
        # Report at most every 10 seconds to avoid log spam
        if now - last_report_time[0] >= 10 or processed == total:
            pct = processed * 100 // total if total > 0 else 0
            log.info(f"  Embedded {processed:,}/{total:,} documents ({pct}%)")
            last_report_time[0] = now

    # Use the store's built-in backfill method — it handles batching,
    # commit-per-batch for resumability, and individual fallback internally.
    result = store.backfill_embeddings(
        batch_size=batch_size,
        max_docs=max_docs,
        callback=progress_callback,
    )

    # Final report
    processed = result.get("processed", 0)
    failed = result.get("failed", 0)
    elapsed = result.get("elapsed_seconds", 0)
    rate = result.get("rate_docs_per_sec", 0)

    print(f"\n{'='*60}")
    print(f"  Backfill Complete")
    print(f"{'='*60}")
    print(f"  Processed:  {processed:,}")
    print(f"  Failed:     {failed:,}")
    print(f"  Elapsed:    {elapsed:.1f}s")
    print(f"  Rate:       {rate:.1f} docs/s")

    if failed > 0:
        print(f"\n  WARNING: {failed:,} documents failed to embed.")
        print(f"  Re-run this script to retry them (safe to re-run).")

    print()

    # Print post-backfill status
    print_status(store)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backfill embeddings for documents missing them in cortex-vectors.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The script auto-detects the DB location by checking:\n"
            "  1. CORTEX_DATA_DIR env var\n"
            "  2. ~/clawd/data/cortex-vectors.db\n"
            "  3. ~/.cortex/data/cortex-vectors.db\n"
            "\n"
            "It picks the largest existing file (more data = primary DB).\n"
            "Use --db-path to override."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of documents to embed per batch (default: 50). "
             "Lower values use less memory.",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Explicit path to cortex-vectors.db (overrides auto-detection).",
    )
    parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Maximum number of documents to process (default: all).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only print embedding status, don't process anything.",
    )
    args = parser.parse_args()

    # Resolve DB path
    if args.db_path:
        db_path = Path(args.db_path).expanduser().resolve()
        if not db_path.exists():
            log.error(f"Specified DB path does not exist: {db_path}")
            sys.exit(1)
    else:
        log.info("Auto-detecting DB path...")
        db_path = _detect_db_path()

    log.info(f"Using DB: {db_path}")

    if not db_path.exists():
        log.error(
            f"Database not found at {db_path}. "
            f"Run the memory worker first to create it, or specify --db-path."
        )
        sys.exit(1)

    # Load the store
    store = _load_store(db_path)

    if args.status:
        print_status(store)
        sys.exit(0)

    # Run the backfill
    result = run_backfill(store, args.batch_size, args.max_docs)

    # Exit code: 0 if all succeeded, 1 if any failures
    if result.get("failed", 0) > 0:
        sys.exit(1)
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
