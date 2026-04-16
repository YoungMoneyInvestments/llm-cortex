#!/usr/bin/env python3
"""
One-time cleanup: purge stale observation IDs from memcells.observation_ids.

BUG-C1-06 forward fix is in memory_worker.RetentionManager._cascade_memcell_cleanup().
This script handles the orphans that accumulated before that fix landed.

Safety guarantees:
- Backs up nothing itself — caller must backup the DB first.
- Idempotent: safe to re-run; a second pass finds 0 rows and exits cleanly.
- Batched commits: reads 200 memcell rows at a time, commits per batch so the
  live worker is never blocked for more than a handful of milliseconds.
- Never deletes observations — only cleans memcell rows/arrays.
- Memcell rows whose observation_ids array becomes [] are deleted (empty
  memcells have no useful content and no readers depend on them).

Usage:
    python3 scripts/cleanup_memcell_orphans.py [--db PATH] [--dry-run] [--batch 200]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path.home() / "clawd" / "data" / "cortex-observations.db"
BATCH_SIZE = 200


def count_orphans(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        WITH mc_refs AS (
            SELECT CAST(je.value AS INTEGER) AS obs_id
            FROM memcells mc, json_each(mc.observation_ids) je
        )
        SELECT COUNT(*) AS n FROM mc_refs
        WHERE obs_id NOT IN (SELECT id FROM observations)
        """
    ).fetchone()
    return row[0] if row else 0


def run_cleanup(db_path: Path, dry_run: bool, batch_size: int) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    before = count_orphans(conn)
    print(f"Orphaned memcell refs before cleanup: {before}")

    if before == 0:
        print("Nothing to clean.")
        conn.close()
        return {"orphans_before": 0, "orphans_after": 0, "rows_updated": 0, "rows_deleted": 0}

    # Collect ALL current observation IDs for fast membership tests.
    # The table can have ~11k rows — fits in memory comfortably.
    live_ids: set[int] = {
        row[0] for row in conn.execute("SELECT id FROM observations").fetchall()
    }

    # Scan memcells in batches ordered by id to avoid re-scanning rows.
    last_id = 0
    total_updated = 0
    total_deleted = 0

    while True:
        rows = conn.execute(
            "SELECT id, observation_ids FROM memcells WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()

        if not rows:
            break

        updates: list[tuple[str, int]] = []
        deletes: list[int] = []

        for row in rows:
            try:
                obs_list: list[int] = json.loads(row["observation_ids"])
            except (ValueError, TypeError):
                last_id = row["id"]
                continue

            cleaned = [oid for oid in obs_list if oid in live_ids]

            if len(cleaned) == len(obs_list):
                # No change — all IDs still live.
                last_id = row["id"]
                continue

            if cleaned:
                updates.append((json.dumps(cleaned), row["id"]))
            else:
                deletes.append(row["id"])

            last_id = row["id"]

        if not dry_run and (updates or deletes):
            conn.execute("BEGIN")
            if updates:
                conn.executemany(
                    "UPDATE memcells SET observation_ids = ? WHERE id = ?",
                    updates,
                )
            if deletes:
                del_ph = ",".join("?" for _ in deletes)
                conn.execute(f"DELETE FROM memcells WHERE id IN ({del_ph})", deletes)
            conn.execute("COMMIT")

        total_updated += len(updates)
        total_deleted += len(deletes)

        action = "Would fix" if dry_run else "Fixed"
        if updates or deletes:
            print(
                f"  {action} batch ending at memcell id={last_id}: "
                f"{len(updates)} updated, {len(deletes)} deleted"
            )

    after = count_orphans(conn)
    conn.close()

    print(f"\nOrphaned memcell refs after cleanup: {after}")
    print(f"Total memcell rows updated: {total_updated}")
    print(f"Total memcell rows deleted (became empty): {total_deleted}")

    return {
        "orphans_before": before,
        "orphans_after": after,
        "rows_updated": total_updated,
        "rows_deleted": total_deleted,
        "dry_run": dry_run,
    }


def main():
    parser = argparse.ArgumentParser(description="Clean orphaned memcell observation refs")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to cortex-observations.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--batch", type=int, default=BATCH_SIZE, help="Rows per commit batch")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("DRY RUN — no changes will be written.\n")

    result = run_cleanup(args.db, args.dry_run, args.batch)

    if not args.dry_run and result["orphans_after"] != 0:
        print(f"\nWARNING: {result['orphans_after']} orphan refs remain after cleanup!", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
