#!/usr/bin/env python3
"""
Pass Q — Entity Merge / Alias Script
=====================================
Implements safe entity deduplication for the Cortex knowledge graph.

Background
----------
Entities are stored with id = _normalize_id(display_name) (lowercase, spaces->underscores).
Because id is PRIMARY KEY, two entities can NEVER share the same normalized name.
This means the strict-rule merge path (same display_name case-insensitively) produces 0
candidates — normalization already prevents that class of duplicate from being created.

What this script DOES do:
  1.  Strict-rule merge dry-run: reports candidates under the spec-defined rules.
      (Expected result: 0 candidates — confirms the normalization invariant.)
  2.  Retype pass: updates entity_type for a curated set of well-known entities
      that were created with type='unknown' because they were first seen outside of
      the KNOWN_SYSTEMS/KNOWN_PROJECTS sets in EntityExtractor.
  3.  Documents deferral reasons for near-duplicate classes that are intentionally
      excluded (singular/plural, Levenshtein-1/2, sentence-fragment unknowns).

Safety rules (from spec, never relaxed without explicit justification in this file):
  - Do NOT delete entities with >0 edges without aliasing first.
  - Do NOT merge entities of different canonical types.
  - Do NOT auto-merge if loser has >100 edges (heavy entity — flag for manual review).
  - Do NOT auto-merge if loser was created within the last 7 days.
  - Do NOT auto-merge if display_names differ by more than case + trailing whitespace.

Usage
-----
    # Dry-run (default): show what would be merged, no changes
    python scripts/merge_duplicate_entities.py

    # Retype-only pass: update entity_type for well-known unknowns (no merges)
    python scripts/merge_duplicate_entities.py --retype

    # Actual merge run (requires --no-dry-run explicit opt-in):
    python scripts/merge_duplicate_entities.py --no-dry-run

    # Show first 20 merge candidates only (for review):
    python scripts/merge_duplicate_entities.py --show-candidates

"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path.home() / "clawd" / "data" / "cortex-knowledge-graph.db"

# ── Safety thresholds (spec-mandated, do NOT lower without adding a justification) ──
MAX_LOSER_EDGE_COUNT = 100       # spec: do not auto-merge if loser has >100 edges
RECENT_DAYS_WINDOW   = 7         # spec: do not auto-merge if loser was created < 7 days ago

# ── Curated retype table ──
# Entities currently stored as 'unknown' that are clearly a specific type.
# This is NOT a merge — there is no duplicate to merge into; we just fix the type.
#
# Format: (entity_id, correct_type, rationale)
# Only include entities where there is NO risk of semantic confusion.
RETYPE_CANDIDATES: list[tuple[str, str, str]] = [
    # Tools / platforms
    ("github",       "tool",    "GitHub is a well-known source control platform"),
    ("openai",       "company", "OpenAI is a well-known AI company"),
    ("ninjatrader",  "system",  "NinjaTrader is a trading platform used in this system"),
    ("nt8",          "system",  "NT8 = NinjaTrader 8, alias for the trading platform"),
    ("imessage",     "system",  "iMessage is an Apple messaging system"),
    ("visionclaw",   "system",  "VisionClaw is a Cameron-built vision/camera integration"),
    ("pypi",         "tool",    "PyPI is the Python package index"),
    ("oauth",        "system",  "OAuth is an authentication protocol/system"),
    ("nssm",         "tool",    "NSSM is Non-Sucking Service Manager, a Windows service tool"),
    ("json",         "system",  "JSON is a data format; tagged as system to match MCP/SQLite pattern"),
]


def get_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_edge_count(conn: sqlite3.Connection, entity_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as c FROM relationships WHERE source = ? OR target = ?",
        (entity_id, entity_id),
    ).fetchone()
    return row["c"] if row else 0


def get_entity(conn: sqlite3.Connection, entity_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, entity_type, display_name, created_at FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Merge logic
# ─────────────────────────────────────────────────────────────────────────────

def find_strict_merge_candidates(conn: sqlite3.Connection) -> list[dict]:
    """
    Find candidates under strict spec rules:
      - unknown entity whose display_name matches a typed entity's display_name
        case-insensitively (rule from spec Section "Merge rules (high-confidence only)").

    Because entity id = _normalize_id(display_name) = lower+underscores, two entities
    with the same normalized display_name cannot coexist (PRIMARY KEY constraint).
    As a result, this function is expected to return 0 candidates.

    It is kept for completeness and verification — the dry-run output documents this.
    """
    rows = conn.execute("""
        SELECT e1.id       AS loser_id,
               e1.entity_type AS loser_type,
               e1.display_name AS loser_display,
               e1.created_at   AS loser_created,
               e2.id           AS winner_id,
               e2.entity_type  AS winner_type,
               e2.display_name AS winner_display
        FROM entities e1
        JOIN entities e2
          ON lower(trim(coalesce(e1.display_name, '')))
           = lower(trim(coalesce(e2.display_name, '')))
        WHERE e1.entity_type  = 'unknown'
          AND e2.entity_type != 'unknown'
          AND e1.id != e2.id
          AND e1.display_name IS NOT NULL
          AND e2.display_name IS NOT NULL
          AND trim(e1.display_name) != ''
    """).fetchall()

    candidates = []
    for row in rows:
        loser_edges = get_edge_count(conn, row["loser_id"])

        # Parse created_at
        try:
            created_at = datetime.fromisoformat(row["loser_created"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            created_at = None

        # Apply safety gates
        skip_reason = None
        if loser_edges > MAX_LOSER_EDGE_COUNT:
            skip_reason = f"loser has {loser_edges} edges (>{MAX_LOSER_EDGE_COUNT})"
        elif created_at and (now_utc() - created_at) < timedelta(days=RECENT_DAYS_WINDOW):
            skip_reason = f"loser created {(now_utc() - created_at).days}d ago (<{RECENT_DAYS_WINDOW}d window)"

        candidates.append({
            "loser_id":       row["loser_id"],
            "loser_type":     row["loser_type"],
            "loser_display":  row["loser_display"],
            "loser_edges":    loser_edges,
            "winner_id":      row["winner_id"],
            "winner_type":    row["winner_type"],
            "winner_display": row["winner_display"],
            "skip_reason":    skip_reason,
        })

    return candidates


def execute_merge(conn: sqlite3.Connection, candidate: dict, dry_run: bool) -> bool:
    """
    Merge loser entity into winner entity.

    Steps (inside a single transaction):
      1. Update all relationships pointing at loser_id -> winner_id.
      2. Add alias: loser_display -> winner_id.
      3. Delete loser entity row.

    Returns True if merge succeeded (or would succeed in dry-run), False on error.
    """
    if candidate["skip_reason"]:
        return False

    loser_id    = candidate["loser_id"]
    winner_id   = candidate["winner_id"]
    loser_disp  = candidate["loser_display"] or loser_id

    if dry_run:
        edges = candidate["loser_edges"]
        print(
            f"  [DRY-RUN] MERGE  '{loser_id}' ({candidate['loser_type']}) "
            f"-> '{winner_id}' ({candidate['winner_type']})  edges={edges}"
        )
        return True

    try:
        conn.execute("BEGIN")

        # 1. Repoint relationships
        conn.execute(
            "UPDATE relationships SET source = ? WHERE source = ?",
            (winner_id, loser_id),
        )
        conn.execute(
            "UPDATE relationships SET target = ? WHERE target = ?",
            (winner_id, loser_id),
        )

        # 2. Register alias  (loser_display -> winner_id)
        conn.execute("""
            INSERT INTO aliases (alias, canonical_id)
            VALUES (?, ?)
            ON CONFLICT(alias) DO UPDATE SET canonical_id = excluded.canonical_id
        """, (loser_id, winner_id))

        # 3. Delete the loser entity
        conn.execute("DELETE FROM entities WHERE id = ?", (loser_id,))

        conn.execute("COMMIT")
        return True

    except Exception as exc:
        conn.execute("ROLLBACK")
        print(f"  [ERROR] Merge {loser_id} -> {winner_id} failed: {exc}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Retype logic
# ─────────────────────────────────────────────────────────────────────────────

def execute_retype(conn: sqlite3.Connection, dry_run: bool) -> int:
    """
    Update entity_type for curated RETYPE_CANDIDATES.

    This is NOT a merge — we are fixing the type of an existing entity that has
    no typed counterpart to merge into. The entity id stays, display_name stays;
    only entity_type changes.

    Returns count of retypes applied (or would-be count in dry-run).
    """
    retypes_applied = 0

    for entity_id, correct_type, rationale in RETYPE_CANDIDATES:
        entity = get_entity(conn, entity_id)
        if entity is None:
            print(f"  [SKIP] '{entity_id}' not found in entities table")
            continue

        current_type = entity["entity_type"]
        if current_type == correct_type:
            print(f"  [SKIP] '{entity_id}' already type='{correct_type}'")
            continue

        if current_type != "unknown":
            print(
                f"  [SKIP] '{entity_id}' has type='{current_type}' "
                f"(not 'unknown') -- skip to avoid overwriting typed entity"
            )
            continue

        edges = get_edge_count(conn, entity_id)

        if dry_run:
            print(
                f"  [DRY-RUN] RETYPE '{entity_id}': "
                f"unknown -> {correct_type}  edges={edges}  ({rationale})"
            )
        else:
            conn.execute(
                "UPDATE entities SET entity_type = ?, updated_at = ? WHERE id = ?",
                (correct_type, now_utc().isoformat(), entity_id),
            )
            conn.commit()
            print(
                f"  [DONE] RETYPED '{entity_id}': "
                f"unknown -> {correct_type}  edges={edges}"
            )

        retypes_applied += 1

    return retypes_applied


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def print_type_breakdown(conn: sqlite3.Connection, label: str):
    print(f"\n  Entity type breakdown ({label}):")
    rows = conn.execute(
        "SELECT entity_type, COUNT(*) as cnt FROM entities GROUP BY entity_type ORDER BY cnt DESC"
    ).fetchall()
    total = sum(r["cnt"] for r in rows)
    for row in rows:
        print(f"    {row['entity_type']:15s}  {row['cnt']:5d}")
    print(f"    {'TOTAL':15s}  {total:5d}")


def print_deferral_report():
    print("""
  Deferrals (intentional — spec rules applied):
    1. Singular/plural pairs (e.g. 'tool' vs 'tools'):
       Spec says: do NOT auto-merge singular/plural.
       Action: flagged, deferred for manual review.

    2. Near-duplicates (Levenshtein 1-2, names >6 chars):
       Spec says: do NOT auto-merge near-duplicates.
       Action: deferred for manual review.

    3. Sentence-fragment 'unknown' entities
       (e.g. 'the sidelines', 'institutional scale', 'being committed to the repo'):
       These are NLP extraction noise — not entities that correspond to any typed concept.
       Merging is not applicable; these should be cleaned up separately (cleanup_ner_false_positives.py).
       Action: deferred.

    4. Prefix-match false positives
       (e.g. 'postgresql_instead_of_direct_nt8' has 'postgresql' as prefix):
       These are phrase fragments, not duplicates of the postgresql entity.
       Action: deferred (prefix matching is not a valid merge rule per spec).

    5. Self-referential aliases (alias == canonical_id):
       230 aliases found where alias = canonical_id.
       Pass A2 was supposed to clean these; they persist as a no-op (resolve_entity
       short-circuits on exact id match before alias lookup, so they cause no harm).
       Action: regression note filed; cleanup deferred to Pass A2 owner.
