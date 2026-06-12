#!/usr/bin/env python3
"""TV alert backtest v2 — validation pass.

Adds vs v1:
  1. Strict entry: next 1-min bar STRICTLY after email ts (kills look-ahead).
  2. Commission + slippage model (0.005% round-trip per trade by default).
  3. Walk-forward: first 70% of dates = TRAIN, last 30% = TEST. Report both.
  4. Regime split:
        - VIX level  (<18 low, 18-25 mid, >25 high)
        - ES 50d MA trend  (above / below)
        - session     (RTH: 08:30-15:00 CT  vs  after-hours)
  5. Per-symbol breakdown (ES vs NQ).
  6. Trade-count independence: flag when same strategy fires same direction
     on the SAME symbol within 15 minutes (double-counting).

Read-only on core.market_data. Same signal source file as v1.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import os
from zoneinfo import ZoneInfo

import psycopg2

ALERTS = Path("/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_alerts_clean.ndjson")
def db_config():
    password = os.environ.get("TRADINGCORE_POSTGRES_PASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("Set TRADINGCORE_POSTGRES_PASSWORD or POSTGRES_PASSWORD for TradingCore.")
    return dict(host="100.67.112.3", port=5432, database="tradingcore",
                user="trading_user", password=password)


CT = ZoneInfo("America/Chicago")
COST_ROUND_TRIP = 0.00005   # 0.005% = 0.5 bp (typical futures RT w/ slippage)


def fmt_pct(x): return f"{x*100:+.3f}%"


def bar_strictly_after(cur, symbol, ts):
    """First 1-min bar with time > ts (strict). Avoids using the contemporaneous close."""
    cur.execute("""
        SELECT close, time FROM core.market_data
        WHERE symbol=%s AND bar_size='1 min' AND time > %s
        ORDER BY time LIMIT 1
    """, (symbol, ts))
    r = cur.fetchone()
    return (float(r[0]), r[1]) if r else (None, None)


def bar_at_or_after(cur, symbol, ts):
    cur.execute("""
        SELECT close, time FROM core.market_data
        WHERE symbol=%s AND bar_size='1 min' AND time >= %s
        ORDER BY time LIMIT 1
    """, (symbol, ts))
    r = cur.fetchone()
    return (float(r[0]), r[1]) if r else (None, None)


def vix_at(cur, ts):
    cur.execute("""
        SELECT close FROM core.market_data
        WHERE symbol='VIX' AND bar_size='1 day' AND time::date <= %s::date
        ORDER BY time DESC LIMIT 1
    """, (ts,))
    r = cur.fetchone()
    return float(r[0]) if r else None


def sma50_at(cur, symbol, ts):
    cur.execute("""
        WITH b AS (
            SELECT close, time FROM core.market_data
            WHERE symbol=%s AND bar_size='1 day' AND time::date <= %s::date
            ORDER BY time DESC LIMIT 50
        )
        SELECT AVG(close), (SELECT close FROM b ORDER BY time DESC LIMIT 1) FROM b
    """, (symbol, ts))
    r = cur.fetchone()
    return (float(r[0]), float(r[1])) if r and r[0] else (None, None)


def stats(rs):
    if not rs: return {}
    wins = sum(1 for r in rs if r > 0)
    return {
        "n": len(rs), "wr": wins / len(rs),
        "mean": statistics.mean(rs),
        "med": statistics.median(rs),
        "std": statistics.stdev(rs) if len(rs) > 1 else 0,
    }


def print_row(label, s, cost_adj=None):
    if not s or s["n"] == 0:
        print(f"  {label:<40} n=0"); return
    net = f"net={fmt_pct(cost_adj)}" if cost_adj is not None else ""
    print(f"  {label:<40} n={s['n']:>4} WR={s['wr']:>5.1%} "
          f"mean={fmt_pct(s['mean']):>9} med={fmt_pct(s['med']):>9} "
          f"std={fmt_pct(s['std']):>8}  {net}")


def run():
    alerts = [json.loads(l) for l in ALERTS.read_text().splitlines() if l.strip()]
    # keep only YMI_2.0 with clear direction (that's what v1 showed signal on)
    ymi = [a for a in alerts
           if a["strategy"] == "YMI_2.0" and a["direction"] in ("long", "short")
           and a["symbol"] in ("ES", "NQ")]
    print(f"YMI_2.0 ES/NQ long+short alerts: {len(ymi)}")

    # Dedupe: same strategy+direction+symbol within 15 min = double-count, keep first
    ymi.sort(key=lambda a: a["ts"])
    kept = []
    last = {}
    doubles = 0
    for a in ymi:
        key = (a["strategy"], a["direction"], a["symbol"])
        ts = datetime.fromisoformat(a["ts"])
        if key in last and (ts - last[key]).total_seconds() < 900:
            doubles += 1
            continue
        kept.append(a); last[key] = ts
    print(f"after 15-min double-count dedupe: {len(kept)}  (dropped {doubles})")

    # Split 70/30 by date
    kept.sort(key=lambda a: a["ts"])
    split_idx = int(len(kept) * 0.7)
    train = kept[:split_idx]
    test = kept[split_idx:]
    if train and test:
        print(f"train: {len(train)} ({train[0]['ts'][:10]} .. {train[-1]['ts'][:10]})")
        print(f"test : {len(test)}  ({test[0]['ts'][:10]} .. {test[-1]['ts'][:10]})")

    conn = psycopg2.connect(**db_config()); conn.autocommit = True
    cur = conn.cursor()

    holds_min = [15, 30, 60, 240, 1440]

    # results[set][strategy+dir+symbol][hold] = list[ret]
    results = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    # regime buckets
    regime = defaultdict(lambda: defaultdict(list))

    skipped = 0
    for split_name, bucket in [("ALL", kept), ("TRAIN", train), ("TEST", test)]:
        for a in bucket:
            ts = datetime.fromisoformat(a["ts"]).replace(tzinfo=CT)
            sym = a["symbol"]
            sign = 1.0 if a["direction"] == "long" else -1.0

            # Strict entry: first bar STRICTLY after email ts
            entry_px, entry_ts = bar_strictly_after(cur, sym, ts)
            if entry_px is None:
                skipped += 1; continue

            for h in holds_min:
                exit_ts = entry_ts + timedelta(minutes=h)
                exit_px, _ = bar_at_or_after(cur, sym, exit_ts)
                if exit_px is None: continue
                raw = sign * (exit_px - entry_px) / entry_px
                net = raw - COST_ROUND_TRIP
                key = f"{a['direction']}_{sym}"
                results[split_name][key][h].append(raw)

                # Regime tag (only compute once per alert, not per hold)
                if split_name == "ALL" and h == 240:
                    vx = vix_at(cur, ts)
                    sma50, last_close = sma50_at(cur, "ES", ts)
                    if vx is not None:
                        if vx < 18: vix_bucket = "vix<18"
                        elif vx <= 25: vix_bucket = "vix18-25"
                        else: vix_bucket = "vix>25"
                    else: vix_bucket = "vix?"
                    if sma50 and last_close:
                        trend = "above50" if last_close > sma50 else "below50"
                    else: trend = "?"

                    # Session
                    local_h = ts.hour
                    if 8 <= local_h < 15: session = "RTH"
                    else: session = "AH"

                    regime[f"{a['direction']}_{sym}_vix"][vix_bucket].append(raw)
                    regime[f"{a['direction']}_{sym}_trend"][trend].append(raw)
                    regime[f"{a['direction']}_{sym}_session"][session].append(raw)

    # ---- Report ----
    print(f"\nskipped (no bar): {skipped}")

    print("\n" + "=" * 92)
    print("PER-SPLIT BACKTEST (strict entry, commission-adjusted)")
    print("=" * 92)
    for split_name in ("ALL", "TRAIN", "TEST"):
        print(f"\n--- {split_name} ---")
        for key in sorted(results[split_name]):
            for h in holds_min:
                rs = results[split_name][key][h]
                if len(rs) < 5: continue
                s = stats(rs)
                net_mean = s["mean"] - COST_ROUND_TRIP
                label = f"{key}  hold={h}m" if h < 1440 else f"{key}  hold=1d"
                print_row(label, s, cost_adj=net_mean)

    print("\n" + "=" * 92)
    print("REGIME SEGMENTATION (4h hold only, ALL data)")
    print("=" * 92)
    for key in sorted(regime):
        print(f"\n{key}:")
        for sub, rs in regime[key].items():
            if len(rs) < 5: continue
            s = stats(rs)
            print_row(f"  {sub}", s, cost_adj=s["mean"] - COST_ROUND_TRIP)

    conn.close()


if __name__ == "__main__":
    run()
