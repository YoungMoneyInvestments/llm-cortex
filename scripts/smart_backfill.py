#!/usr/bin/env python3
"""
Smart Chunked Backfill Utility
================================
Intelligently fills historical bar data gaps in the DB by:
1. Detecting exactly what's missing per symbol/bar_size
2. Pulling from NT8 SocketServerMCP in date-sliced chunks
3. Walking backward in time until target history depth is reached
4. Idempotent — safe to re-run, ON CONFLICT DO NOTHING

Usage:
    python smart_backfill.py                        # All KPL instruments, 1 year
    python smart_backfill.py --symbols ES NQ GC     # Specific instruments
    python smart_backfill.py --days 730             # 2 years back
    python smart_backfill.py --bar-size "5 min"     # Specific bar size
    python smart_backfill.py --check-only           # Report gaps without filling
"""

import os
import sys
import json
import socket
import time
import argparse
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timedelta, date, timezone
from pathlib import Path
import logging

# ── Config ──────────────────────────────────────────────────────────────────
NT8_HOST = os.getenv("NT8_HOST", "100.107.193.101")
NT8_PORT = int(os.getenv("NT8_PORT", "49999"))

DB_HOST = os.getenv("POSTGRES_HOST", "100.67.112.3")
DB_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
DB_NAME = os.getenv("POSTGRES_DB", "tradingcore")
DB_USER = os.getenv("POSTGRES_USER", "trading_user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

# KPL instruments Tyler posts daily
KPL_INSTRUMENTS = ["ES", "NQ", "YM", "RTY", "GC", "SI", "CL", "NG",
                   "ZC", "ZW", "ZS", "ZN", "ZB", "6E"]

# Ralph trading instruments
RALPH_INSTRUMENTS = ["MES", "MNQ", "MYM", "M2K", "MGC", "MCL", "VX"]

ALL_INSTRUMENTS = KPL_INSTRUMENTS + RALPH_INSTRUMENTS

# Current front-month contract map (update at each roll)
CONTRACT_MAP = {
    "ES": "ES 03-26",  "NQ": "NQ 03-26",  "YM": "YM 03-26",
    "RTY": "RTY 03-26", "MES": "MES 03-26", "MNQ": "MNQ 03-26",
    "MYM": "MYM 03-26", "M2K": "M2K 03-26",
    "GC": "GC 04-26",  "MGC": "MGC 04-26", "SI": "SI 05-26",
    "CL": "CL 04-26",  "MCL": "MCL 04-26",
    "NG": "NG 04-26",  "ZC": "ZC 03-26",   "ZW": "ZW 03-26",
    "ZS": "ZS 03-26",  "ZN": "ZN 03-26",   "ZB": "ZB 03-26",
    "6E": "6E 03-26",  "VX": "VX 03-26",
}

BARS_PER_CHUNK = 50000   # Max bars per NT8 request
SOCKET_TIMEOUT = 120     # Seconds to wait for NT8 response

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smart_backfill")

# ── NT8 Socket ──────────────────────────────────────────────────────────────
def nt8_request(payload: dict, timeout=SOCKET_TIMEOUT) -> dict:
    """Send a request to NT8 SocketServerMCP and return parsed JSON response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((NT8_HOST, NT8_PORT))
        msg = json.dumps(payload) + "\n"
        sock.sendall(msg.encode())

        buf = b""
        while True:
            chunk = sock.recv(131072)
            if not chunk:
                break
            buf += chunk
            # NT8 sends complete JSON objects terminated with newline or closing brace
            stripped = buf.strip()
            if stripped.endswith(b"}") or stripped.endswith(b"]"):
                try:
                    return json.loads(stripped.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue  # keep reading — incomplete JSON
        return json.loads(buf.strip().decode("utf-8", errors="replace"))
    finally:
        sock.close()


def fetch_bars_chunk(symbol: str, bar_size: str,
                     from_date: datetime, to_date: datetime,
                     max_bars: int = BARS_PER_CHUNK) -> list:
    """Fetch a single chunk of bars from NT8."""
    period_map = {
        "1 min": "minute", "1 mins": "minute",
        "5 min": "minute", "5 mins": "minute",
        "15 min": "minute", "15 mins": "minute",
        "30 min": "minute", "30 mins": "minute",
        "daily": "day"
    }
    period_type = period_map.get(bar_size, "minute")
    bar_period = int(bar_size.split()[0]) if "min" in bar_size else 1

    payload = {
        "command": "GET_HISTORICAL_BARS",
        "symbol": symbol,
        "bars": max_bars,
        "period": period_type,
        "bar_period": bar_period,
        "from_date": from_date.strftime("%Y-%m-%d"),
        "to_date": to_date.strftime("%Y-%m-%d"),
    }

    resp = nt8_request(payload)
    if resp.get("status") != "OK":
        log.error(f"NT8 error for {symbol}: {resp.get('error', resp)}")
        return []

    return resp.get("data", [])


# ── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASS,
                            connect_timeout=15,
                            options="-c statement_timeout=60000")


def get_gap_info(symbol: str, bar_size: str, target_days_back: int) -> dict:
    """
    Find gaps in DB for this symbol/bar_size.
    Returns: oldest_bar, newest_bar, total_bars, gap_days
    """
    target_start = datetime.now() - timedelta(days=target_days_back)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MIN(time), MAX(time), COUNT(*)
                FROM core.market_data
                WHERE symbol = %s AND bar_size = %s AND source = 'nt8'
                  AND time >= %s
            """, (symbol, bar_size, target_start))
            row = cur.fetchone()

    oldest, newest, count = row
    # Normalize to offset-naive UTC for comparison
    if oldest is not None and hasattr(oldest, 'tzinfo') and oldest.tzinfo is not None:
        oldest = oldest.replace(tzinfo=None)
    if hasattr(target_start, 'tzinfo') and target_start.tzinfo is not None:
        target_start = target_start.replace(tzinfo=None)
    gap_start = target_start if oldest is None else min(oldest, target_start)
    gap_days = (datetime.now() - gap_start).days if oldest else target_days_back

    return {
        "symbol": symbol,
        "bar_size": bar_size,
        "oldest_in_db": oldest,
        "newest_in_db": newest,
        "bars_in_db": count or 0,
        "target_start": target_start,
        "needs_backfill": oldest is None or oldest.date() > target_start.date() + timedelta(days=2),
        "gap_days": gap_days,
    }