""")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cortex knowledge graph entity deduplication (Pass Q)"
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        default=False,
        help="Actually execute merges (default: dry-run only)",
    )
    parser.add_argument(
        "--retype",
        action="store_true",
        default=False,
        help="Apply the curated retype pass (fix entity_type for well-known unknowns)",
    )
    parser.add_argument(
        "--show-candidates",
        action="store_true",
        default=False,
        help="Show first 20 merge candidates and exit",
    )
    args = parser.parse_args()

    dry_run = not args.no_dry_run

    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = get_conn(DB_PATH)

    print(f"\n{'='*60}")
    print(f"  Cortex Knowledge Graph — Entity Deduplication (Pass Q)")
    print(f"  Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
    print(f"  DB: {DB_PATH}")
    print(f"{'='*60}\n")

    # ── Pre-run breakdown ──
    print_type_breakdown(conn, "before")

    # ── Self-referential alias check (regression guard) ──
    self_ref = conn.execute(
        "SELECT COUNT(*) as c FROM aliases WHERE alias = canonical_id"
    ).fetchone()["c"]
    print(f"\n  Self-referential aliases (alias == canonical_id): {self_ref}")
    if self_ref > 0:
        print(
            f"  NOTE: {self_ref} self-referential aliases detected. "
            "Pass A2 was supposed to remove these. They are harmless (resolve_entity "
            "short-circuits on exact id match) but should be cleaned up by Pass A2 owner."
        )

    # ── Strict-rule merge candidates ──
    print("\n  --- Strict-Rule Merge Candidates ---")
    print("  Rule: unknown entity whose display_name matches a typed entity's display_name")
    print("  (case-insensitive). Expected count: 0 (normalization invariant).")

    candidates = find_strict_merge_candidates(conn)
    actionable = [c for c in candidates if c["skip_reason"] is None]
    skipped    = [c for c in candidates if c["skip_reason"] is not None]

    print(f"\n  Total candidates found: {len(candidates)}")
    print(f"  Actionable (pass all safety gates): {len(actionable)}")
    print(f"  Skipped (safety gates): {len(skipped)}")

    if args.show_candidates:
        print("\n  First 20 merge candidates:")
        for c in candidates[:20]:
            print(
                f"    {'[SKIP] ' + c['skip_reason'] if c['skip_reason'] else '[OK]  '} "
                f"{c['loser_id']} ({c['loser_type']}) -> {c['winner_id']} ({c['winner_type']}) "
                f"edges={c['loser_edges']}"
            )
        return

    # ── Execute merges ──
    merged = 0
    edges_rewritten = 0
    aliases_created = 0

    for candidate in actionable:
        loser_edges = candidate["loser_edges"]
        ok = execute_merge(conn, candidate, dry_run=dry_run)
        if ok:
            merged += 1
            if not dry_run:
                edges_rewritten += loser_edges * 2  # source + target updates
                aliases_created += 1

    print(f"\n  Merges applied: {merged}")
    if not dry_run:
        print(f"  Edge rows rewritten: {edges_rewritten}")
        print(f"  Aliases created: {aliases_created}")

    # ── Retype pass ──
    if args.retype or dry_run:
        action_label = "DRY-RUN" if dry_run else "LIVE"
        print(f"\n  --- Retype Pass ({action_label}) ---")
        print(f"  Candidates in curated list: {len(RETYPE_CANDIDATES)}")
        retypes = execute_retype(conn, dry_run=dry_run)
        print(f"  Retypes {'would be applied' if dry_run else 'applied'}: {retypes}")

    # ── Post-run breakdown ──
    if not dry_run:
        print_type_breakdown(conn, "after")

    # ── Deferral report ──
    print_deferral_report()

    print(f"\n  Summary:")
    print(f"    Strict-rule merge candidates: {len(candidates)}")
    print(f"    Actionable merges: {len(actionable)}")
    print(f"    Merges {'would be executed' if dry_run else 'executed'}: {merged}")
    print(f"    Retype candidates in curated list: {len(RETYPE_CANDIDATES)}")
    print(f"    Self-referential aliases: {self_ref} (regression from Pass A2)")
    print(f"\n{'='*60}\n")

    conn.close()


if __name__ == "__main__":
    main()
