#!/usr/bin/env python3
"""
Fill Recent Gaps
================
Fills forward-in-time gaps in core.market_data by fetching 1-min bars
from NT8 day-by-day, starting from each symbol's newest existing bar.

Unlike smart_backfill.py (which fills backward from oldest), this script
detects that recent data is missing and fetches forward to today.

Usage:
    python fill_recent_gaps.py --symbols NG GC MGC ZC ZW MCL MBT
    python fill_recent_gaps.py --symbols CL --from-date 2026-05-19
    python fill_recent_gaps.py --symbols NG --contract "NG 06-26"
"""

import os
import sys
import json
import socket
import time
import logging
import argparse
import psycopg2
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fill_recent_gaps")

# -- Config -------------------------------------------------------------------
NT8_HOST = os.getenv("NT8_HOST", "100.107.193.101")
NT8_PORT = int(os.getenv("NT8_PORT", "49999"))

DB_HOST = os.getenv("POSTGRES_HOST", "100.67.112.3")
DB_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
DB_NAME = os.getenv("POSTGRES_DB", "tradingcore")
DB_USER = os.getenv("POSTGRES_USER", "trading_user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

SOCKET_TIMEOUT = 30   # seconds per NT8 request
BARS_PER_DAY   = 1500 # 1 day of 1-min bars; NT8 times out on larger windows

# Current front-month contract per symbol (update after each roll)
CONTRACT_MAP = {
    "ES":  "ES 06-26",  "NQ":  "NQ 06-26",  "YM":  "YM 06-26",
    "RTY": "RTY 06-26", "MES": "MES 06-26",  "MNQ": "MNQ 06-26",
    "MYM": "MYM 06-26", "M2K": "M2K 06-26",
    "GC":  "GC 08-26",  "MGC": "MGC 08-26",
    "SI":  "SI 07-26",
    "CL":  "CL 07-26",  "MCL": "MCL 07-26",
    "NG":  "NG 07-26",
    "ZC":  "ZC 07-26",  "ZW":  "ZW 07-26",  "ZS": "ZS 07-26",
    "ZN":  "ZN 06-26",  "ZB":  "ZB 06-26",
    "6E":  "6E 06-26",  "HG":  "HG 07-26",
    "MBT": "MBT 06-26",
}

# Per-symbol secondary contract for older gaps
# If primary contract returns no bars for a day, try this one
SECONDARY_CONTRACT = {
    "MCL": "MCL 07-26",  # July MCL for post-May-21 data
    "NG":  "NG 07-26",   # July NG after June expires May 29
}


def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
        connect_timeout=15,
        options="-c statement_timeout=0",  # disable: peer-parity trigger on GC->MGC is slow
    )


def nt8_request(payload: dict) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT)
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
            stripped = buf.strip()
            if stripped.endswith(b"}") or stripped.endswith(b"]"):
                try:
                    return json.loads(stripped.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
        return json.loads(buf.strip().decode("utf-8", errors="replace"))
    finally:
        sock.close()


def fetch_day(nt8_symbol: str, day: date) -> list:
    """Fetch 1-min bars for one calendar day from NT8."""
    payload = {
        "command": "GET_HISTORICAL_BARS",
        "symbol": nt8_symbol,
        "bars": BARS_PER_DAY,
        "period": "minute",
        "bar_period": 1,
        "from_date": day.strftime("%Y-%m-%d"),
        "to_date": day.strftime("%Y-%m-%d"),
    }
    try:
        resp = nt8_request(payload)
        if resp.get("status") != "OK":
            return []
        return resp.get("data", [])
    except Exception as exc:
        log.warning("  NT8 error for %s on %s: %s", nt8_symbol, day, exc)
        return []


def insert_bars(bars: list, symbol: str) -> int:
    """Insert bars via SECURITY DEFINER function that bypasses peer-parity triggers."""
    if not bars:
        return 0
    payload = []
    for b in bars:
        try:
            payload.append({
                "time":   b["time"],
                "symbol": symbol,
                "open":   float(b["open"]),
                "high":   float(b["high"]),
                "low":    float(b["low"]),
                "close":  float(b["close"]),
                "volume": int(b.get("volume", 0)),
            })
        except (KeyError, ValueError):
            continue
    if not payload:
        return 0
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT core.bulk_insert_market_data_no_triggers(%s)",
                (json.dumps(payload),),
            )
            result = cur.fetchone()
        conn.commit()
    return result[0] if result else 0


def get_newest_bar(symbol: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(time)::date
                FROM core.market_data
                WHERE symbol = %s AND bar_size = '1 min'
                """,
                (symbol,),
            )
            row = cur.fetchone()
    return row[0] if row and row[0] else None


def refresh_cagg() -> None:
    log.info("Refreshing market_data_5min cagg (last 90 days)...")
    conn = get_db()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        start = datetime.now() - timedelta(days=90)
        cur.execute(
            "CALL refresh_continuous_aggregate('market_data_5min', %s, %s)",
            (start, datetime.now()),
        )
        log.info("Cagg refresh complete")
    except Exception as exc:
        log.warning("Cagg refresh failed: %s", exc)
    finally:
        conn.close()


def fill_symbol(symbol: str, nt8_symbol: str, from_date: date) -> int:
    today = date.today()
    total_inserted = 0
    secondary = SECONDARY_CONTRACT.get(symbol)
    current = from_date

    while current <= today:
        bars = fetch_day(nt8_symbol, current)

        # Try secondary contract if primary returned nothing
        if not bars and secondary:
            bars = fetch_day(secondary, current)
            if bars:
                log.info("  %s %s: used secondary contract %s (%d bars)",
                         symbol, current, secondary, len(bars))

        if bars:
            inserted = insert_bars(bars, symbol)
            total_inserted += inserted
            log.info("  %s %s: fetched=%d inserted=%d", symbol, current, len(bars), inserted)
        else:
            log.debug("  %s %s: no bars (non-trading day or contract gap)", symbol, current)

        current += timedelta(days=1)
        time.sleep(0.3)  # be gentle on NT8

    return total_inserted


def main():
    parser = argparse.ArgumentParser(description="Fill forward gaps in core.market_data from NT8")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--from-date", default=None,
                        help="Override start date YYYY-MM-DD (default: DB newest bar + 1 day)")
    parser.add_argument("--contract", default=None,
                        help="Override NT8 contract for all symbols")
    args = parser.parse_args()

    results = {}
    for sym in args.symbols:
        nt8_sym = args.contract or CONTRACT_MAP.get(sym, sym)

        if args.from_date:
            start = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        else:
            newest = get_newest_bar(sym)
            if newest is None:
                log.warning("%s: no existing bars, use smart_backfill.py instead", sym)
                continue
            start = newest + timedelta(days=1)
            if start > date.today():
                log.info("%s: already current (newest=%s)", sym, newest)
                continue

        log.info("%s (%s): filling %s -> %s", sym, nt8_sym, start, date.today())
        inserted = fill_symbol(sym, nt8_sym, start)
        results[sym] = inserted
        log.info("%s: total inserted = %d", sym, inserted)
        time.sleep(1)

    print("\n=== FILL SUMMARY ===")
    total = sum(results.values())
    for sym, n in results.items():
        print(f"  {sym}: {n:,} rows inserted")
    print(f"\nTotal: {total:,} rows")

    if total > 0:
        refresh_cagg()

    return 0


if __name__ == "__main__":
    sys.exit(main())
