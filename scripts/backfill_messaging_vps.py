#!/usr/bin/env python3
"""
Incremental pgvector backfill for public.messaging_messages on Storage VPS.

Adds 384-dim sentence-transformers/all-MiniLM-L6-v2 embeddings to every row
where content is non-empty and embedding IS NULL.  Safe to interrupt and re-run:
rows already embedded are skipped (WHERE embedding IS NULL).

Usage:
    python3 backfill_messaging_vps.py --dry-run
    python3 backfill_messaging_vps.py --max-rows 1000
    python3 backfill_messaging_vps.py --max-rows 100000
    python3 backfill_messaging_vps.py                     # full backfill

Credentials:
    Set PGPASSWORD env var (or rely on ~/.pgpass).
    DB host/port/dbname default to Storage VPS values; override via env.

Pass 7 of Cameron's adversarial improvement loop — 2026-04-16.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backfill_messaging_vps")

# ---------------------------------------------------------------------------
# Config — read from env, never hardcoded
# ---------------------------------------------------------------------------

DB_HOST = os.environ.get("PGHOST", "100.67.112.3")
DB_PORT = int(os.environ.get("PGPORT", "5432"))
DB_NAME = os.environ.get("PGDATABASE", "cami_memory")
DB_USER = os.environ.get("PGUSER", "trading_user")
# PGPASSWORD is picked up automatically by psycopg2 / libpq

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_DIM = 384

BATCH_SIZE = 500       # rows fetched + updated per outer commit
ENCODE_BATCH = 64      # sentences per forward pass through the model
PROGRESS_EVERY = 1000  # print progress line every N rows embedded
DEFAULT_MAX_ROWS = 1000  # conservative default; pass None for unlimited


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def connect() -> psycopg2.extensions.connection:
    """Open psycopg2 connection using env/libpq credentials."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            # password read from PGPASSWORD env or ~/.pgpass
            connect_timeout=10,
        )
        conn.autocommit = False
        log.info("Connected to %s:%s/%s as %s", DB_HOST, DB_PORT, DB_NAME, DB_USER)
        return conn
    except psycopg2.Error as exc:
        log.error("DB connection failed: %s", exc)
        sys.exit(1)


def count_pending(conn: psycopg2.extensions.connection) -> int:
    """Return number of embeddable rows not yet embedded."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.messaging_messages
            WHERE content IS NOT NULL
              AND length(trim(content)) > 0
              AND embedding IS NULL
            """
        )
        return cur.fetchone()[0]


def count_total(conn: psycopg2.extensions.connection) -> int:
    """Return total rows and rows with non-empty content."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*),
                   COUNT(CASE WHEN content IS NOT NULL AND length(trim(content)) > 0 THEN 1 END)
            FROM public.messaging_messages
            """
        )
        return cur.fetchone()


def fetch_batch(
    conn: psycopg2.extensions.connection,
    batch_size: int,
    offset: int,
    max_id_seen: int | None,
) -> list[tuple[int, str]]:
    """
    Fetch up to batch_size (id, content) rows where embedding IS NULL.
    Uses id > max_id_seen for stable cursor-style pagination without an actual
    server-side cursor (reconnect-safe).
    """
    with conn.cursor() as cur:
        if max_id_seen is None:
            cur.execute(
                """
                SELECT id, content
                FROM public.messaging_messages
                WHERE content IS NOT NULL
                  AND length(trim(content)) > 0
                  AND embedding IS NULL
                ORDER BY id
                LIMIT %s
                """,
                (batch_size,),
            )
        else:
            cur.execute(
                """
                SELECT id, content
                FROM public.messaging_messages
                WHERE id > %s
                  AND content IS NOT NULL
                  AND length(trim(content)) > 0
                  AND embedding IS NULL
                ORDER BY id
                LIMIT %s
                """,
                (max_id_seen, batch_size),
            )
        return cur.fetchall()


