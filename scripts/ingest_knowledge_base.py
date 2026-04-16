#!/usr/bin/env python3
"""
Pass 9 — Ingest clawd knowledge-base.db into cortex-vectors.db.

Source: ~/clawd/db/knowledge-base.db  (read-only)
Target: ~/.cortex/data/cortex-vectors.db  (canonical, via unified_vector_store)

Tables with embed-worthy text:
  - content_posts   (16 rows): Cameron's published trading content
  - twitter_mentions (9 rows): Twitter replies mentioning @KingCam23

Tables skipped (empty, metadata-only, or binary):
  - sources, chunks, strategies, source_links  -> 0 rows
  - content_metrics, topic_performance         -> 0 rows (metadata only)
  - embedding BLOB in chunks                   -> binary, never embed

Idempotency key: doc_id = "kb-{table}-{pk}"
  _upsert uses ON CONFLICT(id) DO UPDATE, so re-runs produce 0 new rows.
  text_hash dedup also prevents content-level duplicates.

PII check: No email, phone, or API-key patterns found in either table.
"""

import argparse
import hashlib
import logging
import sqlite3
import sys
from pathlib import Path

# Allow importing from src/ when running from scripts/ or repo root
_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO / "src"))

from unified_vector_store import get_vector_store, DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kb-ingest")

KB_PATH = Path("~/clawd/db/knowledge-base.db").expanduser()
# Never write to the retired path
RETIRED_PATH = Path("~/clawd/data/cortex-vectors.db").expanduser()


def _check_paths():
    if not KB_PATH.exists():
        raise FileNotFoundError(f"Source DB not found: {KB_PATH}")
    if DB_PATH == RETIRED_PATH:
        raise RuntimeError(
            f"Vector store resolved to retired path {RETIRED_PATH}. "
            "Set CORTEX_DATA_DIR or unset to use ~/.cortex/data."
        )
    logger.info("Source: %s", KB_PATH)
    logger.info("Target: %s", DB_PATH)


def _open_kb() -> sqlite3.Connection:
    """Open knowledge-base.db read-only."""
    conn = sqlite3.connect(f"file:{KB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_counts(conn: sqlite3.Connection) -> dict:
    tables = ["sources", "chunks", "strategies", "source_links",
              "twitter_mentions", "content_posts", "content_metrics",
              "topic_performance"]
    counts = {}
    for t in tables:
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return counts


def _content_posts_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, platform, content_text, topic, hashtags, posted_at, status "
        "FROM content_posts ORDER BY id"
    ).fetchall()


def _twitter_mention_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, tweet_id, author_handle, tweet_text, created_at "
        "FROM twitter_mentions ORDER BY id"
    ).fetchall()


def _build_content_post_doc(row: sqlite3.Row) -> tuple[str, str, dict]:
    """Return (doc_id, text, metadata) for a content_posts row."""
    doc_id = f"kb-content_posts-{row['id']}"
    parts = [row["content_text"] or ""]
    if row["topic"]:
        parts.append(f"Topic: {row['topic']}")
    if row["hashtags"]:
        parts.append(f"Hashtags: {row['hashtags']}")
    text = "\n".join(p for p in parts if p).strip()
    metadata = {
        "tags": "knowledge-base,content_posts",
        "source_table": "content_posts",
        "source_pk": row["id"],
        "platform": row["platform"] or "",
        "topic": row["topic"] or "",
        "posted_at": row["posted_at"] or "",
        "status": row["status"] or "",
    }
    return doc_id, text, metadata


def _build_twitter_mention_doc(row: sqlite3.Row) -> tuple[str, str, dict]:
    """Return (doc_id, text, metadata) for a twitter_mentions row."""
    doc_id = f"kb-twitter_mentions-{row['id']}"
    handle = row["author_handle"] or "unknown"
    tweet = row["tweet_text"] or ""
    text = f"@{handle}: {tweet}".strip()
    metadata = {
        "tags": f"knowledge-base,twitter_mentions,twitter,{handle}",
        "source_table": "twitter_mentions",
        "source_pk": row["id"],
        "tweet_id": row["tweet_id"] or "",
        "author_handle": handle,
        "created_at": row["created_at"] or "",
    }
    return doc_id, text, metadata


def dry_run(conn: sqlite3.Connection) -> tuple[list, list]:
    """Compute what would be inserted. Returns (post_docs, mention_docs)."""
    post_rows = _content_posts_rows(conn)
    mention_rows = _twitter_mention_rows(conn)

    post_docs = [_build_content_post_doc(r) for r in post_rows if (r["content_text"] or "").strip()]
    mention_docs = [_build_twitter_mention_doc(r) for r in mention_rows if (r["tweet_text"] or "").strip()]

    logger.info("--- DRY RUN ---")
    logger.info("content_posts eligible:   %d rows -> %d docs", len(post_rows), len(post_docs))
    logger.info("twitter_mentions eligible: %d rows -> %d docs", len(mention_rows), len(mention_docs))
    logger.info("Total estimated inserts:  %d", len(post_docs) + len(mention_docs))
    logger.info("Skipped tables (empty):   sources, chunks, strategies, source_links, "
                "content_metrics, topic_performance")
    return post_docs, mention_docs


