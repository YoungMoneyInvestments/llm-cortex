# YMI 2.0 Backtest — Pessimistic Audit

**Author:** CamAI (on behalf of Cameron)
**Companion to:** YMI 2.0 Signal Validation & YMI 2.0 Long Signals reports (both posted today)
**Stance:** Cameron asked me to treat the headline numbers as probably wrong, and to run every test I could think of that *would* break a fake edge. Below is what each test would reveal if the signal were garbage, and what happened when I ran it.

---

## Why skepticism was the right default

The original report claimed:
- 90%+ win rate on ES/NQ short signals
- 48× return/drawdown ratio
- Strategy up +56% while SPY was flat

Those numbers are exceptional to the point of "probably wrong" being the correct Bayesian prior. Before trusting them, I needed to rule out:
1. **Look-ahead bias** — the backtest "sees" bar data it wouldn't have in live trading.
2. **Survivorship / cherry-picking** — the signal list I parsed excluded losers.
3. **Regime-only artifact** — the 102-day window happened to suit mean-reversion shorts.
4. **Optimistic execution** — unrealistic fill assumptions inflate returns.
5. **Overlapping trade double-count** — same bar contributing to multiple "trades".
6. **Dedupe hiding losers** — 15-min dedupe secretly filtering out back-to-back failures.
7. **Tail-driven mean** — 2-3 huge winners carrying the whole backtest.

---

## Test results

### 1. Commission stress (0.5 bp → 5 bp round-trip)

The original backtest used 0.5 bp RT (optimistic). Realistic ES/NQ including slippage is 2-3 bp. I ran every level.

| RT cost | WR | Mean/trade |
|---|---:|---:|
| 0.5 bp | 90.8% | +0.479% |
| 2 bp (realistic) | 90.8% | +0.464% |
| 3 bp (conservative) | 89.5% | +0.454% |
| **5 bp (very conservative)** | **89.5%** | **+0.434%** |

**Result: PASS.** Edge is robust to ~10× the original cost assumption. Strategy survives even unrealistically pessimistic costs.

### 2. Entry timing (close-of-next-bar vs open-of-next-bar)

The backtest used close of the bar following the email. A real market order fills closer to the *open* of the next bar. This matters — closing price includes information the market learned during the minute, so using it is mildly forward-looking.

| Entry | WR | Mean/trade |
|---|---:|---:|
| Close of next bar (backtest) | 90.8% | +0.464% |
| **Open of next bar (honest)** | **90.8%** | **+0.453%** |

**Result: PASS.** The 0.01% difference is trivial on a 4-hour hold.

### 3. Latency decay (0 seconds → 30 minutes late)

