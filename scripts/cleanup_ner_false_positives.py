#!/usr/bin/env python3
"""Pass H cleanup: reclassify @mention false-positive entities in cortex-knowledge-graph.db.

Two categories of entities were incorrectly typed as 'person' by the old @mention
handler in NERExtractor._extract_people (before Pass H):

1. Bot handles: handles whose names contain "bot" as a suffix, prefix, or versioned
   component (e.g. @BotFather, @MartyProBot, @MartyProBot_2026_03_28_23).
   Correct type: 'tool'

2. Brand/company handles: known brand social-media handles
   (e.g. @YoungMoneyInvestments, @MrTopStep, @apextraderfunding).
   Correct type: 'company'

Hard limits enforced:
- Entities with >0 edges are RECLASSIFIED (not deleted).
- Isolated entities (0 edges) that are clear false positives are DELETED.
- 'unknown' entities are NOT touched (they come from kg.add_relationship auto-creation,
  not from the NER regex bugs being fixed here).

Re-running this script is a no-op (idempotent): already-reclassified entities
satisfy the WHERE conditions, so UPDATE/DELETE affects 0 rows on second run.
"""

import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / "clawd" / "data" / "cortex-knowledge-graph.db"


# ── Classification helpers (mirrors NERExtractor._is_bot_handle) ────────────

def _is_bot_handle(entity_id: str) -> bool:
    """Return True if the entity ID looks like a bot/automation handle."""
    ml = entity_id.lower()
    if ml.startswith("bot"):
        return True
    if ml.endswith("bot"):
        return True
    # Versioned bot: "bot" followed by underscore or digit
    if re.search(r"bot[_\d]", ml):
        return True
    return False


# Canonical BRAND_HANDLES set (must match NERExtractor.BRAND_HANDLES exactly)
BRAND_HANDLES: set[str] = {
    "youngmoneyinvestments", "youngmoneytrades",
    "pennyteetrading", "mrtopstep", "thefuturesdesk",
    "moneylikesmike", "alexgonzaleztrades", "blacklinetrading",
    "intuitmachine",
    "camibuffett",
    "apextraderfunding", "auth0spajs",
    "algorithmictradingstrategies",
}


def _edge_count(conn: sqlite3.Connection, entity_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM relationships WHERE source=? OR target=?",
        (entity_id, entity_id),
    ).fetchone()
    return row[0] if row else 0


def run(db_path: Path = DB_PATH, dry_run: bool = False) -> dict:
    """Execute the cleanup pass.  Returns a summary dict."""
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Fetch all current 'person' entities
    rows = conn.execute(
        "SELECT id, display_name FROM entities WHERE entity_type='person'"
    ).fetchall()

    stats = {
        "total_person_entities": len(rows),
        "bot_reclassified_to_tool": 0,
        "bot_deleted_isolated": 0,
        "brand_reclassified_to_company": 0,
        "brand_deleted_isolated": 0,
        "dry_run": dry_run,
    }

    bot_reclassify = []
    bot_delete = []
    brand_reclassify = []
    brand_delete = []

    for row in rows:
        eid = row["id"]
        edges = _edge_count(conn, eid)

        if _is_bot_handle(eid):
            if edges > 0:
                bot_reclassify.append(eid)
            else:
                bot_delete.append(eid)
        elif eid in BRAND_HANDLES:
            if edges > 0:
                brand_reclassify.append(eid)
            else:
                brand_delete.append(eid)

    print(f"Pass H cleanup {'(DRY RUN) ' if dry_run else ''}on {db_path}")
    print(f"  Total 'person' entities scanned: {stats['total_person_entities']}")
    print(f"  Bot handles to reclassify (tool, has edges): {len(bot_reclassify)}")
    print(f"  Bot handles to delete (no edges): {len(bot_delete)}")
    print(f"  Brand handles to reclassify (company, has edges): {len(brand_reclassify)}")
    print(f"  Brand handles to delete (no edges): {len(brand_delete)}")

    if not dry_run:
        for eid in bot_reclassify:
            conn.execute(
                "UPDATE entities SET entity_type='tool', updated_at=datetime('now') WHERE id=?",
                (eid,),
            )
        for eid in bot_delete:
            conn.execute("DELETE FROM entities WHERE id=?", (eid,))
        for eid in brand_reclassify:
            conn.execute(
                "UPDATE entities SET entity_type='company', updated_at=datetime('now') WHERE id=?",
                (eid,),
            )
        for eid in brand_delete:
            conn.execute("DELETE FROM entities WHERE id=?", (eid,))
        conn.commit()
        print("  Committed.")
    else:
        print("  (No changes made — dry run)")

    conn.close()

    stats["bot_reclassified_to_tool"] = len(bot_reclassify)
    stats["bot_deleted_isolated"] = len(bot_delete)
    stats["brand_reclassified_to_company"] = len(brand_reclassify)
    stats["brand_deleted_isolated"] = len(brand_delete)
    return stats


def _verify_idempotent(db_path: Path = DB_PATH) -> None:
    """Run a second pass and assert that 0 rows would be changed."""
    stats = run(db_path=db_path, dry_run=True)
    total_would_change = (
        stats["bot_reclassified_to_tool"]
        + stats["bot_deleted_isolated"]
        + stats["brand_reclassified_to_company"]
        + stats["brand_deleted_isolated"]
    )
    if total_would_change == 0:
        print("  Idempotency check PASSED: second run would affect 0 rows.")
    else:
        print(
            f"  Idempotency check FAILED: second run would still change "
            f"{total_would_change} rows.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without touching the DB")
    parser.add_argument("--verify-idempotent", action="store_true", help="Run then re-run to verify 0 rows change on second pass")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to cortex-knowledge-graph.db")
    args = parser.parse_args()

    run(db_path=args.db, dry_run=args.dry_run)

    if args.verify_idempotent and not args.dry_run:
        print("\nVerifying idempotency...")
        _verify_idempotent(db_path=args.db)
