#!/usr/bin/env python3
"""
Cortex DB maintenance script — weekly VACUUM + FTS optimize + WAL checkpoint.

Responsibilities per run:
  - PRAGMA wal_checkpoint(TRUNCATE) on each DB
  - FTS5 optimize (documents_fts / observations_fts) where present
  - VACUUM on each DB
  - Log start/end time + before/after file sizes
  - Write ~/.cortex/data/maintenance-last-run.json
  - Exit 0 on clean run (even if some DBs were skipped), 1 on unexpected failure

Concurrency:
  - flock on /tmp/cortex_maintenance.lock — overlapping runs skip cleanly
  - Per-DB WAL mtime guard: skip if <db>-wal was modified within 5 seconds
    (Pass Q coordination — entity merge writes may be in flight)

Usage:
    python scripts/maintenance.py [--dry-run] [--data-dir PATH]

Options:
    --dry-run       Print planned actions without executing them.
    --data-dir PATH Override CORTEX_DATA_DIR.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOCK_FILE = Path("/tmp/cortex_maintenance.lock")
RECENT_WRITE_THRESHOLD_S = 5  # skip a DB if its WAL was touched < 5s ago
BUSY_TIMEOUT_MS = 30_000       # 30s; worker must not be blocked longer

_DEFAULT_DATA_DIR = Path(
    os.environ.get("CORTEX_DATA_DIR", str(Path.home() / ".cortex" / "data"))
).expanduser()

# Each entry: (db filename, fts_table or None)
_DB_SPECS = [
    ("cortex-vectors.db", "documents_fts"),
    ("cortex-observations.db", "observations_fts"),
    ("cortex-knowledge-graph.db", None),
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("cortex-maintenance")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_size_mb(path: Path) -> float | None:
    try:
        return round(path.stat().st_size / (1024 * 1024), 3)
    except OSError:
        return None


def _wal_age_s(db_path: Path) -> float | None:
    """Return seconds since the WAL file was last modified, or None if absent."""
    wal = Path(str(db_path) + "-wal")
    try:
        mtime = wal.stat().st_mtime
        return time.time() - mtime
    except OSError:
        return None


def _run_maintenance_on(
    db_path: Path,
    fts_table: str | None,
    dry_run: bool,
) -> dict:
    """
    Run checkpoint + FTS optimize + VACUUM on a single database.

    Returns a dict with per-DB results for the summary JSON.
    Raises nothing — all exceptions are caught and stored in the result.
    """
    result: dict = {
        "db": str(db_path),
        "status": "unknown",
        "size_before_mb": None,
        "size_after_mb": None,
        "wal_age_s": None,
        "phases": {},
        "error": None,
    }

    # -- Skip if file is missing
    if not db_path.exists():
        result["status"] = "skipped_missing"
        log.warning("  Skipping %s — file not found", db_path.name)
        return result

    # -- Skip if 0-byte (e.g. empty cortex-observations.db)
    if db_path.stat().st_size == 0:
        result["status"] = "skipped_empty"
        log.warning("  Skipping %s — file is 0 bytes", db_path.name)
        return result

    # -- Skip if WAL was recently written (Pass Q guard)
    wal_age = _wal_age_s(db_path)
    result["wal_age_s"] = round(wal_age, 2) if wal_age is not None else None
    if wal_age is not None and wal_age < RECENT_WRITE_THRESHOLD_S:
        result["status"] = "skipped_recent_write"
        log.warning(
            "  Skipping %s — WAL modified %.1fs ago (< %ss threshold)",
            db_path.name,
            wal_age,
            RECENT_WRITE_THRESHOLD_S,
        )
        return result

    result["size_before_mb"] = _file_size_mb(db_path)
    log.info("  Processing %s (%.3f MB)", db_path.name, result["size_before_mb"] or 0)

    if dry_run:
        result["status"] = "dry_run"
        result["phases"] = {
            "checkpoint": "skipped (dry-run)",
            "fts_optimize": "skipped (dry-run)" if fts_table else "n/a",
            "vacuum": "skipped (dry-run)",
        }
        return result

    try:
        # isolation_level=None = autocommit mode.  VACUUM and FTS optimize both
        # require that no transaction is open; autocommit prevents the implicit
        # BEGIN that Python's sqlite3 module inserts around DML statements.
        conn = sqlite3.connect(
            str(db_path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

        # Phase 1: WAL checkpoint
        t0 = time.monotonic()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            result["phases"]["checkpoint"] = f"ok ({time.monotonic() - t0:.2f}s)"
            log.info("    checkpoint: ok")
        except sqlite3.OperationalError as exc:
            result["phases"]["checkpoint"] = f"error: {exc}"
            log.warning("    checkpoint failed: %s", exc)

        # Phase 2: FTS5 optimize (only where a table exists)
        if fts_table:
            t0 = time.monotonic()
            try:
                conn.execute(
                    f"INSERT INTO {fts_table}({fts_table}) VALUES('optimize')"
                )
                result["phases"]["fts_optimize"] = f"ok ({time.monotonic() - t0:.2f}s)"
                log.info("    fts_optimize(%s): ok", fts_table)
            except sqlite3.OperationalError as exc:
                result["phases"]["fts_optimize"] = f"error: {exc}"
                log.warning("    fts_optimize(%s) failed: %s", fts_table, exc)
        else:
            result["phases"]["fts_optimize"] = "n/a"

        # Phase 3: VACUUM
        t0 = time.monotonic()
        try:
            conn.execute("VACUUM")
            result["phases"]["vacuum"] = f"ok ({time.monotonic() - t0:.2f}s)"
            log.info("    vacuum: ok")
        except sqlite3.OperationalError as exc:
            result["phases"]["vacuum"] = f"error: {exc}"
            log.warning("    vacuum failed: %s", exc)

        conn.close()

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        log.error("  Unexpected error on %s: %s", db_path.name, exc)
        return result

    result["size_after_mb"] = _file_size_mb(db_path)
    saved = (
        round((result["size_before_mb"] or 0) - (result["size_after_mb"] or 0), 3)
        if result["size_before_mb"] is not None and result["size_after_mb"] is not None
        else None
    )
    result["saved_mb"] = saved
    result["status"] = "ok"
    log.info(
        "  Done %s — %.3f MB → %.3f MB (saved %.3f MB)",
        db_path.name,
        result["size_before_mb"] or 0,
        result["size_after_mb"] or 0,
        saved or 0,
    )
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cortex weekly DB maintenance")
    parser.add_argument("--dry-run", action="store_true", help="Print plan, no writes")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override CORTEX_DATA_DIR (default: ~/.cortex/data)",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else _DEFAULT_DATA_DIR

    log.info("=== Cortex maintenance START ===")
    log.info("data_dir: %s  dry_run: %s", data_dir, args.dry_run)

    # --- Concurrency lock ------------------------------------------------
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("Another maintenance run is already in progress — skipping.")
        lock_fh.close()
        return 0

    started_at = datetime.now(timezone.utc).isoformat()
    db_results: list[dict] = []
    unexpected_failure = False

    try:
        for db_filename, fts_table in _DB_SPECS:
            db_path = data_dir / db_filename
            log.info("--- %s ---", db_filename)
            result = _run_maintenance_on(db_path, fts_table, dry_run=args.dry_run)
            if result["status"] == "error":
                unexpected_failure = True
            db_results.append(result)

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    finished_at = datetime.now(timezone.utc).isoformat()
    exit_code = 1 if unexpected_failure else 0

    summary = {
        "started_at": started_at,
        "finished_at": finished_at,
        "dry_run": args.dry_run,
        "exit_code": exit_code,
        "databases": db_results,
    }

    # Write summary JSON
    summary_path = data_dir / "maintenance-last-run.json"
    if not args.dry_run:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, indent=2))
            log.info("Summary written to %s", summary_path)
        except OSError as exc:
            log.warning("Could not write summary JSON: %s", exc)
    else:
        log.info("Dry-run — would write summary to %s", summary_path)
        # Print the JSON preview so the operator can see what would happen
        print(json.dumps(summary, indent=2))

    log.info(
        "=== Cortex maintenance %s (exit %d) ===",
        "COMPLETE" if exit_code == 0 else "FAILED",
        exit_code,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