def ingest(conn: sqlite3.Connection, dry: bool = False, force_replace: bool = True) -> dict:
    """Run ingestion. Returns summary dict.

    Args:
        conn: Open read-only connection to knowledge-base.db.
        dry: If True, estimate rows without writing.
        force_replace: If True (default), call delete_document_and_chunks before
            each add_knowledge so stale chunks from a previously-larger row are
            removed.  The doc IDs for this source are stable PKs
            (``kb-content_posts-{id}``, ``kb-twitter_mentions-{id}``) so
            force_replace is safe and idempotent.
    """
    post_docs, mention_docs = dry_run(conn)

    if dry:
        return {
            "dry_run": True,
            "content_posts": len(post_docs),
            "twitter_mentions": len(mention_docs),
            "total": len(post_docs) + len(mention_docs),
        }

    store = get_vector_store()
    pre_count = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    replaced_count = 0

    inserted_posts = 0
    for doc_id, text, metadata in post_docs:
        if force_replace:
            # Internal ID in documents has the kg- prefix added by add_knowledge
            n = store.delete_document_and_chunks(f"kg-{doc_id}")
            if n:
                replaced_count += 1
        store.add_knowledge(doc_id, text, metadata)
        inserted_posts += 1
        logger.debug("Upserted %s", doc_id)

    inserted_mentions = 0
    for doc_id, text, metadata in mention_docs:
        if force_replace:
            n = store.delete_document_and_chunks(f"kg-{doc_id}")
            if n:
                replaced_count += 1
        store.add_knowledge(doc_id, text, metadata)
        inserted_mentions += 1
        logger.debug("Upserted %s", doc_id)

    post_count = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    delta = post_count - pre_count

    logger.info("--- REAL RUN ---")
    logger.info("content_posts upserted:   %d", inserted_posts)
    logger.info("twitter_mentions upserted: %d", inserted_mentions)
    logger.info("Replaced (had stale chunks): %d", replaced_count)
    logger.info("DB rows before: %d  after: %d  delta: %d", pre_count, post_count, delta)

    return {
        "dry_run": False,
        "content_posts": inserted_posts,
        "twitter_mentions": inserted_mentions,
        "total_upserted": inserted_posts + inserted_mentions,
        "replaced": replaced_count,
        "rows_before": pre_count,
        "rows_after": post_count,
        "delta": delta,
    }


def smoke_test(store) -> list[dict]:
    """Run 3 smoke queries against the just-ingested content."""
    queries = [
        "backtesting overfitting bias",
        "position sizing stops risk blowup",
        "regime detection trending chop",
    ]
    hits = []
    for q in queries:
        results = store.search(q, limit=3)
        kb_hits = [r for r in results if r.get("id", "").startswith("kb-")]
        hits.append({"query": q, "total_results": len(results), "kb_hits": len(kb_hits),
                     "top_id": results[0].get("id") if results else None})
        logger.info("Smoke [%s]: %d total, %d from KB, top=%s",
                    q, len(results), len(kb_hits), results[0].get("id") if results else "none")
    return hits


def main():
    parser = argparse.ArgumentParser(description="Ingest clawd knowledge-base.db into cortex")
    parser.add_argument("--dry-run", action="store_true", help="Estimate rows without inserting")
    parser.add_argument("--no-smoke", action="store_true", help="Skip smoke test")
    parser.add_argument("--no-force-replace", action="store_true",
                        help="Disable delete-before-insert (may leave orphaned chunks)")
    parser.add_argument("--idempotency-check", action="store_true",
                        help="Run a second pass to verify 0-delta re-run")
    args = parser.parse_args()

    force_replace = not args.no_force_replace

    _check_paths()
    conn = _open_kb()

    try:
        counts = _row_counts(conn)
        logger.info("knowledge-base.db table row counts: %s", counts)

        result = ingest(conn, dry=args.dry_run, force_replace=force_replace)
        logger.info("Ingestion result: %s", result)

        if not args.dry_run and not args.no_smoke:
            store = get_vector_store()
            hits = smoke_test(store)
            result["smoke_hits"] = hits

        if args.idempotency_check and not args.dry_run:
            logger.info("--- IDEMPOTENCY CHECK (second pass) ---")
            store = get_vector_store()
            pre2 = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            # Re-run ingest with force_replace (second pass must also be clean)
            ingest(conn, dry=False, force_replace=force_replace)
            post2 = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            delta2 = post2 - pre2
            logger.info("Second pass delta: %d (must be 0)", delta2)
            result["idempotency_delta"] = delta2
            if delta2 != 0:
                logger.error("IDEMPOTENCY FAILURE: delta=%d", delta2)
                sys.exit(1)
            else:
                logger.info("Idempotency: PASS")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