def write_embeddings(
    conn: psycopg2.extensions.connection,
    id_vector_pairs: list[tuple[int, list[float]]],
) -> None:
    """
    UPDATE embedding for each row, then commit.
    Uses execute_batch for efficiency (~N round-trips reduced).
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE public.messaging_messages SET embedding = %s::vector WHERE id = %s",
            [(str(vec), row_id) for row_id, vec in id_vector_pairs],
            page_size=200,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Model loader (lazy singleton)
# ---------------------------------------------------------------------------

_model = None


def get_model():
    global _model
    if _model is None:
        log.info("Loading sentence-transformers model: %s", EMBED_MODEL_NAME)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            log.error(
                "sentence-transformers not installed. "
                "Run: pip install sentence-transformers"
            )
            sys.exit(1)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _model = SentenceTransformer(EMBED_MODEL_NAME)
        # Verify dimensions
        test_vec = _model.encode(["dim check"], show_progress_bar=False)
        actual_dim = len(test_vec[0])
        if actual_dim != EMBED_DIM:
            log.error(
                "Model returned %d-dim vectors; expected %d. "
                "Check EMBED_MODEL_NAME config.",
                actual_dim, EMBED_DIM,
            )
            sys.exit(1)
        log.info("Model ready — %d dims confirmed", EMBED_DIM)
    return _model


# ---------------------------------------------------------------------------
# Core backfill
# ---------------------------------------------------------------------------

def run_backfill(
    conn: psycopg2.extensions.connection,
    max_rows: int | None,
    dry_run: bool,
) -> dict:
    """
    Main backfill loop.  Fetches rows in batches, encodes content, writes embeddings.

    Returns stats dict: {embedded, skipped, failed, elapsed_seconds}.
    """
    pending = count_pending(conn)
    total_rows, embeddable_rows = count_total(conn)

    log.info(
        "Table state: %d total rows, %d with non-empty content, %d pending embedding",
        total_rows, embeddable_rows, pending,
    )

    effective_limit = pending if max_rows is None else min(max_rows, pending)

    if dry_run:
        # Estimate time: ~1ms per row at batch encode rates (64-row batches, CPU)
        est_seconds = effective_limit * 0.002  # conservative 2ms/row
        log.info(
            "[DRY RUN] Would embed up to %d rows (of %d pending).",
            effective_limit, pending,
        )
        log.info(
            "[DRY RUN] Estimated time at 2ms/row: %.1f minutes (%.0f seconds).",
            est_seconds / 60, est_seconds,
        )
        return {
            "dry_run": True,
            "pending": pending,
            "effective_limit": effective_limit,
            "estimated_seconds": est_seconds,
        }

    if pending == 0:
        log.info("No rows pending embedding. Nothing to do.")
        return {"embedded": 0, "skipped": 0, "failed": 0, "elapsed_seconds": 0.0}

    model = get_model()

    embedded = 0
    skipped = 0
    failed = 0
    max_id_seen: int | None = None
    start_time = time.monotonic()
    last_progress_at = 0

    log.info("Starting backfill (max_rows=%s)...", max_rows if max_rows else "unlimited")

    while embedded < effective_limit:
        remaining = effective_limit - embedded
        fetch_n = min(BATCH_SIZE, remaining)

        rows = fetch_batch(conn, fetch_n, 0, max_id_seen)
        if not rows:
            log.info("No more embeddable rows found. Backfill complete.")
            break

        # Skip rows with truly empty content (double-check)
        valid_rows = []
        for row_id, content in rows:
            if not content or not content.strip():
                skipped += 1
                continue
            valid_rows.append((row_id, content.strip()))

        if valid_rows:
            ids = [r[0] for r in valid_rows]
            texts = [r[1] for r in valid_rows]
            max_id_seen = ids[-1]

            # Encode in sub-batches for memory efficiency
            import warnings
            vectors_flat: list[list[float]] = []
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    vecs = model.encode(
                        texts,
                        batch_size=ENCODE_BATCH,
                        show_progress_bar=False,
                        convert_to_numpy=True,
                    )
                for vec in vecs:
                    vectors_flat.append(vec.tolist())
            except Exception as exc:
                log.warning(
                    "Encode failed for batch ending at id=%d: %s — skipping %d rows",
                    max_id_seen, exc, len(texts),
                )
                failed += len(texts)
                continue

            # Write to DB
            try:
                write_embeddings(conn, list(zip(ids, vectors_flat)))
                embedded += len(ids)
            except Exception as exc:
                log.warning(
                    "Write failed for batch ending at id=%d: %s — rolling back",
                    max_id_seen, exc,
                )
                conn.rollback()
                failed += len(ids)
                continue
        else:
            # All rows in this batch were empty — advance max_id_seen
            if rows:
                max_id_seen = rows[-1][0]

        # Progress report
        if embedded - last_progress_at >= PROGRESS_EVERY:
            elapsed = time.monotonic() - start_time
            rate = embedded / elapsed if elapsed > 0 else 0
            remaining_count = effective_limit - embedded
            eta_s = remaining_count / rate if rate > 0 else float("inf")
            log.info(
                "Progress: %d/%d embedded | %d failed | %d skipped | "
                "%.1f rows/s | ETA %.1f min",
                embedded, effective_limit, failed, skipped,
                rate, eta_s / 60,
            )
            last_progress_at = embedded

    elapsed = time.monotonic() - start_time
    rate = embedded / elapsed if elapsed > 0 else 0

    log.info(
        "Backfill done: %d embedded, %d skipped, %d failed in %.1fs (%.1f rows/s)",
        embedded, skipped, failed, elapsed, rate,
    )
    return {
        "embedded": embedded,
        "skipped": skipped,
        "failed": failed,
        "elapsed_seconds": elapsed,
        "rows_per_second": rate,
    }


# ---------------------------------------------------------------------------
# Post-backfill helpers
# ---------------------------------------------------------------------------

def create_index(conn: psycopg2.extensions.connection) -> None:
    """
    Create ivfflat index for cosine similarity search.
    Run AFTER backfill so IVFFlat can see enough vectors to partition.
    lists=200 is safe for ~120K rows (sqrt(120K) ~ 346; 200 is acceptable).

    Opens a fresh connection with autocommit=True because CREATE INDEX
    cannot run inside an explicit transaction block.
    """
    log.info("Creating ivfflat index on messaging_messages.embedding ...")
    try:
        idx_conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            connect_timeout=10,
        )
        idx_conn.autocommit = True
        with idx_conn.cursor() as cur:
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS messaging_messages_embedding_ivfflat_idx
                ON public.messaging_messages
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 200)
                """
            )
        idx_conn.close()
        log.info("Index created (or already exists).")
    except psycopg2.Error as exc:
        log.error("Index creation failed: %s", exc)


