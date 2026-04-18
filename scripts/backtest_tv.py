#!/usr/bin/env python3
"""Backtest TradingView email alerts against storage VPS futures bars.

Signal source : tv_alerts_clean.ndjson  (parsed Gmail scrape)
Market data   : core.market_data @ 100.67.112.3:5432/tradingcore (1-min + daily)
Runtime       : read-only; SPY-baseline alpha; grouped by strategy and hold.

Rules we evaluate:
  - YMI_2.0 (buy|sell) on ES, NQ
  - LuxAlgo_Reversal (direction unknown → long trial)
  - VOLDQ crossing (long trial)
  - K_continuation
  - Lorentzian_short
  - S5FI_breadth (long trial)
  - SPY_put_call (short trial)
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

import psycopg2

ALERTS = Path("/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_alerts_clean.ndjson")
DB = dict(host="100.67.112.3", port=5432, database="tradingcore",
          user="trading_user", password="TradingCore2025!")


def fmt_pct(x: float) -> str:
    return f"{x*100:+.3f}%"


# Fetch 1-min close at-or-after ts; return (price, actual_ts)
def close_at_or_after(cur, symbol, ts, within_minutes=60):
    end = ts + timedelta(minutes=within_minutes)
    cur.execute("""
        SELECT close, time
        FROM core.market_data
        WHERE symbol=%s AND bar_size='1 min' AND time >= %s AND time <= %s
        ORDER BY time
        LIMIT 1
    """, (symbol, ts, end))
    r = cur.fetchone()
    return (float(r[0]), r[1]) if r else (None, None)


def close_at_after_minutes(cur, symbol, entry_ts, n_minutes):
    target = entry_ts + timedelta(minutes=n_minutes)
    cur.execute("""
        SELECT close, time
        FROM core.market_data
        WHERE symbol=%s AND bar_size='1 min' AND time >= %s
        ORDER BY time
        LIMIT 1
    """, (symbol, target))
    r = cur.fetchone()
    return (float(r[0]), r[1]) if r else (None, None)


# Map futures continuous ticker -> our market_data symbol (same)
def to_db_sym(s):
    return s  # already mapped in parser


def run() -> None:
    alerts = [json.loads(l) for l in ALERTS.read_text().splitlines() if l.strip()]
    print(f"loaded {len(alerts)} alerts")

    holds_min = [5, 15, 30, 60, 240, 1440]

    conn = psycopg2.connect(**DB)
    conn.autocommit = True

    # returns[strategy][direction][hold_min] = list of (ret, alpha, symbol, ts)
    results: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    unmapped = defaultdict(int)

    with conn.cursor() as cur:
        for i, a in enumerate(alerts):
            ts = datetime.fromisoformat(a["ts"])
            # Assume America/Chicago — the db bars are stored as timestamptz, email times are local CT
            # Gmail titles are in user's local tz; treat as naive → align to CT by tagging CT.
            from zoneinfo import ZoneInfo
            ts = ts.replace(tzinfo=ZoneInfo("America/Chicago"))

            sym = to_db_sym(a["symbol"])
            if sym not in ("ES", "NQ", "YM", "RTY", "CL", "GC",
                           "SPY", "QQQ", "VTI", "IWM", "S5FI", "PCSP", "MMFI"):
                unmapped[sym] += 1
                continue

            direction = a["direction"]
            if direction not in ("long", "short"): continue
            sign = 1.0 if direction == "long" else -1.0

            entry_px, entry_ts = close_at_or_after(cur, sym, ts)
            if entry_px is None: continue

            # SPY baseline at same ts
            spy_entry, _ = close_at_or_after(cur, "SPY", ts)

            for h in holds_min:
                exit_px, exit_ts = close_at_after_minutes(cur, sym, entry_ts, h)
                if exit_px is None: continue
                raw_ret = sign * (exit_px - entry_px) / entry_px

                alpha = None
                if spy_entry is not None:
                    spy_exit, _ = close_at_after_minutes(cur, "SPY", entry_ts, h)
                    if spy_exit is not None:
                        spy_ret = (spy_exit - spy_entry) / spy_entry
                        alpha = raw_ret - sign * spy_ret  # SPY beta adjusted by direction

                results[a["strategy"]][direction][h].append({
                    "ret": raw_ret, "alpha": alpha,
                    "symbol": sym, "ts": a["ts"],
                })

    conn.close()

    # Report
    print("\n" + "=" * 96)
    print(f"{'strategy':>20} {'dir':>6} {'hold':>6} {'n':>5} {'WR':>6} {'mean':>9} "
          f"{'med':>8} {'stdev':>8} {'alpha':>9} {'aWR':>6} {'amed':>9}")
    print("-" * 96)

    for strategy in sorted(results):
        for direction in sorted(results[strategy]):
            for h in holds_min:
                bucket = results[strategy][direction][h]
                if not bucket: continue
                rs = [r["ret"] for r in bucket]
                alphas = [r["alpha"] for r in bucket if r["alpha"] is not None]
                wins = sum(1 for r in rs if r > 0)
                mean = statistics.mean(rs)
                med = statistics.median(rs)
                std = statistics.stdev(rs) if len(rs) > 1 else 0
                amean = statistics.mean(alphas) if alphas else 0
                amed = statistics.median(alphas) if alphas else 0
                aWR = sum(1 for a in alphas if a > 0) / len(alphas) if alphas else 0
                label = f"{h}m" if h < 1440 else "1d"
                print(f"{strategy:>20} {direction:>6} {label:>6} {len(rs):>5} "
                      f"{wins/len(rs):>5.1%} {fmt_pct(mean):>8} {fmt_pct(med):>7} "
                      f"{fmt_pct(std):>7} {fmt_pct(amean):>8} {aWR:>5.1%} {fmt_pct(amed):>8}")

    # Top/bottom 5-min trades for YMI_2.0 long (sanity)
    bucket = results.get("YMI_2.0", {}).get("long", {}).get(15, [])
    if bucket:
        print("\n-- YMI_2.0 long, 15m hold: top 5 / bottom 5 --")
        sorted_b = sorted(bucket, key=lambda r: r["ret"], reverse=True)
        for r in sorted_b[:5]:
            print(f"  {r['ts']:25s} {r['symbol']:4s} ret={fmt_pct(r['ret'])}")
        print("  ---")
        for r in sorted_b[-5:]:
            print(f"  {r['ts']:25s} {r['symbol']:4s} ret={fmt_pct(r['ret'])}")

    if unmapped:
        print(f"\nunmapped symbols (skipped): {dict(unmapped)}")


if __name__ == "__main__":
    run()
