#!/usr/bin/env python3
"""Max drawdown + benchmark comparison.

Builds equity curves for the refined rules (S1 short_ES 4h, S2 short_NQ 4h,
L1 long_AH_VIXlow, L2 long 1d) and compares to:
  - Buy-and-hold SPY (start→end of sample)
  - DCA SPY (equal-dollar purchase on each day a rule fires, hold to end)

Treats each trade as a 1-unit return on the underlying futures price (the
cumulative equity is cumprod(1 + signed_return) — i.e., single-contract
perpetual-roll series, no leverage scaling, no compounding beyond the
underlying's own move).

Reports over the full Jan 2026 → Apr 2026 window.
"""
from __future__ import annotations

import json
import statistics
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


def fmt_pct(x): return f"{x*100:+.2f}%"


def preload_min(conn, symbols, start, end):
    out = {s: [] for s in symbols}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, time, close FROM core.market_data
            WHERE symbol = ANY(%(s)s) AND bar_size='1 min'
              AND time BETWEEN %(a)s AND %(b)s
            ORDER BY symbol, time
        """, {"s": symbols, "a": start, "b": end})
        for sym, t, c in cur.fetchall():
            out[sym].append((t, float(c)))
    return out


def preload_daily(conn, symbols):
    out = {s: [] for s in symbols}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, time::date, close FROM core.market_data
            WHERE symbol = ANY(%(s)s) AND bar_size='1 day'
            ORDER BY symbol, time
        """, {"s": symbols})
        for sym, d, c in cur.fetchall():
            out[sym].append((d, float(c)))
    return out


def bar_strictly_after(bars, ts):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] <= ts: lo = mid + 1
        else: hi = mid
    if lo >= len(bars): return None, None
    return bars[lo][1], bars[lo][0]


def bar_at_or_after(bars, ts):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] < ts: lo = mid + 1
        else: hi = mid
    if lo >= len(bars): return None, None
    return bars[lo][1], bars[lo][0]


def daily_before_or_at(daily, d):
    lo, hi = 0, len(daily)
    while lo < hi:
        mid = (lo + hi) // 2
        if daily[mid][0] <= d: lo = mid + 1
        else: hi = mid
    if lo == 0: return None
    return daily[lo - 1][1]


def sma50(daily, d):
    closes = [c for (dd, c) in daily if dd <= d]
    if len(closes) < 50: return None
    return sum(closes[-50:]) / 50


def vix_on_or_before(daily, d):
    return daily_before_or_at(daily, d)


def equity_stats(returns, trade_count):
    """Given a list of per-trade returns in entry order, compute equity curve stats."""
    if not returns:
        return None
    eq = [1.0]
    peak = 1.0
    max_dd = 0.0
    max_dd_peak = None; max_dd_trough = None
    for r in returns:
        eq.append(eq[-1] * (1 + r))
        if eq[-1] > peak:
            peak = eq[-1]
        dd = (eq[-1] - peak) / peak
        if dd < max_dd:
            max_dd = dd; max_dd_peak = peak; max_dd_trough = eq[-1]
    total_ret = eq[-1] - 1
    wins = sum(1 for r in returns if r > 0)
    wr = wins / len(returns)
    mean = statistics.mean(returns)
    std = statistics.stdev(returns) if len(returns) > 1 else 0
    return {
        "n": trade_count, "wr": wr, "total": total_ret,
        "mean": mean, "std": std,
        "max_dd": max_dd, "final_eq": eq[-1],
        "peak_before_dd": max_dd_peak, "trough": max_dd_trough,
    }


def build_trades(alerts, bars, rule_fn, hold_min):
    """For each alert passing rule_fn(a), compute (entry_ts, net_return_after_cost)."""
    trades = []
    for a in sorted(alerts, key=lambda x: x["ts"]):
        if not rule_fn(a): continue
        ts = datetime.fromisoformat(a["ts"]).replace(tzinfo=CT)
        sym = a["symbol"]
        sign = 1.0 if a["direction"] == "long" else -1.0
        entry_px, entry_ts = bar_strictly_after(bars[sym], ts)
        if entry_px is None: continue
        exit_px, _ = bar_at_or_after(bars[sym], entry_ts + timedelta(minutes=hold_min))
        if exit_px is None: continue
        ret = sign * (exit_px - entry_px) / entry_px - COST_RT
        trades.append((entry_ts, ret))
    return trades


