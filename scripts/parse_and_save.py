#!/usr/bin/env python3
"""Dedupe TV rows, parse into (timestamp, symbol, direction, strategy), save NDJSON."""
import json
import re
from pathlib import Path
from datetime import datetime

SRC = Path("/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_rows.ndjson")
DST = Path("/Users/cameronbennion/Projects/llm-cortex/.playwright-cli/tv_alerts_clean.ndjson")

rows = [json.loads(l) for l in SRC.read_text().splitlines() if l.strip()]
# dedupe by (subject, date_title, snippet[:60])
seen = set(); uniq = []
for r in rows:
    k = (r["subject"], r["date_title"], r["snippet"][:60])
    if k in seen: continue
    seen.add(k); uniq.append(r)
print(f"deduped: {len(uniq)}")

# Keep only Alert: subjects
alerts = [r for r in uniq if r["subject"].startswith("Alert:")]
print(f"Alert: subjects: {len(alerts)}")

# Parse timestamp
def parse_ts(title: str):
    # "Fri, Apr 10, 2026, 10:45 AM" — NBSP chars possible
    title = title.replace("\u202f", " ").replace("\u00a0", " ").strip()
    fmts = ["%a, %b %d, %Y, %I:%M %p", "%A, %B %d, %Y, %I:%M %p"]
    for f in fmts:
        try: return datetime.strptime(title, f)
        except ValueError: continue
    return None

def parse_alert(subj: str, snippet: str):
    """Return (symbol, direction, strategy) from subject + snippet."""
    s = subj.replace("Alert:", "").strip()
    sn = snippet.replace("\u00a0", " ").strip()

    # Symbol: from snippet "Your XXX alert was triggered" (NQ1!, ES1!, S5FI, SPY, VTI, QQQ)
    m = re.search(r"Your\s+([A-Z0-9!\.]+)\s+alert", sn)
    symbol = m.group(1) if m else None
    # Map TV ticker -> our market_data ticker
    sym_map = {"NQ1!": "NQ", "ES1!": "ES", "YM1!": "YM", "RTY1!": "RTY",
               "CL1!": "CL", "GC1!": "GC"}
    db_symbol = sym_map.get(symbol, symbol)

    # Direction
    low = s.lower()
    if "buy" in low: direction = "long"
    elif "sell" in low or "short" in low: direction = "short"
    elif "above 50d ma are <22%" in low: direction = "long"  # S5FI oversold = buy
    elif "put-call" in low and "extended" in low: direction = "short"  # heavy puts = contrarian short-market
    elif "reversal" in low: direction = "reversal"   # undefined direction
    elif "continuation" in low: direction = "continuation"
    elif "crossing above or below voldq" in low: direction = "crossing"
    else: direction = "unknown"

    # Strategy
    if "ymi2.0" in low or "ymi 2.0" in low: strategy = "YMI_2.0"
    elif "luxalgo" in low: strategy = "LuxAlgo_Reversal"
    elif "voldq" in low: strategy = "VOLDQ"
    elif "s&p stocks above 50d ma" in low or "stocks above 50d" in low: strategy = "S5FI_breadth"
    elif "put-call" in low: strategy = "SPY_put_call"
    elif "k continuation" in low: strategy = "K_continuation"
    elif "lorentzian" in low: strategy = "Lorentzian"
    else: strategy = "other"

    return db_symbol, direction, strategy

out = []
for r in alerts:
    ts = parse_ts(r["date_title"])
    if not ts: continue
    sym, direction, strategy = parse_alert(r["subject"], r["snippet"])
    out.append({
        "ts": ts.isoformat(),
        "symbol": sym,
        "direction": direction,
        "strategy": strategy,
        "subject": r["subject"],
        "snippet": r["snippet"][:150].strip(),
    })

# write
with DST.open("w") as f:
    for o in out: f.write(json.dumps(o) + "\n")
print(f"wrote {len(out)} parsed alerts to {DST}")

# stats
from collections import Counter
print("\nby strategy:")
for s, c in Counter(o["strategy"] for o in out).most_common():
    print(f"  {c:3d}  {s}")
print("\nby (strategy, direction):")
for k, c in Counter((o["strategy"], o["direction"]) for o in out).most_common():
    print(f"  {c:3d}  {k}")
print("\nby symbol:")
for s, c in Counter(o["symbol"] for o in out).most_common():
    print(f"  {c:3d}  {s}")
print(f"\ndate range: {min(o['ts'] for o in out)} .. {max(o['ts'] for o in out)}")
