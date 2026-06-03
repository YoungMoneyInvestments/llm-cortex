#!/usr/bin/env python3
"""Maintenance tasks for local Cortex memory stores."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


HOME = Path.home()
OBS_DB = HOME / ".cortex" / "data" / "cortex-observations.db"
VECTOR_DB = HOME / ".cortex" / "data" / "cortex-vectors.db"
BACKUP_DIR = HOME / ".cortex" / "backups"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def backup(path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, dest)
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    if wal.exists():
        shutil.copy2(wal, BACKUP_DIR / f"{wal.name}.{stamp}.bak")
    if shm.exists():
        shutil.copy2(shm, BACKUP_DIR / f"{shm.name}.{stamp}.bak")
    return dest


def audit(_: argparse.Namespace) -> int:
    conn = connect(OBS_DB)
    print("Cortex Observation Audit")
    for label, sql in {
        "agents": "SELECT agent, COUNT(*) c FROM observations GROUP BY agent ORDER BY c DESC",
        "status": "SELECT status, COUNT(*) c FROM observations GROUP BY status ORDER BY c DESC",
        "memory_type": "SELECT COALESCE(memory_type,'(null)') memory_type, COUNT(*) c FROM observations GROUP BY memory_type ORDER BY c DESC",
        "vector": "SELECT vector_synced, COUNT(*) c FROM observations GROUP BY vector_synced ORDER BY vector_synced",
    }.items():
        print(f"\n## {label}")
        for row in conn.execute(sql):
            print(dict(row))
    print("\n## session counter drift")
    row = conn.execute(
        """
        SELECT COUNT(*) sessions
        FROM sessions s
        LEFT JOIN (SELECT session_id, COUNT(*) actual FROM observations GROUP BY session_id) a
          ON a.session_id = s.id
        WHERE s.observation_count != COALESCE(a.actual, 0)
        """
    ).fetchone()
    print(dict(row))
    print("\n## legacy gitnexus mentions")
    row = conn.execute(
        """
        SELECT COUNT(*) c FROM observations
        WHERE lower(COALESCE(tool_name,'') || ' ' || COALESCE(summary,'') || ' ' ||
                    COALESCE(raw_input,'') || ' ' || COALESCE(raw_output,'')) LIKE '%gitnexus%'
        """
    ).fetchone()
    print(dict(row))
    conn.close()
    return 0


def repair_session_counts(args: argparse.Namespace) -> int:
    conn = connect(OBS_DB)
    rows = conn.execute(
        """
        SELECT s.id, s.observation_count, COALESCE(a.actual, 0) actual
        FROM sessions s
        LEFT JOIN (SELECT session_id, COUNT(*) actual FROM observations GROUP BY session_id) a
          ON a.session_id = s.id
        WHERE s.observation_count != COALESCE(a.actual, 0)
        """
    ).fetchall()
    print(f"session counter drift rows: {len(rows)}")
    if args.dry_run:
        conn.close()
        return 0
    bak = backup(OBS_DB)
    conn.execute(
        """
        UPDATE sessions
        SET observation_count = (
            SELECT COUNT(*) FROM observations WHERE observations.session_id = sessions.id
        )
        """
    )
    conn.commit()
    conn.close()
    print(f"backup: {bak}")
    print("session counters repaired")
    return 0


def sanitize_legacy_gitnexus(args: argparse.Namespace) -> int:
    conn = connect(OBS_DB)
    count = conn.execute(
        """
        SELECT COUNT(*) c FROM observations
        WHERE lower(COALESCE(tool_name,'') || ' ' || COALESCE(summary,'') || ' ' ||
                    COALESCE(raw_input,'') || ' ' || COALESCE(raw_output,'')) LIKE '%gitnexus%'
        """
    ).fetchone()["c"]
    print(f"legacy gitnexus rows: {count}")
    if args.dry_run or count == 0:
        conn.close()
        return 0
    bak = backup(OBS_DB)
    replacements = [
        ("Gitnexus", "legacy-code-index"),
        ("GitNexus", "legacy-code-index"),
        ("gitnexus", "legacy-code-index"),
    ]
    for old, new in replacements:
        conn.execute(
            """
            UPDATE observations
            SET tool_name = replace(tool_name, ?, ?),
                summary = replace(summary, ?, ?),
                raw_input = replace(raw_input, ?, ?),
                raw_output = replace(raw_output, ?, ?)
            WHERE tool_name LIKE ? OR summary LIKE ? OR raw_input LIKE ? OR raw_output LIKE ?
            """,
            (old, new, old, new, old, new, old, new, f"%{old}%", f"%{old}%", f"%{old}%", f"%{old}%"),
        )
    conn.commit()
    conn.execute("INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()
    print(f"backup: {bak}")
    print("legacy gitnexus text sanitized and FTS rebuilt")
    return 0


def checkpoint_vector(_: argparse.Namespace) -> int:
    if not VECTOR_DB.exists():
        print(f"missing vector DB: {VECTOR_DB}")
        return 1
    bak = backup(VECTOR_DB)
    conn = connect(VECTOR_DB)
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        print(f"wal_checkpoint: {[tuple(r) for r in result]}")
    finally:
        conn.close()
    print(f"backup: {bak}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cortex memory maintenance")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("audit").set_defaults(func=audit)

    p = sub.add_parser("repair-session-counts")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=repair_session_counts)

    p = sub.add_parser("sanitize-legacy-gitnexus")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=sanitize_legacy_gitnexus)

    sub.add_parser("checkpoint-vector").set_defaults(func=checkpoint_vector)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
