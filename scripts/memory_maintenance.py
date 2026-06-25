#!/usr/bin/env python3
"""Maintenance tasks for local Cortex memory stores."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
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


def resync_vectors(args: argparse.Namespace) -> int:
    conn = connect(OBS_DB)
    rows = conn.execute(
        """
        SELECT id, source, tool_name, agent, summary
        FROM observations
        WHERE status = 'processed'
          AND vector_synced = 0
          AND COALESCE(memory_type, 'episodic') = 'episodic'
          AND COALESCE(summary, '') != ''
        ORDER BY id
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"episodic rows pending vector sync: {len(rows)}")
    if args.dry_run or not rows:
        conn.close()
        return 0

    obs_bak = backup(OBS_DB)
    vec_bak = backup(VECTOR_DB) if VECTOR_DB.exists() else None

    sys.path.insert(0, str((Path(__file__).resolve().parent.parent / "src")))
    from unified_vector_store import get_vector_store  # noqa: PLC0415

    store = get_vector_store()
    synced = 0
    failed: list[tuple[int, str]] = []
    for row in rows:
        try:
            store.add_observation(
                obs_id=str(row["id"]),
                text=row["summary"],
                metadata={
                    "source": row["source"],
                    "tool_name": row["tool_name"] or "",
                    "agent": row["agent"] or "main",
                },
            )
            conn.execute("UPDATE observations SET vector_synced = 1 WHERE id = ?", (row["id"],))
            synced += 1
        except Exception as exc:  # noqa: BLE001 - maintenance should report and continue.
            failed.append((row["id"], str(exc)))
    conn.commit()
    conn.close()

    print(f"observations backup: {obs_bak}")
    if vec_bak:
        print(f"vector backup: {vec_bak}")
    print(f"vector synced: {synced}")
    if failed:
        print(f"vector sync failures: {len(failed)}")
        for obs_id, error in failed[:10]:
            print(f"- {obs_id}: {error}")
        return 1
    return 0


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def close_stale_sessions(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    cutoff_seconds = args.older_than_hours * 3600
    conn = connect(OBS_DB)
    rows = conn.execute(
        """
        SELECT s.id, s.started_at, s.observation_count, MAX(o.timestamp) AS last_observation_at
        FROM sessions s
        LEFT JOIN observations o ON o.session_id = s.id
        WHERE s.status = 'active'
        GROUP BY s.id
        """
    ).fetchall()

    stale: list[sqlite3.Row] = []
    skipped_unparseable: list[str] = []
    for row in rows:
        last_seen = _parse_dt(row["last_observation_at"]) or _parse_dt(row["started_at"])
        if last_seen is None:
            skipped_unparseable.append(row["id"])
            continue
        age_seconds = (now - last_seen).total_seconds()
        if age_seconds >= cutoff_seconds:
            stale.append(row)

    print(f"active sessions: {len(rows)}")
    print(f"stale sessions older than {args.older_than_hours}h: {len(stale)}")
    if skipped_unparseable:
        print(f"skipped unparseable sessions: {len(skipped_unparseable)}")
    if args.preview:
        preview = [
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "last_observation_at": row["last_observation_at"],
                "observation_count": row["observation_count"],
            }
            for row in stale[: args.preview]
        ]
        print(json.dumps(preview, indent=2))
    if args.dry_run or not stale:
        conn.close()
        return 0

    bak = backup(OBS_DB)
    ended_at = now.isoformat()
    conn.executemany(
        "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ? AND status = 'active'",
        [(ended_at, row["id"]) for row in stale],
    )
    conn.commit()
    conn.close()
    print(f"backup: {bak}")
    print(f"closed stale sessions: {len(stale)}")
    return 0


def _row_value(row: sqlite3.Row, key: str, default: str = "") -> str:
    value = row[key] if key in row.keys() else default
    return value or default


def _session_summary_rule_based(rows: list[sqlite3.Row], user_prompt: str | None, agent: str) -> dict:
    tool_counts: dict[str, int] = {}
    file_paths: set[str] = set()
    decisions: list[str] = []

    for row in rows:
        tool = _row_value(row, "tool_name")
        if tool:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
        summary = _row_value(row, "summary")
        raw_input = _row_value(row, "raw_input")
        for text in (summary, raw_input):
            file_paths.update(p for p in re.findall(r"(?:/[\w./-]+\.[\w]+)", text) if len(p) > 5)
        if tool in {"Write", "Edit"} and summary:
            decisions.append(summary[:150])

    top_tools = sorted(tool_counts.items(), key=lambda item: -item[1])[:5]
    parts = [f"[{agent}] Session with {len(rows)} observations."]
    if user_prompt:
        parts.append(f"Task: {user_prompt[:200]}")
    if top_tools:
        parts.append("Tools: " + ", ".join(f"{tool}({count})" for tool, count in top_tools))
    if file_paths:
        parts.append("Files: " + ", ".join(sorted(file_paths)[:10]))
    return {
        "summary": " | ".join(parts),
        "key_decisions": decisions[:10],
        "entities_mentioned": sorted(file_paths)[:20],
    }