def run():
    alerts = [json.loads(l) for l in ALERTS.read_text().splitlines() if l.strip()]
    # dedupe per (direction, symbol) within 15 min
    ymi = [a for a in alerts if a["strategy"] == "YMI_2.0"
           and a["direction"] in ("long", "short")
           and a["symbol"] in ("ES", "NQ")]
    ymi.sort(key=lambda a: a["ts"])
    last = {}; kept = []
    for a in ymi:
        key = (a["direction"], a["symbol"])
        ts = datetime.fromisoformat(a["ts"])
        if key in last and (ts - last[key]).total_seconds() < 900: continue
        kept.append(a); last[key] = ts

    start = datetime.fromisoformat(kept[0]["ts"]) - timedelta(days=2)
    end = datetime.fromisoformat(kept[-1]["ts"]) + timedelta(days=2)
    start = start.replace(tzinfo=CT); end = end.replace(tzinfo=CT)

    conn = psycopg2.connect(**DB); conn.autocommit = True
    bars = preload_min(conn, ["ES", "NQ", "SPY"], start, end)
    daily = preload_daily(conn, ["SPY", "VIX", "ES"])
    conn.close()

    # --- Define rules ---
    def vix_at_alert(a): return vix_on_or_before(daily["VIX"], datetime.fromisoformat(a["ts"]).date())
    def is_ah(a): return not (8 <= datetime.fromisoformat(a["ts"]).hour < 15)

    rules = {
        "S1 short_ES 4h":      (lambda a: a["direction"]=="short" and a["symbol"]=="ES", 240),
        "S2 short_NQ 4h":      (lambda a: a["direction"]=="short" and a["symbol"]=="NQ", 240),
        "S3 short ES+NQ 4h":   (lambda a: a["direction"]=="short" and a["symbol"] in ("ES","NQ"), 240),
        "S4 short ES+NQ 1d":   (lambda a: a["direction"]=="short" and a["symbol"] in ("ES","NQ"), 1440),
        "L1 long AH VIX<18 4h": (lambda a: a["direction"]=="long" and is_ah(a) and (vix_at_alert(a) or 99) < 18, 240),
        "L2 long ES+NQ 1d":    (lambda a: a["direction"]=="long" and a["symbol"] in ("ES","NQ"), 1440),
    }

    # Build trades
    all_trades = {}
    for name, (fn, hold) in rules.items():
        trades = build_trades(kept, bars, fn, hold)
        all_trades[name] = trades

    # Combined = S3 + L1 (take every short 4h + only AH low-VIX longs)
    combined = sorted(all_trades["S3 short ES+NQ 4h"] + all_trades["L1 long AH VIX<18 4h"],
                      key=lambda x: x[0])
    all_trades["COMBINED (S3 + L1)"] = combined

    # --- Benchmark: buy-and-hold SPY from sample start to sample end ---
    spy_days = daily["SPY"]
    spy_start_price = daily_before_or_at(spy_days, start.date() + timedelta(days=3))
    spy_end_price   = daily_before_or_at(spy_days, end.date())
    bh_ret = (spy_end_price - spy_start_price) / spy_start_price if spy_start_price else 0

    # SPY buy-hold max drawdown over the same window
    in_window = [c for (d, c) in spy_days if (start.date() + timedelta(days=3)) <= d <= end.date()]
    peak = -1; dd_bh = 0
    for c in in_window:
        if c > peak: peak = c
        if peak > 0:
            dd_bh = min(dd_bh, (c - peak) / peak)
    spy_days_count = len(in_window)

    # DCA SPY: one buy per distinct day any trade fired, equal $ each
    # Compute return = avg of (spy_end - spy_buy_day) / spy_buy_day over all distinct buy days
    trade_dates = sorted({t[0].date() for t in combined})
    if trade_dates:
        pnl = []
        for d in trade_dates:
            px_buy = daily_before_or_at(spy_days, d)
            if px_buy and spy_end_price:
                pnl.append((spy_end_price - px_buy) / px_buy)
        dca_ret = statistics.mean(pnl) if pnl else 0
    else:
        dca_ret = 0

    # --- Report ---
    print("=" * 88)
    print(f"SAMPLE WINDOW:  {start.date() + timedelta(days=3)} -> {end.date()}  "
          f"({(end - start).days - 4} calendar days)")
    print("=" * 88)

    print(f"\nBENCHMARK SPY:")
    print(f"  Buy-and-hold SPY:   total={fmt_pct(bh_ret):>8}   "
          f"max_dd={fmt_pct(dd_bh):>8}   ({spy_days_count} trading days)")
    print(f"  DCA SPY (equal-$ on each signal day): total={fmt_pct(dca_ret):>8}   "
          f"(buys on {len(trade_dates)} distinct days)")

    print(f"\n{'RULE':<28} {'n':>4} {'WR':>6} {'total':>10} {'mean':>9} {'max_DD':>9} {'return/DD':>10}")
    print("-" * 88)

    for name in ["S1 short_ES 4h", "S2 short_NQ 4h", "S3 short ES+NQ 4h",
                 "S4 short ES+NQ 1d", "L1 long AH VIX<18 4h", "L2 long ES+NQ 1d",
                 "COMBINED (S3 + L1)"]:
        trades = all_trades[name]
        rets = [t[1] for t in trades]
        s = equity_stats(rets, len(rets))
        if not s: continue
        retdd = abs(s["total"] / s["max_dd"]) if s["max_dd"] else float("inf")
        print(f"{name:<28} {s['n']:>4} {s['wr']:>5.1%} "
              f"{fmt_pct(s['total']):>10} {fmt_pct(s['mean']):>9} "
              f"{fmt_pct(s['max_dd']):>9} {retdd:>9.2f}x")

    # Time-based comparison: show COMBINED equity curve by month + SPY benchmark
    print(f"\n{'='*88}\nCUMULATIVE RETURN OVER TIME (single ES/NQ contract perpetual-roll equiv):")
    print(f"{'date':>12} {'combined':>11} {'SPY_BH':>11} {'outperf':>11}")
    print("-" * 50)
    combined_trades = all_trades["COMBINED (S3 + L1)"]
    eq = 1.0
    monthly_checkpoints = {}
    for ts, ret in combined_trades:
        eq *= (1 + ret)
        monthly_checkpoints[ts.date().isoformat()[:7]] = (ts.date(), eq)

    for month, (d, equity) in sorted(monthly_checkpoints.items()):
        spy_px = daily_before_or_at(spy_days, d)
        spy_cum = (spy_px - spy_start_price) / spy_start_price if spy_start_price else 0
        strat_cum = equity - 1
        print(f"{d.isoformat():>12} {fmt_pct(strat_cum):>11} {fmt_pct(spy_cum):>11} "
              f"{fmt_pct(strat_cum - spy_cum):>11}")

    print(f"\nNOTE: Strategy return is per-trade compounded (single contract). No leverage sizing;")
    print(f"no slippage beyond 0.5bp round-trip; no overnight-gap handling beyond the 1d exit.")


if __name__ == "__main__":
    run()