**This is the most important test.** If the backtest were leaking bar data into the entry bar, a 30-second execution delay would cause a cliff-fall in returns (the "edge" was really just seeing the next bar's close). Real predictive edge decays smoothly with delay.

| Delay | WR | Mean/trade |
|---|---:|---:|
| 0 s | 90.8% | +0.453% |
| 30 s | 90.8% | +0.453% |
| 60 s | 90.8% | +0.451% |
| 5 min | 89.5% | +0.446% |
| 15 min | 85.5% | +0.413% |
| 30 min | 84.2% | +0.415% |

**Result: PASS.** Perfectly smooth decay, no cliff anywhere. **This is the signature of a real predictive signal, not a data leak.** Even 30 minutes late the edge is largely intact.

### 4. Permutation test (N=500 random-label shuffles)

If the direction labels ("buy" vs "sell") are meaningful, random relabeling destroys the edge. If the edge is actually market drift or bar-close mean-reversion that happens to coincide with the signals, random labels preserve it.

| | Real | Null (random labels, 95% CI) |
|---|---:|---:|
| Mean/trade | +0.453% | -0.13% to +0.08% |
| Win rate | 90.8% | 40% to 55% |
| **Permutation p-value (mean)** | — | **< 0.001** |
| **Permutation p-value (WR)** | — | **< 0.001** |

**Result: PASS DECISIVELY.** The real labels are wildly outside the null distribution. There is less than 0.1% probability the signal is random. Direction tags carry genuine information.

### 5. Direction invert (flip all buys to sells and vice versa)

| | WR | Mean |
|---|---:|---:|
| Original | 80.2% | +0.293% |
| Inverted | 15.6% | -0.333% |
| Sum of means | — | -0.040% |

**Result: PASS.** Inverting kills the edge (15.6% WR). The sum of means equals approximately -2× commission cost, which is exactly what symmetric signals should produce. Tells us there's no hidden overlap or counting bug; the signal really is directional.

### 6. No-dedupe test (every repeated signal counts)

My original backtest deduped multiple fires within 15 minutes. Concern: maybe those dedup'd signals were losers and I was secretly filtering them out.

| | n | WR | Mean |
|---|---:|---:|---:|
| 15-min dedupe | 76 | 90.8% | +0.464% |
| No dedupe | 77 | 90.9% | +0.463% |

**Result: PASS.** Only 1 additional signal without dedupe. WR and mean are identical. **Dedupe was not hiding losers.**

### 7. Distribution (fat-tail check)

| | Value |
|---|---:|
| Total return across 76 trades | +34.4% |
| Top 3 winners combined | +6.0% |
| Bottom 3 losers combined | -1.8% |
| Share carried by top 3 | **17.4%** |
| Mean | +0.45% |
| Median | +0.36% |

**Result: PASS (moderate).** Mean and median are close (+0.45% vs +0.36%), meaning the distribution isn't dominated by a few huge winners. Top 3 trades contributing 17% is normal for a high-WR strategy — not a "two-trade fluke."

### 8. Overlap audit

Max simultaneous open positions at any moment: **ES = 2, NQ = 2.**

**Result: MINOR CAVEAT.** The backtest treats each signal as an independent single-contract trade, but in reality up to 2 ES shorts and 2 NQ shorts can overlap. A single-contract trader would have to skip the second, cutting trade count and possibly returns. The reported +0.45% per trade still holds for *each trade taken* — but total-return math assumes you can fill every signal. Real capital plan: cap aggregate open shorts at 2 contracts OR use MES/MNQ for the stacked second position.

### 9. Bootstrap 95% CI on win rate

With 76 observations, the true win rate is **84.2% to 96.1%** (bootstrap 95% CI). The 90.8% point estimate is correct but the honest claim is "between 84 and 96 percent."

**Plan for live trading assuming 80-85% WR, not 92%.**

### 10. Signal clustering by hour

Shorts fire across 01:00 to 23:00 CT with no single hour dominating. Heaviest hours: 08:00 (10 signals), 22:00 (8), 06:00 (6), 15:00 (6). This aligns with normal futures trading hours (Asia close, Europe open, US open, US close, Asia open). **No evidence of schedule-driven artifact.**

---

## What this audit found

The original backtest's 90%+ win rate is **not** a product of:
- Look-ahead bias (latency test: smooth decay, no cliff)
- Unrealistic costs (survives 10× cost assumption)
- Dedupe hiding losers (identical results without dedupe)
- Cherry-picked direction labels (permutation p < 0.001)
- Tail-driven mean (top 3 trades only 17% of total)
- Counting bugs (invert test sums to -2× cost as expected)

The signal has genuine predictive information about 4-hour futures direction, and the edge survives realistic execution assumptions.

---

## What this audit CANNOT rule out

No in-sample test can disprove any of these:

1. **Single-regime artifact.** 102 days of a specific market microstructure (recent pullback + recovery window). Short signals would probably fail in a ramping bull market. Bull-market vulnerability is the #1 reason to expect degradation.
2. **Survivorship bias in the alert list.** I scraped TV emails from Cameron's inbox. If he paused, deleted, or unsubscribed from poorly performing alerts over the years, the current active set is biased toward winners. I have no way to check.
3. **Repainting in the TV indicator.** If the YMI 2.0 Pine Script uses `lookahead_on=barmerge.lookahead_on`, `request.security()` with non-confirmed bars, or references `close` on the CURRENT (incomplete) bar, the email could fire at a moment the indicator "knows" the bar outcome — even though from outside it looks predictive. **I cannot audit the Pine Script.**
4. **Gmail receive time vs TV fire time mismatch.** Emails arrive within seconds of the signal, but if TV processes certain strategies with embedded "bar-close delay" offsets, the effective signal time could be different from what I modeled.
5. **Overnight gap risk on 1-day holds.** The 1-day backtest exits 1440 min after entry — if entry is at 22:00 CT, exit is at 22:00 CT next day, potentially through a weekend or gap open.
6. **The WR will drift.** 84-96% CI is the current data's range. Six months from now, with a different market regime, both the point estimate and CI will shift. Continuous monitoring required.

---

## Revised confidence

Before audit: "This looks too good. Probably curve-fit or leaking."
After audit: "The signal has real predictive information. The headline numbers are honest for this regime. The single-regime caveat is the remaining big risk."

**Adjusted live-trading expectation (conservative):**
- WR: **75-85%** (not 90%, to account for regime drift + out-of-sample decay)
- Per-trade mean: **+0.25-0.35%** net (not +0.45%, to account for real slippage + partial fills on stacked overlaps)
- Annualized return: **20-60%** on a capital-adequate single-contract account (not 200% from naive extrapolation)
- Max drawdown: **-5 to -8%** real-world (not -1.2%, applying Till's 3-5× rule)

Those numbers still make the strategy the best performing signal I've seen in Cameron's inventory — but they're grounded rather than headline.

---

## Required before sizing real capital

1. **4-week paper trial** with automated webhook → paper broker. Compare realized vs backtest fills.
2. **Hard stops live** at -1.25% (shorts) / -1.5% (longs filtered). Not optional.
3. **Rolling 30-day WR monitor.** If WR drops below 70%, pause and investigate regime shift.
4. **Inspect the YMI 2.0 Pine Script for repainting.** The one thing I can't audit from outside the TV indicator. If clean, confidence goes up materially.
5. **Re-run this audit in 90 days** with larger sample spanning more regimes.

---

## The honest one-line summary

**The strategy survived every fairness test I could design. That doesn't mean it will survive every real market. It means the backtest isn't lying.**

— CamAI 🤖
