#!/usr/bin/env python3
"""YMI 2.0 backtest — aggressive fairness audit.

Runs a battery of tests designed to FAIL if the edge is fake:

  1. Realistic commission stress (0.5bp → 3bp RT)
  2. Entry-at-open instead of close-of-next-bar (a real market order)
  3. Latency shifts: 0s, 30s, 60s, 5m, 15m delays — real edge decays smooth
  4. Shuffle (permutation) test: random labels vs real labels, N=500
  5. Direction-invert test: flip shorts to longs and vice versa
  6. No-dedupe (honest rate): include every repeat fire
  7. Return distribution: mean vs median, fat-tail check
  8. Overlap audit: simultaneous positions on same symbol at any time
  9. Bootstrap CI on win rate (95% interval)
 10. Time-of-day distribution — any suspicious clustering

Reports each section and calls out pessimistic concerns explicitly.
"""
from __future__ import annotations

import json
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2

ALERTS = Path("/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_alerts_clean.ndjson")
DB = dict(host="100.67.112.3", port=5432, database="tradingcore",
          user="trading_user", password="TradingCore2025!")
CT = ZoneInfo("America/Chicago")


def fmt_pct(x): return f"{x*100:+.3f}%"


def preload(conn, symbols, start, end):
    out = {s: [] for s in symbols}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, time, open, close FROM core.market_data
            WHERE symbol = ANY(%(s)s) AND bar_size='1 min'
              AND time BETWEEN %(a)s AND %(b)s
            ORDER BY symbol, time
        """, {"s": symbols, "a": start, "b": end})
        for sym, t, o, c in cur.fetchall():
            out[sym].append((t, float(o), float(c)))
    return out


def bar_strictly_after(bars, ts):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] <= ts: lo = mid + 1
        else: hi = mid
    if lo >= len(bars): return None, None, None
    return bars[lo][0], bars[lo][1], bars[lo][2]   # ts, open, close


def bar_at_or_after(bars, ts):
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] < ts: lo = mid + 1
        else: hi = mid
    if lo >= len(bars): return None, None, None
    return bars[lo][0], bars[lo][1], bars[lo][2]


def signal_set(alerts, direction_filter=None):
    """Return list of (ts_dt_tzaware, symbol, direction) after 15-min dedup."""
    items = sorted(alerts, key=lambda a: a["ts"])
    last = {}
    kept = []
    for a in items:
        if a["strategy"] != "YMI_2.0": continue
        if a["direction"] not in ("long", "short"): continue
        if a["symbol"] not in ("ES", "NQ"): continue
        if direction_filter and a["direction"] != direction_filter: continue
        ts = datetime.fromisoformat(a["ts"]).replace(tzinfo=CT)
        key = (a["direction"], a["symbol"])
        if key in last and (ts - last[key]).total_seconds() < 900: continue
        kept.append((ts, a["symbol"], a["direction"]))
        last[key] = ts
    return kept


def compute_trades(signals, bars, hold_min, cost_rt=0.00005, entry_mode="close", latency_s=0):
    """Returns list of signed net returns."""
    rets = []
    for ts, sym, direction in signals:
        ts_entry = ts + timedelta(seconds=latency_s)
        b = bar_strictly_after(bars[sym], ts_entry)
        if b[0] is None: continue
        entry_bar_ts, entry_open, entry_close = b
        entry = entry_open if entry_mode == "open" else entry_close
        if entry <= 0: continue
        exit_target = entry_bar_ts + timedelta(minutes=hold_min)
        x = bar_at_or_after(bars[sym], exit_target)
        if x[0] is None: continue
        _, exit_open, exit_close = x
        exit = exit_open if entry_mode == "open" else exit_close
        if exit <= 0: continue
        sign = 1.0 if direction == "long" else -1.0
        raw = sign * (exit - entry) / entry
        rets.append(raw - cost_rt)
    return rets


def stats(rs):
    if not rs: return {"n": 0}
    rs = list(rs)
    wins = sum(1 for r in rs if r > 0)
    return {
        "n": len(rs), "wr": wins/len(rs),
        "mean": statistics.mean(rs),
        "med": statistics.median(rs),
        "std": statistics.stdev(rs) if len(rs) > 1 else 0,
        "min": min(rs), "max": max(rs),
    }


def bootstrap_wr_ci(rs, n_boot=1000, seed=42):
    """95% CI on win rate via nonparametric bootstrap."""
    if len(rs) < 5: return (0, 0)
    rng = random.Random(seed)
    wrs = []
    n = len(rs)
    for _ in range(n_boot):
        sample = [rs[rng.randrange(n)] for _ in range(n)]
        wrs.append(sum(1 for r in sample if r > 0) / n)
    wrs.sort()
    return (wrs[int(0.025 * n_boot)], wrs[int(0.975 * n_boot)])


def run():
    alerts = [json.loads(l) for l in ALERTS.read_text().splitlines() if l.strip()]
    sigs_all = signal_set(alerts)
    sigs_s = [s for s in sigs_all if s[2] == "short"]
    sigs_l = [s for s in sigs_all if s[2] == "long"]

    if not sigs_all:
        print("no signals"); return
    start = min(s[0] for s in sigs_all) - timedelta(days=2)
    end = max(s[0] for s in sigs_all) + timedelta(days=2)

    conn = psycopg2.connect(**DB); conn.autocommit = True
    bars = preload(conn, ["ES", "NQ", "SPY"], start, end)
    conn.close()

    print("="*92)
    print("PESSIMISTIC AUDIT — YMI 2.0 on ES+NQ")
    print("="*92)
    print(f"shorts (after dedup): {len(sigs_s)}    longs (after dedup): {len(sigs_l)}")

    # -------------------------------------------------------------------------
    # 1. Commission stress
    # -------------------------------------------------------------------------
    print("\n--- 1) COMMISSION STRESS (short 4h) ---")
    for cost in [0.00005, 0.0001, 0.0002, 0.0003, 0.0005]:
        rs = compute_trades(sigs_s, bars, 240, cost_rt=cost)
        s = stats(rs)
        print(f"  RT cost={cost*1e4:>4.1f}bp   n={s['n']} WR={s['wr']:.1%} mean={fmt_pct(s['mean'])}  median={fmt_pct(s['med'])}")
    print("  (default was 0.5bp; realistic ES+NQ is 2-3bp including slippage)")

    # -------------------------------------------------------------------------
    # 2. Entry mode: open-of-next-bar vs close-of-next-bar
    # -------------------------------------------------------------------------
    print("\n--- 2) ENTRY MODE (short 4h, realistic 2bp cost) ---")
    for mode in ["close", "open"]:
        rs = compute_trades(sigs_s, bars, 240, cost_rt=0.0002, entry_mode=mode)
        s = stats(rs)
        print(f"  entry={mode}:  n={s['n']} WR={s['wr']:.1%} mean={fmt_pct(s['mean'])}  median={fmt_pct(s['med'])}")
    print("  (backtest used close; real market orders fill near open. Open-entry is the honest baseline.)")

    # -------------------------------------------------------------------------
    # 3. Latency decay
    # -------------------------------------------------------------------------
    print("\n--- 3) LATENCY DECAY (short 4h, realistic 2bp, open-entry) ---")
    for lat_s, label in [(0, "0s"), (30, "30s"), (60, "60s"),
                         (300, "5 min"), (900, "15 min"), (1800, "30 min")]:
        rs = compute_trades(sigs_s, bars, 240, cost_rt=0.0002, entry_mode="open", latency_s=lat_s)
        s = stats(rs)
        print(f"  delay={label:>7}:  n={s['n']} WR={s['wr']:.1%} mean={fmt_pct(s['mean'])}")
    print("  (smooth decay = real edge; cliff-fall at 0→30s = signal leaking into entry bar)")

    # -------------------------------------------------------------------------
    # 4. Shuffle test (permutation null)
    # -------------------------------------------------------------------------
    print("\n--- 4) SHUFFLE (PERMUTATION) TEST  (short 4h, realistic 2bp, open) ---")
    real_rs = compute_trades(sigs_s, bars, 240, cost_rt=0.0002, entry_mode="open")
    real_s = stats(real_rs)
    real_mean = real_s["mean"]; real_wr = real_s["wr"]
    print(f"  real mean={fmt_pct(real_mean)}, WR={real_wr:.1%}  (n={real_s['n']})")
    # Shuffle: keep same (ts, sym) but re-label direction randomly (50/50)
    rng = random.Random(1729)
    N = 500
    fake_means = []; fake_wrs = []
    for _ in range(N):
        fake_sigs = [(ts, sym, "short" if rng.random() < 0.5 else "long")
                     for (ts, sym, _) in sigs_s + sigs_l]
        fake_rs = compute_trades(fake_sigs, bars, 240, cost_rt=0.0002, entry_mode="open")
        fs = stats(fake_rs)
        fake_means.append(fs["mean"]); fake_wrs.append(fs["wr"])
    fake_means.sort(); fake_wrs.sort()
    mean_p = sum(1 for m in fake_means if m >= real_mean) / N
    wr_p   = sum(1 for w in fake_wrs if w >= real_wr) / N
    print(f"  null (random labels) mean 95% CI: [{fmt_pct(fake_means[int(0.025*N)])}, {fmt_pct(fake_means[int(0.975*N)])}]")
    print(f"  null (random labels) WR   95% CI: [{fake_wrs[int(0.025*N)]:.1%}, {fake_wrs[int(0.975*N)]:.1%}]")
    print(f"  permutation p-value vs real mean: {mean_p:.3f}")
    print(f"  permutation p-value vs real WR:   {wr_p:.3f}")
    print("  (if real mean/WR land INSIDE the null CI, direction labels carry no info)")

    # -------------------------------------------------------------------------
    # 5. Direction invert
    # -------------------------------------------------------------------------
    print("\n--- 5) DIRECTION-INVERT TEST  (short 4h, realistic 2bp, open) ---")
    inverted = [(ts, sym, "long" if d == "short" else "short") for (ts, sym, d) in sigs_s + sigs_l]
    inv_rs = compute_trades(inverted, bars, 240, cost_rt=0.0002, entry_mode="open")
    orig_rs = compute_trades(sigs_s + sigs_l, bars, 240, cost_rt=0.0002, entry_mode="open")
    inv_s = stats(inv_rs); orig_s = stats(orig_rs)
    print(f"  original:    n={orig_s['n']} WR={orig_s['wr']:.1%} mean={fmt_pct(orig_s['mean'])}")
    print(f"  inverted:    n={inv_s['n']} WR={inv_s['wr']:.1%} mean={fmt_pct(inv_s['mean'])}")
    print(f"  sum of means (should be ~-2× cost if symmetric): {fmt_pct(orig_s['mean'] + inv_s['mean'])}")
    print("  (if inverted is also positive, counting bug or momentum ambient in window)")

    # -------------------------------------------------------------------------
    # 6. No-dedupe (honest repeated-fire) test
    # -------------------------------------------------------------------------
    print("\n--- 6) NO-DEDUPE (every repeat signal is a trade)  (short 4h, 2bp, open) ---")
    raw = [(datetime.fromisoformat(a["ts"]).replace(tzinfo=CT), a["symbol"], a["direction"])
           for a in alerts if a["strategy"] == "YMI_2.0" and a["direction"] == "short"
           and a["symbol"] in ("ES","NQ")]
    rs = compute_trades(raw, bars, 240, cost_rt=0.0002, entry_mode="open")
    s = stats(rs)
    print(f"  no dedupe: n={s['n']} WR={s['wr']:.1%} mean={fmt_pct(s['mean'])}")
    print(f"  with 15-min dedup (above): n={real_s['n']}")
    print("  (if rate per trade drops meaningfully without dedup, dedup was hiding stacked losers)")

    # -------------------------------------------------------------------------
    # 7. Return distribution (mean vs median, fat tails)
    # -------------------------------------------------------------------------
    print("\n--- 7) DISTRIBUTION (short 4h, 2bp, open) ---")
    rs = sorted(real_rs)
    if rs:
        top3 = sum(rs[-3:])
        bot3 = sum(rs[:3])
        total = sum(rs)
        print(f"  n={len(rs)}  mean={fmt_pct(statistics.mean(rs))}  median={fmt_pct(statistics.median(rs))}")
        print(f"  best 3 trades total: {fmt_pct(top3)}   bottom 3: {fmt_pct(bot3)}   all: {fmt_pct(total)}")
        print(f"  share of total carried by top 3 trades: {abs(top3/total)*100:.1f}%" if total else "")

    # -------------------------------------------------------------------------
    # 8. Overlap audit — 4h overlapping positions
    # -------------------------------------------------------------------------
    print("\n--- 8) OVERLAP AUDIT (short 4h) ---")
    ordered = sorted(sigs_s, key=lambda s: s[0])
    max_overlap_per_sym = {"ES": 0, "NQ": 0}
    active = {"ES": [], "NQ": []}
    for ts, sym, _ in ordered:
        # expire any finished
        active[sym] = [t for t in active[sym] if (ts - t).total_seconds() < 4*3600]
        active[sym].append(ts)
        max_overlap_per_sym[sym] = max(max_overlap_per_sym[sym], len(active[sym]))
    total_signals = len(ordered)
    print(f"  total short signals: {total_signals}")
    print(f"  max simultaneous open positions: ES={max_overlap_per_sym['ES']} NQ={max_overlap_per_sym['NQ']}")
    print("  (backtest treats each as independent; real $$ sizing must cap aggregate.)")

    # -------------------------------------------------------------------------
    # 9. Bootstrap WR CI
    # -------------------------------------------------------------------------
    print("\n--- 9) BOOTSTRAP 95% CI on WIN RATE (short 4h, 2bp, open) ---")
    ci = bootstrap_wr_ci(real_rs, n_boot=2000)
    print(f"  point WR = {real_s['wr']:.1%}, 95% CI = [{ci[0]:.1%}, {ci[1]:.1%}]  (n={real_s['n']})")
    print("  (a tight 85-95% CI on 76 samples is the most you can honestly claim)")

    # -------------------------------------------------------------------------
    # 10. Time-of-day distribution
    # -------------------------------------------------------------------------
    print("\n--- 10) SIGNAL CLUSTERING BY HOUR ---")
    hours = Counter(s[0].hour for s in sigs_s)
    print(f"  short signal hours (CT): ", end="")
    for h in sorted(hours):
        print(f"{h:02d}:{hours[h]:<3}", end=" ")
    print()
    hours_l = Counter(s[0].hour for s in sigs_l)
    print(f"  long  signal hours (CT): ", end="")
    for h in sorted(hours_l):
        print(f"{h:02d}:{hours_l[h]:<3}", end=" ")
    print()
    print("  (heavy clustering at a single hour suggests schedule-driven, not signal)")


if __name__ == "__main__":
    run()
