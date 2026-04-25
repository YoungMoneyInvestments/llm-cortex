#!/usr/bin/env python3
"""TV alert backtest v3 — batched preload (fast) + full validation."""
from __future__ import annotations

import bisect
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2

ALERTS = Path("/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_alerts_clean.ndjson")
DB = dict(host="100.67.112.3", port=5432, database="tradingcore",
          user="trading_user", password="TradingCore2025!")

CT = ZoneInfo("America/Chicago")
COST_RT = 0.00005


def fmt_pct(x): return f"{x*100:+.3f}%"


def preload(conn, symbols, start, end):
    """Return dict {sym: [(ts, close)]} sorted by ts."""
    out = {s: [] for s in symbols}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, time, close
            FROM core.market_data
            WHERE symbol = ANY(%(syms)s)
              AND bar_size='1 min'
              AND time BETWEEN %(start)s AND %(end)s
            ORDER BY symbol, time
        """, {"syms": symbols, "start": start, "end": end})
        for sym, ts, close in cur.fetchall():
            out[sym].append((ts, float(close)))
    print(f"preloaded 1-min bars: {[(s, len(out[s])) for s in symbols]}", file=sys.stderr)
    return out


def preload_daily(conn, symbols):
    out = {s: [] for s in symbols}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, time::date, close
            FROM core.market_data
            WHERE symbol = ANY(%(syms)s) AND bar_size='1 day'
            ORDER BY symbol, time
        """, {"syms": symbols})
        for sym, d, close in cur.fetchall():
            out[sym].append((d, float(close)))
    return out


def bar_strictly_after(bars, ts):
    """bars = [(dt, close)] sorted.  Return first with dt > ts."""
    if not bars: return None, None
    # bisect on timestamps
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] <= ts: lo = mid + 1
        else: hi = mid
    if lo >= len(bars): return None, None
    return bars[lo][1], bars[lo][0]


def bar_at_or_after(bars, ts):
    if not bars: return None, None
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] < ts: lo = mid + 1
        else: hi = mid
    if lo >= len(bars): return None, None
    return bars[lo][1], bars[lo][0]


