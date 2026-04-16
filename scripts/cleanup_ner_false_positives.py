#!/usr/bin/env python3
"""Cleanup scripts for NER false-positive entities in cortex-knowledge-graph.db.

--- Pass H cleanup ---
Reclassify @mention false-positive entities.

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

--- Pass CC cleanup (--clean-fragments) ---
Remove NLP extraction noise from type='unknown' entities.

The NER pipeline auto-creates entities for any noun phrase encountered.  Many of
those entities are sentence fragments ("the end of every day", "result is unknown",
"approach that could") rather than real named entities (brands, products, tools).

Heuristics used (all criteria must match to qualify for action):
  - entity_type = 'unknown'
  - display_name contains a space (multi-word) OR display_name starts with a
    stoplist word (a / an / the / and / or / in / to / of / for / is / was / ...)
  - edge_count determines action:
      edge_count = 0  -> DELETE  (orphan fragment, no connections)
      edge_count = 1  -> DELETE  (weakly linked, single relationship, likely noise)
      edge_count >= 2 -> RECLASSIFY to 'unknown_noisy'
                         (someone referenced it; tag for filtering but keep)

New entity type introduced: 'unknown_noisy'
  Meaning: entity was created by NLP extraction but its display_name matches
  sentence-fragment heuristics.  It has 2+ edges, so it is preserved for
  referential integrity.  Downstream queries should treat unknown_noisy the same
  as unknown unless they are explicitly looking for NLP noise.

  This type is idempotent: re-running --clean-fragments will skip entities already
  tagged unknown_noisy because the WHERE clause selects entity_type='unknown' only.
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


# ── Pass CC: fragment cleanup ────────────────────────────────────────────────

# Words that, when they appear at the start of a display_name, indicate the
# entity is a sentence fragment rather than a named entity.
FRAGMENT_STOPLIST: frozenset[str] = frozenset({
    "a", "an", "the",
    "and", "or", "but", "nor", "so", "yet",
    "in", "to", "of", "for", "on", "at", "by", "as", "up",
    "is", "was", "are", "were", "be", "been", "being",
    "has", "have", "had",
    "if", "it", "its",
    "that", "this", "these", "those",
    "when", "where", "which", "who", "how", "what",
    "all", "any", "each", "every", "both", "few", "more", "most",
    "can", "may", "will", "would", "should", "could", "must",
    "just", "not", "no", "only", "also",
    "do", "does", "did", "done",
    "from", "with", "into", "about", "than", "then",
})


def _is_fragment(display_name: str) -> bool:
    """Return True if display_name looks like a sentence fragment, not a real entity.

    Criteria (OR):
    - Contains a space (multi-word phrase that was auto-extracted)
    - First word is in FRAGMENT_STOPLIST (single-word entity starting with grammar word)
    """
    if not display_name:
        return True
    first_word = display_name.split()[0].lower().strip(".,;:!?\"'()")
    if first_word in FRAGMENT_STOPLIST:
        return True
    if " " in display_name:
        return True
    return False


def _categorize_fragment(display_name: str) -> str:
    """Return a human-readable category label for dry-run output."""
    if not display_name:
        return "empty_display_name"
    first_word = display_name.split()[0].lower().strip(".,;:!?\"'()")
    has_space = " " in display_name
    if first_word in FRAGMENT_STOPLIST and has_space:
        return "stoplist_prefix_with_space"
    if first_word in FRAGMENT_STOPLIST:
        return "stoplist_prefix_no_space"
    if has_space and display_name.lower() == display_name:
        return "all_lowercase_with_space"
    if has_space:
        return "mixed_case_with_space"
    return "other"


def clean_fragments(
    db_path: Path = DB_PATH,
    dry_run: bool = False,
    sample_size: int = 30,
) -> dict:
    """Pass CC: delete or reclassify NLP sentence-fragment entities.

    Heuristics (all criteria must match to qualify):
      - entity_type = 'unknown'
      - display_name contains a space OR starts with a stoplist word
      - edge_count < 2  -> DELETE
      - edge_count >= 2 -> RECLASSIFY to 'unknown_noisy'

    Returns a summary dict.  Idempotent: re-running is a no-op because
    deleted rows are gone and reclassified rows have entity_type='unknown_noisy'
    which is not selected by this pass.
    """
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, display_name FROM entities WHERE entity_type='unknown'"
    ).fetchall()

    # Tally category counts for reporting
    category_counts: dict[str, int] = {}
    to_delete: list[str] = []
    to_reclassify: list[str] = []

    for row in rows:
        eid = row["id"]
        dn = row["display_name"] or ""
        if not _is_fragment(dn):
            continue
        cat = _categorize_fragment(dn)
        category_counts[cat] = category_counts.get(cat, 0) + 1
        edges = _edge_count(conn, eid)
        if edges < 2:
            to_delete.append((eid, dn, edges, cat))
        else:
            to_reclassify.append((eid, dn, edges, cat))

    total_unknowns = len(rows)
    total_fragment_candidates = len(to_delete) + len(to_reclassify)

    print(f"\nPass CC --clean-fragments {'(DRY RUN) ' if dry_run else ''}on {db_path}")
    print(f"  Total 'unknown' entities scanned:    {total_unknowns}")
    print(f"  Fragment candidates:                 {total_fragment_candidates}")
    print(f"    -> to DELETE  (edge_count < 2):    {len(to_delete)}")
    print(f"    -> to RECLASSIFY (edge_count >= 2): {len(to_reclassify)}")
    print()
    print("  Category breakdown (fragment candidates):")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat:40s} {count}")

    # Print a sample of up to sample_size entries from the deletion list
    sample = to_delete[:sample_size]
    print(f"\n  Sample of up to {sample_size} entities to DELETE:")
    print(f"  {'ID':<50} {'DISPLAY NAME':<55} {'EDGES'} {'CATEGORY'}")
    print(f"  {'-'*50} {'-'*55} {'-----'} {'--------'}")
    for eid, dn, edges, cat in sample:
        print(f"  {eid[:50]:<50} {dn[:55]:<55} {edges:5d} {cat}")

    if not dry_run:
        with conn:
            for eid, dn, edges, cat in to_delete:
                conn.execute("DELETE FROM entities WHERE id=? AND entity_type='unknown'", (eid,))
            for eid, dn, edges, cat in to_reclassify:
                conn.execute(
                    "UPDATE entities SET entity_type='unknown_noisy', updated_at=datetime('now') "
                    "WHERE id=? AND entity_type='unknown'",
                    (eid,),
                )
        print(f"\n  Committed: {len(to_delete)} deleted, {len(to_reclassify)} reclassified to unknown_noisy.")

        # Post-run counts
        remaining_unknown = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='unknown'"
        ).fetchone()[0]
        total_entities = conn.execute(
            "SELECT COUNT(*) FROM entities"
        ).fetchone()[0]
        print(f"  Post-run unknown count:  {remaining_unknown}")
        print(f"  Post-run total entities: {total_entities}")
    else:
        print("\n  (No changes made -- dry run)")

    conn.close()

    return {
        "dry_run": dry_run,
        "total_unknowns_scanned": total_unknowns,
        "fragment_candidates": total_fragment_candidates,
        "deleted": len(to_delete),
        "reclassified_to_unknown_noisy": len(to_reclassify),
        "category_counts": category_counts,
    }


def _verify_idempotent_fragments(db_path: Path = DB_PATH) -> None:
    """Re-run --clean-fragments in dry-run mode and assert 0 rows would change."""
    stats = clean_fragments(db_path=db_path, dry_run=True, sample_size=0)
    total_would_change = stats["deleted"] + stats["reclassified_to_unknown_noisy"]
    if total_would_change == 0:
        print("  Fragment idempotency check PASSED: second run would affect 0 rows.")
    else:
        print(
            f"  Fragment idempotency check FAILED: second run would still change "
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
    parser.add_argument(
        "--clean-fragments",
        action="store_true",
        help=(
            "Pass CC: delete/reclassify type=unknown sentence-fragment entities. "
            "Entities with edge_count < 2 are deleted; edge_count >= 2 are reclassified "
            "to unknown_noisy. Backup is created at <db>.bak-passCC before any writes."
        ),
    )
    args = parser.parse_args()

    if args.clean_fragments:
        if not args.dry_run:
            bak = Path(str(args.db) + ".bak-passCC")
            if not bak.exists():
                import shutil
                print(f"Creating backup: {bak}")
                shutil.copy2(args.db, bak)
            else:
                print(f"Backup already exists at {bak}, skipping copy.")
        clean_fragments(db_path=args.db, dry_run=args.dry_run)
        if args.verify_idempotent and not args.dry_run:
            print("\nVerifying fragment cleanup idempotency...")
            _verify_idempotent_fragments(db_path=args.db)
    else:
        run(db_path=args.db, dry_run=args.dry_run)
        if args.verify_idempotent and not args.dry_run:
            print("\nVerifying idempotency...")
            _verify_idempotent(db_path=args.db)