def coverage_report(conn: psycopg2.extensions.connection) -> None:
    """Print embedding coverage stats."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*)                                          AS total,
                COUNT(embedding)                                  AS embedded,
                COUNT(*) - COUNT(embedding)                       AS remaining,
                ROUND(COUNT(embedding)::numeric / NULLIF(COUNT(*),0) * 100, 2) AS pct
            FROM public.messaging_messages
            """
        )
        total, embedded, remaining, pct = cur.fetchone()
    log.info(
        "Coverage: %d/%d embedded (%.2f%%), %d remaining",
        embedded, total, float(pct or 0), remaining,
    )


def smoke_test(conn: psycopg2.extensions.connection) -> None:
    """Embed a sample query and return top-5 nearest neighbours."""
    log.info("Running smoke test: 'trading plan for today'")
    model = get_model()
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        qvec = model.encode(["trading plan for today"], show_progress_bar=False)[0].tolist()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,
                   SUBSTRING(content, 1, 100) AS preview,
                   embedding <=> %s::vector   AS dist
            FROM public.messaging_messages
            WHERE embedding IS NOT NULL
            ORDER BY dist
            LIMIT 5
            """,
            (str(qvec),),
        )
        rows = cur.fetchall()

    if not rows:
        log.warning("Smoke test: no embedded rows found — backfill may not have run.")
        return

    log.info("=== Smoke test top-5 nearest to 'trading plan for today' ===")
    for rank, (row_id, preview, dist) in enumerate(rows, 1):
        log.info("  #%d  id=%-8d  dist=%.4f  %r", rank, row_id, dist, preview)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incremental pgvector backfill for public.messaging_messages"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report pending row count and estimated time; do not mutate.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=(
            f"Maximum rows to embed this run (default: {DEFAULT_MAX_ROWS}). "
            "Set to 0 for unlimited."
        ),
    )
    parser.add_argument(
        "--create-index",
        action="store_true",
        help="Create ivfflat index after backfill (only useful post-bulk-load).",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run similarity smoke test after backfill.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Print coverage stats and exit without embedding.",
    )
    args = parser.parse_args()

    max_rows: int | None = None if args.max_rows == 0 else args.max_rows

    conn = connect()

    try:
        if args.coverage:
            coverage_report(conn)
            return

        stats = run_backfill(conn, max_rows=max_rows, dry_run=args.dry_run)

        if not args.dry_run:
            coverage_report(conn)

            if args.create_index:
                create_index(conn)

            if args.smoke_test:
                smoke_test(conn)

            # Summary
            log.info(
                "Pass 7 summary: embedded=%d skipped=%d failed=%d elapsed=%.1fs",
                stats.get("embedded", 0),
                stats.get("skipped", 0),
                stats.get("failed", 0),
                stats.get("elapsed_seconds", 0.0),
            )

    except KeyboardInterrupt:
        log.warning("Interrupted — progress committed up to last batch boundary.")
        conn.rollback()
    finally:
        conn.close()
        log.info("Connection closed.")


if __name__ == "__main__":
    main()