def daily_before(daily_bars, d):
    """Last daily close at or before date d.  daily_bars = [(date, close)]."""
    if not daily_bars: return None
    lo, hi = 0, len(daily_bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if daily_bars[mid][0] <= d: lo = mid + 1
        else: hi = mid
    if lo == 0: return None
    return daily_bars[lo - 1][1]


def sma50(daily_bars, d):
    """50-day SMA of closes at-or-before date d. Returns (sma, last_close)."""
    if not daily_bars: return None, None
    closes = [c for (dd, c) in daily_bars if dd <= d]
    if len(closes) < 50: return None, closes[-1] if closes else None
    return sum(closes[-50:]) / 50, closes[-1]


def stats(rs):
    if not rs: return {}
    wins = sum(1 for r in rs if r > 0)
    return {"n": len(rs), "wr": wins / len(rs),
            "mean": statistics.mean(rs), "med": statistics.median(rs),
            "std": statistics.stdev(rs) if len(rs) > 1 else 0}


def print_row(label, s, net=None):
    if not s or s["n"] == 0:
        print(f"  {label:<40} n=0"); return
    net_str = f"  net={fmt_pct(net)}" if net is not None else ""
    print(f"  {label:<40} n={s['n']:>4} WR={s['wr']:>5.1%} "
          f"mean={fmt_pct(s['mean']):>9} med={fmt_pct(s['med']):>9}{net_str}")


def run():
    alerts = [json.loads(l) for l in ALERTS.read_text().splitlines() if l.strip()]
    ymi = [a for a in alerts
           if a["strategy"] == "YMI_2.0" and a["direction"] in ("long", "short")
           and a["symbol"] in ("ES", "NQ")]
    ymi.sort(key=lambda a: a["ts"])

    # 15-min dedupe
    last = {}
    kept = []
    for a in ymi:
        key = (a["direction"], a["symbol"])
        ts = datetime.fromisoformat(a["ts"])
        if key in last and (ts - last[key]).total_seconds() < 900:
            continue
        kept.append(a); last[key] = ts
    print(f"YMI alerts kept after dedupe: {len(kept)}")

    if not kept:
        print("nothing to test"); return

    start = datetime.fromisoformat(kept[0]["ts"]) - timedelta(days=2)
    end = datetime.fromisoformat(kept[-1]["ts"]) + timedelta(days=2)
    start = start.replace(tzinfo=CT); end = end.replace(tzinfo=CT)

    conn = psycopg2.connect(**DB); conn.autocommit = True
    bars = preload(conn, ["ES", "NQ", "SPY"], start, end)
    daily = preload_daily(conn, ["ES", "VIX"])
    conn.close()

    holds_min = [15, 30, 60, 240, 1440]

    # split 70/30
    split_idx = int(len(kept) * 0.7)
    train, test = kept[:split_idx], kept[split_idx:]
    print(f"train:{len(train)} ({train[0]['ts'][:10]}..{train[-1]['ts'][:10]})")
    print(f"test :{len(test)} ({test[0]['ts'][:10]}..{test[-1]['ts'][:10]})")

    # results[split][key][hold] = list
    results = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    regime = defaultdict(lambda: defaultdict(list))

    for split_name, bucket in [("ALL", kept), ("TRAIN", train), ("TEST", test)]:
        for a in bucket:
            ts = datetime.fromisoformat(a["ts"]).replace(tzinfo=CT)
            sym = a["symbol"]
            sign = 1.0 if a["direction"] == "long" else -1.0
            sym_bars = bars.get(sym)
            entry_px, entry_ts = bar_strictly_after(sym_bars, ts)
            if entry_px is None: continue

            for h in holds_min:
                exit_target = entry_ts + timedelta(minutes=h)
                exit_px, _ = bar_at_or_after(sym_bars, exit_target)
                if exit_px is None: continue
                raw = sign * (exit_px - entry_px) / entry_px
                key = f"{a['direction']}_{sym}"
                results[split_name][key][h].append(raw)

                if split_name == "ALL" and h == 240:
                    # regime at ts
                    d = ts.date()
                    vx = daily_before(daily["VIX"], d)
                    sma_val, last_close = sma50(daily["ES"], d)
                    if vx is None: vix_bucket = "vix?"
                    elif vx < 18: vix_bucket = "vix<18"
                    elif vx <= 25: vix_bucket = "vix18-25"
                    else: vix_bucket = "vix>25"
                    if sma_val and last_close:
                        trend = "ES>50d" if last_close > sma_val else "ES<50d"
                    else: trend = "?"
                    session = "RTH" if 8 <= ts.hour < 15 else "AH"
                    regime[f"{key}_vix"][vix_bucket].append(raw)
                    regime[f"{key}_trend"][trend].append(raw)
                    regime[f"{key}_session"][session].append(raw)

    # Report
    print("\n" + "=" * 92)
    print("WALK-FORWARD (strict entry, costs subtracted in net)")
    print("=" * 92)
    for sp in ("ALL", "TRAIN", "TEST"):
        print(f"\n--- {sp} ---")
        for k in sorted(results[sp]):
            for h in holds_min:
                rs = results[sp][k][h]
                if len(rs) < 5: continue
                s = stats(rs)
                lbl = f"{k}  hold={h}m" if h < 1440 else f"{k}  hold=1d"
                print_row(lbl, s, net=s["mean"] - COST_RT)

    print("\n" + "=" * 92)
    print("REGIME SPLIT (4h hold, ALL)")
    print("=" * 92)
    for key in sorted(regime):
        print(f"\n{key}:")
        items = list(regime[key].items())
        items.sort()
        for sub, rs in items:
            if len(rs) < 5: continue
            s = stats(rs)
            print_row(f"  {sub}", s, net=s["mean"] - COST_RT)


if __name__ == "__main__":
    run()