def summarize_ended_sessions(args: argparse.Namespace) -> int:
    conn = connect(OBS_DB)
    sessions = conn.execute(
        "SELECT id, agent, user_prompt FROM sessions WHERE status = 'ended' ORDER BY ended_at ASC LIMIT ?",
        (args.limit,),
    ).fetchall()
    print(f"ended sessions pending summary: {len(sessions)}")
    if args.dry_run or not sessions:
        conn.close()
        return 0

    bak = backup(OBS_DB)
    summarized = 0
    for session in sessions:
        rows = conn.execute(
            "SELECT id, source, tool_name, agent, summary, raw_input, raw_output "
            "FROM observations WHERE session_id = ? AND status = 'processed' ORDER BY id ASC",
            (session["id"],),
        ).fetchall()
        agent = session["agent"] or "main"
        if rows:
            rb = _session_summary_rule_based(rows, session["user_prompt"], agent)
            summary = rb["summary"]
            key_decisions = json.dumps(rb["key_decisions"])
            entities = json.dumps(rb["entities_mentioned"])
        else:
            summary = f"[{agent}] Session ended with no processed observations."
            key_decisions = "[]"
            entities = "[]"

        conn.execute(
            "INSERT INTO session_summaries (session_id, summary, key_decisions, entities_mentioned) "
            "VALUES (?, ?, ?, ?)",
            (session["id"], summary, key_decisions, entities),
        )
        conn.execute(
            "UPDATE sessions SET summary = ?, status = 'summarized' WHERE id = ?",
            (summary, session["id"]),
        )
        summarized += 1
    conn.commit()
    conn.close()
    print(f"backup: {bak}")
    print(f"summarized sessions: {summarized}")
    return 0


def _retag_vector_documents(obs_ids: list[int], target_agent: str) -> int:
    if not VECTOR_DB.exists() or not obs_ids:
        return 0
    conn = connect(VECTOR_DB)
    updated = 0
    try:
        for i in range(0, len(obs_ids), 500):
            batch = obs_ids[i:i + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT id, text, metadata FROM documents WHERE id IN ({placeholders})",
                [f"obs-{obs_id}" for obs_id in batch],
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                metadata["agent"] = target_agent
                text = (row["text"] or "").replace("[main]", f"[{target_agent}]", 1)
                text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
                conn.execute(
                    "UPDATE documents SET text = ?, metadata = ?, text_hash = ? WHERE id = ?",
                    (text, json.dumps(metadata), text_hash, row["id"]),
                )
                updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def _retag_main_vector_documents(target_agent: str) -> int:
    if not VECTOR_DB.exists():
        return 0
    conn = connect(VECTOR_DB)
    rows = conn.execute(
        "SELECT id, text, metadata FROM documents "
        "WHERE collection = 'observations' "
        "AND (text LIKE '[main]%' OR metadata LIKE '%\"agent\": \"main\"%' OR metadata LIKE '%\"agent\":\"main\"%')"
    ).fetchall()
    updated = 0
    try:
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            metadata["agent"] = target_agent
            text = (row["text"] or "").replace("[main]", f"[{target_agent}]", 1)
            text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
            conn.execute(
                "UPDATE documents SET text = ?, metadata = ?, text_hash = ? WHERE id = ?",
                (text, json.dumps(metadata), text_hash, row["id"]),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def backfill_main_agent(args: argparse.Namespace) -> int:
    target_agent = args.target_agent
    conn = connect(OBS_DB)
    obs_rows = conn.execute(
        """
        SELECT id FROM observations
        WHERE agent = 'main'
          AND source IN ('post_tool_use', 'user_prompt', 'session_end')
        ORDER BY id
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    session_rows = conn.execute(
        """
        SELECT id FROM sessions
        WHERE agent = 'main'
          AND id IN (
              SELECT DISTINCT session_id FROM observations
              WHERE agent = 'main'
                AND source IN ('post_tool_use', 'user_prompt', 'session_end')
          )
        ORDER BY id
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()

    print(f"observations eligible for main->{target_agent}: {len(obs_rows)}")
    print(f"sessions eligible for main->{target_agent}: {len(session_rows)}")
    if args.dry_run:
        conn.close()
        return 0

    obs_bak = backup(OBS_DB)
    vec_bak = backup(VECTOR_DB) if VECTOR_DB.exists() else None
    obs_ids = [row["id"] for row in obs_rows]
    session_ids = [row["id"] for row in session_rows]

    if obs_ids:
        placeholders = ",".join("?" for _ in obs_ids)
        conn.execute(
            f"UPDATE observations SET agent = ?, summary = replace(summary, '[main]', ?) "
            f"WHERE id IN ({placeholders})",
            [target_agent, f"[{target_agent}]"] + obs_ids,
        )
    if session_ids:
        sess_placeholders = ",".join("?" for _ in session_ids)
        conn.execute(
            f"UPDATE sessions SET agent = ?, summary = replace(summary, '[main]', ?) "
            f"WHERE id IN ({sess_placeholders})",
            [target_agent, f"[{target_agent}]"] + session_ids,
        )
        conn.execute(
            f"UPDATE session_summaries SET summary = replace(summary, '[main]', ?) "
            f"WHERE session_id IN ({sess_placeholders})",
            [f"[{target_agent}]"] + session_ids,
        )
    conn.commit()
    conn.close()

    vector_docs = _retag_vector_documents(obs_ids, target_agent)
    vector_main_docs = _retag_main_vector_documents(target_agent)
    print(f"observations backup: {obs_bak}")
    if vec_bak:
        print(f"vector backup: {vec_bak}")
    print(f"observations retagged: {len(obs_ids)}")
    print(f"sessions retagged: {len(session_ids)}")
    print(f"vector documents retagged: {vector_docs}")
    print(f"residual main vector documents retagged: {vector_main_docs}")
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

    p = sub.add_parser("resync-vectors")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=resync_vectors)

    p = sub.add_parser("close-stale-sessions")
    p.add_argument("--older-than-hours", type=float, default=24)
    p.add_argument("--preview", type=int, default=5)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=close_stale_sessions)

    p = sub.add_parser("summarize-ended-sessions")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=summarize_ended_sessions)

    p = sub.add_parser("backfill-main-agent")
    p.add_argument("--target-agent", default="claude-code")
    p.add_argument("--limit", type=int, default=50000)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=backfill_main_agent)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