def insert_bars(bars: list, symbol: str, bar_size: str) -> int:
    """Insert bars into DB. Returns number of new rows inserted."""
    if not bars:
        return 0

    rows = []
    for b in bars:
        try:
            rows.append((
                b["time"], symbol, bar_size,
                float(b["open"]), float(b["high"]),
                float(b["low"]), float(b["close"]),
                int(b.get("volume", 0)),
                None,  # vwap
                None,  # trades
                None,  # contract_id
                "nt8", "futures"
            ))
        except (KeyError, ValueError):
            continue

    if not rows:
        return 0

    sql = """
        INSERT INTO core.market_data
            (time, symbol, bar_size, open, high, low, close,
             volume, vwap, trades, contract_id, source, asset_class)
        VALUES %s
        ON CONFLICT DO NOTHING
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()

    return len(rows)


# ── Main backfill logic ──────────────────────────────────────────────────────
def backfill_symbol(symbol: str, nt8_symbol: str, bar_size: str,
                    target_days: int, dry_run: bool = False) -> dict:
    """
    Backfill a single symbol by pulling in chunks walking backward from today.
    Returns stats dict.
    """
    log.info(f"{'[DRY RUN] ' if dry_run else ''}Backfilling {symbol} ({bar_size}), {target_days} days")

    gap = get_gap_info(symbol, bar_size, target_days)
    log.info(f"  DB: {gap['bars_in_db']:,} bars | oldest={gap['oldest_in_db']} | "
             f"needs_backfill={gap['needs_backfill']}")

    if not gap["needs_backfill"]:
        log.info(f"  {symbol}: already has full history, skipping")
        return {"symbol": symbol, "status": "already_complete", "inserted": 0}

    if dry_run:
        log.info(f"  [DRY RUN] Would pull ~{gap['gap_days']} days of {bar_size} bars")
        return {"symbol": symbol, "status": "dry_run", "gap_days": gap["gap_days"]}

    # Pull in time-sliced chunks working backward from today
    total_inserted = 0
    chunk_size_days = 10  # Pull 10 days at a time to keep payloads manageable
    end_date = datetime.now()
    start_target = gap["target_start"]

    # If we have some data, only fill the gap before oldest existing bar
    if gap["oldest_in_db"]:
        end_date = gap["oldest_in_db"]

    current_end = end_date
    while current_end > start_target:
        current_start = max(current_end - timedelta(days=chunk_size_days), start_target)

        log.info(f"  Fetching {symbol} {current_start.date()} → {current_end.date()}")

        try:
            bars = fetch_bars_chunk(nt8_symbol, bar_size, current_start, current_end)
            if bars:
                inserted = insert_bars(bars, symbol, bar_size)
                total_inserted += inserted
                log.info(f"    Got {len(bars)} bars, inserted {inserted} new")
            else:
                log.warning(f"    No bars returned for {symbol} {current_start.date()}")

            current_end = current_start - timedelta(days=1)
            time.sleep(0.5)  # Be gentle on NT8

        except Exception as e:
            log.error(f"  Error fetching {symbol} chunk: {e}")
            break

    log.info(f"  {symbol}: total inserted = {total_inserted:,}")
    return {"symbol": symbol, "status": "complete", "inserted": total_inserted}


def check_gaps(symbols: list, bar_sizes: list, target_days: int):
    """Print a gap report without fetching any data."""
    print(f"\n{'Symbol':<8} {'Bar Size':<8} {'Bars in DB':>12} {'Oldest':>12} {'Gap Days':>10} Status")
    print("-" * 65)
    for sym in symbols:
        for bs in bar_sizes:
            gap = get_gap_info(sym, bs, target_days)
            status = "NEEDS FILL" if gap["needs_backfill"] else "OK"
            oldest = gap["oldest_in_db"].strftime("%Y-%m-%d") if gap["oldest_in_db"] else "NONE"
            print(f"{sym:<8} {bs:<8} {gap['bars_in_db']:>12,} {oldest:>12} "
                  f"{gap['gap_days']:>10} {status}")


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Smart chunked backfill for NT8 bar data")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols to backfill (default: all KPL + Ralph instruments)")
    parser.add_argument("--bar-sizes", nargs="+", default=["1 min", "5 min"],
                        help="Bar sizes to backfill")
    parser.add_argument("--days", type=int, default=365,
                        help="How many calendar days back to target (default: 365)")
    parser.add_argument("--check-only", action="store_true",
                        help="Only report gaps, don't fetch data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without actually fetching")
    args = parser.parse_args()

    symbols = args.symbols or ALL_INSTRUMENTS

    if args.check_only:
        check_gaps(symbols, args.bar_sizes, args.days)
        return

    results = []
    for sym in symbols:
        nt8_sym = CONTRACT_MAP.get(sym, sym)
        for bs in args.bar_sizes:
            result = backfill_symbol(sym, nt8_sym, bs, args.days, dry_run=args.dry_run)
            results.append(result)
            time.sleep(1)

    print("\n=== BACKFILL SUMMARY ===")
    total = sum(r.get("inserted", 0) for r in results)
    for r in results:
        print(f"  {r['symbol']}: {r['status']} — {r.get('inserted', 0):,} rows inserted")
    print(f"\nTotal rows inserted: {total:,}")


if __name__ == "__main__":
    main()
