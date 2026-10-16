# Technical Scanner Requirements (TS)

**Version:** 1.0 (October 15, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md) and [STRATEGY_SPEC.md](STRATEGY_SPEC.md)

The technical scanner finds **swing** ideas and a **next-session day-trading watchlist** across the watchlist, using a catalog of classic, published technical techniques. It is advisory: it produces ideas, plans and alerts, and never places orders. The intraday playbook (STRATEGY_SPEC §4–5) still decides live day-trade entries.

```bash
./run.sh scan-eval            # grade every technique on stored history; writes reports/scanner_eval.json
./run.sh scan                 # today's swing ideas + day watchlist (add --telegram to send it)
./run.sh scan --date 2026-09-15 --all    # a past session, every idea
```

trader-core also sends the scan to Telegram every trading day at 16:20 ET (`scanner.alert_after_close`).

## 1. Model choice (decision)

The question was: which free, open-source model "knows all the trading techniques" and can pick the best day and swing ideas?

| Option | Verdict |
| --- | --- |
| A finance LLM (FinGPT, Fin-R1, Qwen3, …) that "knows" trading techniques | **Rejected for detection.** LLMs read numbers poorly, cannot compute indicators reliably and are not reproducible bar by bar. Finance-tuned LLMs are trained on text and Q&A, not on chart detection. |
| [TA-Lib](https://ta-lib.org) (BSD) | **The reference definition.** It is the industry standard (150+ indicators, 61 candlestick patterns). The scanner's indicators and candlestick patterns are ported from it and tests compare them with TA-Lib bar for bar. It is not a runtime dependency: it would add a C extension to every image and to the hashed lock files. |
| pandas-ta | Rejected: the upstream project warns that it may be discontinued; the community fork is young. |
| **Native, deterministic technique engine (chosen)** | Pure Python + NumPy (`src/trader/scanner/`), TA-Lib-identical math, no new runtime dependency, strict-typed, causal by construction. |
| Qwen3 (Apache-2.0, already the local model) | Optional plain-English summary of the computed ideas (TS-13). It never changes a number. |

## 2. Requirements

| ID | Requirement |
| --- | --- |
| `TS-1` | Deterministic engine. Indicators, patterns and techniques are pure functions of the bars. The same bars give the same ideas; no LLM computes a number or decides a signal. |
| `TS-2` | Indicator set, identical to TA-Lib (within 1e-8) including the warm-up: SMA, EMA, RSI (Wilder), MACD (12/26/9), Bollinger (20, 2σ), ATR (Wilder 14), ADX/+DI/−DI (14), slow Stochastic (14/3/3), OBV, ROC. Plus Keltner (EMA20 ± 1.5×ATR20), rolling highs/lows. |
| `TS-3` | Candlestick patterns, identical to TA-Lib on every bar: engulfing (CDLENGULFING), hammer, shooting star, doji, morning star, evening star, piercing line, dark cloud cover (TA-Lib default candle settings: body and shadow sizes against the prior 10 bars). Plus inside bar and NR7 (no TA-Lib equivalent; rules in §3). |
| `TS-4` | Swing techniques (daily bars, long and short), §3. Each technique is written once for longs; shorts run the same code on the mirrored price series, so long and short rules cannot drift apart. |
| `TS-5` | Day-trading watchlist for the next session: tickers ranked by expected range (ATR% scaled by today's relative volume and range, §3.2), with the prior high, low and close, the 20-day range, EMA20 and a trend bias. Directional day entries come from the intraday playbook and the alerts bot, not from daily bars (measured, §6). |
| `TS-6` | Context: market regime (SPY vs its SMA50), trend (close vs SMA50/SMA200), relative strength vs SPY (63-session return difference), relative volume vs the prior 50-session average, ADX. |
| `TS-7` | Every idea carries: ticker, as-of date, technique, side, entry type (buy/sell stop, or next open), trigger, stop, T1 = trigger ± 1.5R, T2 = trigger ± 3R (computed from the printed trigger and stop, so the plan adds up to the cent), R, room to the nearest swing level in R, score 0–100 with its factors, confluence (other techniques that agree) and a plain-English reason with the numbers. R must be between 0.25 and 2.5 ATR, or there is no idea. |
| `TS-8` | Ranking. Score = 50 + measured edge + context (§4). Ideas are sorted by score; only ideas at or above `scanner.min_score` are reported (top `scanner.top_n`). |
| `TS-9` | One idea per ticker: the highest-scoring technique and side wins, the other techniques on that side are listed as confluence. |
| `TS-10` | No look-ahead. The ideas for day D depend only on bars up to and including D. Cutting the series after D never changes them (tested). |
| `TS-11` | Walk-forward grading. A stop entry fills only on the next session, when it trades through the trigger (at the open if the open is past it). An open more than 0.5R past the trigger cancels the idea: gaps are not chased (STRATEGY_SPEC §2, EX-3). Exits: −1R at the stop before T1 (a gap through the stop fills at the open); half off at T1 with the stop moved to break-even; the rest at T2, break-even or the close of session `scanner.max_hold_days`. A bar that touches the stop and a target counts the stop first (STRATEGY_SPEC §8). Sessions with stored minute bars (`--minutes`) are walked minute by minute. |
| `TS-12` | The evaluation report: per technique and side, ideas, fills, hit rate (+1R before −1R), average R, total R and profit factor; the same by score bucket; the day watchlist's lift (top N's next-day range vs the average ticker's) and calibration. A technique with negative average R over at least `scanner.min_samples` filled ideas is flagged **weak**. The measured edge per technique and side (average R × n / (n + 30), shrunk toward zero for small samples) is written to `scanner.eval_file` for the score. |
| `TS-13` | Optional Qwen summary (`trader scan --explain`): Qwen gets the computed ideas as JSON. Any decimal number in its text that is not in the input drops the summary. Without Ollama the scan still runs. |
| `TS-14` | Outputs: `trader scan` (text, `--json`, `--telegram`, `--all`, `--date`), `trader scan-eval`; `./run.sh scan` and `./run.sh scan-eval` (in the trader-core container, which has the bar store; `--telegram` goes through the notifier). trader-core sends the scan at 16:20 ET on trading days and retries until the day's bar is final. |
| `TS-15` | Config: `scanner:` with `symbols`, `min_score`, `top_n`, `in_play_top_n`, `min_samples`, `max_hold_days`, `disabled` (technique names, optionally `:long`/`:short`, validated), `eval_file`, `alert_after_close`. Defaults work without the section. |
| `TS-16` | Data: daily bars from the Parquet store (`trader fetch-history`, which also downloads `scanner.symbols`). A ticker needs 210 sessions (SMA200 + slope); with less, or with no bar on the requested day, it is listed as skipped with the reason, never guessed. |

## 3. Technique rules

Notation: `c, o, h, l, v` = today's bar; ATR = ATR14 (daily); buffer `b` = 0.05 × ATR. Unless stated otherwise the trigger is a next-session stop entry at today's high + b (long) or today's low − b (short). "Uptrend" = c > SMA50 > SMA200. Shorts mirror every rule (highs ↔ lows, above ↔ below); reversal candles use the bearish TA-Lib patterns.

### 3.1 Swing

| Technique (short name) | Setup today | Trigger | Stop |
| --- | --- | --- | --- |
| `TREND_PULLBACK` | Uptrend, EMA20 > EMA50; the lowest low of the last 3 sessions is at most 0.5×ATR above EMA20 and not below EMA50 − 0.25×ATR; close above EMA20; the last 3 sessions did not make a new 10-day high | today's high + b | lowest low of the last 3 sessions − b |
| `BREAKOUT_20D` (`BREAKDOWN_20D`) | Close above the highest high of the prior 20 sessions; volume ≥ 1.5× the prior 50-session average; close in the top 30% of the day's range | today's high + b | prior 20-day high − 0.5×ATR |
| `HIGH_52W` (`LOW_52W`) | Uptrend; close within 1×ATR under the 252-session high, without touching it today; the last 10 sessions span at most 2.5×ATR | 252-session high + b | lowest low of the last 10 sessions − b |
| `BULL_FLAG` (`BEAR_FLAG`) | Close > SMA50. Pole: the high 3–10 sessions ago is the highest since 8 sessions before it and at least 3×ATR above the lowest low of those 8 sessions. Flag (since the pole top): low ≥ 50% retracement, high ≤ pole top, average volume below the pole's | flag high + b | flag low − b |
| `SQUEEZE` | Close > SMA50; Bollinger(20,2) inside Keltner(20,1.5) on at least 5 of the prior 6 sessions; today closes above the upper Bollinger band, or the MACD histogram is positive and rising | today's high + b | lowest low of the last 5 sessions − b |
| `INSIDE_NR7` | Uptrend; today is an inside day (high ≤ prior high, low ≥ prior low, narrower range) and the narrowest range of the last 7 sessions | today's high + b | today's low − b |
| `RSI2_REVERSION` | Connors: c > SMA200 and RSI(2) < 10 (short: c < SMA200 and RSI(2) > 90) | next open (entry type `open`) | c − 2×ATR |
| `MACD_CROSS` | Uptrend; MACD crosses above its signal today while the MACD line is at most 1×ATR (an early cross) | today's high + b | lowest low of the last 5 sessions − b |
| `REVERSAL_CANDLE` | c > SMA200; bullish engulfing, hammer, morning star or piercing line today, with today's low within 0.5×ATR of EMA20, SMA50 or the prior 20-day low | today's high + b | lowest low of the last 3 sessions − b |

### 3.2 Day watchlist

| Field | Rule |
| --- | --- |
| Expected range | ATR% × clamp(1 + 0.15 × (min(RVOL, 4) − 1) + 0.15 × (min(range/ATR, 3) − 1), 0.7, 1.6) |
| Bias | `long` if c > EMA20 and uptrend; `short` if c < EMA20 and downtrend; else `neutral` |
| Levels | prior high, prior low, prior close (today's bar, for tomorrow), 20-day high and low, EMA20 |
| Plan text | long: ORB or VWAP reclaim, strength above the prior high; short: ORB breakdown or VWAP loss below the prior low; neutral: the first clean break of the prior range |

## 4. Score (TS-8)

Starts at 50, clamped to 0–100.

| Factor | Points | Why this weight |
| --- | --- | --- |
| Measured edge (shrunk average R from `scan-eval`) | 100 × edge, at most ±20 | The only factor that reflects what has actually worked |
| ADX ≥ 35 (overheated trend) | −10 | Lost in both halves of the history (§6) |
| Room to the nearest swing high (5 bars each side) or 52-week extreme beyond the trigger | ≥ 2R or none: +5 · < 1R: −10 | Checklist item 8 (STRATEGY_SPEC §3) |
| Trend (trend techniques): uptrend / downtrend against the trade | +5 / −10 | Convention; not predictive on this history, so kept small |
| Market regime (SPY vs SMA50) aligned / against | +5 / −5 | Same |
| Relative strength vs SPY in the trade's direction | up to +5 | Same |
| Relative volume ≥ 1.5 | +3 | Same |
| Confluence: each other technique on the same side | +4, at most +8 | Same |

## 5. Traceability

Every `TS-*` row in [TRACEABILITY.md](TRACEABILITY.md) names its test (`tests/unit/test_scanner.py`, `tests/replay/test_technical_scan_job.py`); CI fails if a row has none. The TA-Lib comparison tests run when TA-Lib is installed (`pip install ta-lib`) and are skipped otherwise.

## 6. Measured results (October 15, 2026)

Stored daily bars, Oct 2024 – Sep 2026, NVDA, AAPL, GOOGL, MSFT, AMZN, TSM, META, AVGO, TSLA (SPCX has too little history). Ideas are graded from Aug 2025 (after the 210-session warm-up). In-sample (IS) = Aug 2025 – Feb 2026; out-of-sample (OOS) = Mar – Sep 2026. Re-run with `./run.sh scan-eval`; nine mega-caps over 14 months is a small sample, so treat every number here as indicative.

### 6.1 Correctness

| Check | Result |
| --- | --- |
| 18 indicator outputs vs TA-Lib on real NVDA bars | identical (max difference 3e-12, same warm-up) |
| 8 candlestick patterns vs TA-Lib, all 13 stored symbols | identical on every bar (1,585 signals, 0 mismatches) |
| Spot checks of the 2026-09-30 signals against raw bars | AAPL pullback (low 328.70 vs EMA20 331.17, trigger 339.84), META NR7 (16.76 < prior six ranges), NVDA inside day: all correct |
| Long/short symmetry | a mirrored price series produces exactly the mirrored short idea (tested) |
| No look-ahead | ideas for day D are identical with and without the later bars (tested) |

### 6.2 What worked and what did not

| Finding | Evidence | Decision |
| --- | --- | --- |
| Directional day trades from daily patterns (NR7, inside day, range expansion) lose | IS −0.10R, OOS −0.15R per trade; also with minute-bar grading and with tighter stops (worse) | Removed. The day output is a watchlist (TS-5) |
| NR7 and inside days do **not** predict a wider next session on these stocks | next-day range 0.82×ATR after NR7, 0.88× after inside days, 0.90× on all days | NR7/inside stay only as swing techniques in trend |
| What predicts tomorrow's range across tickers is ATR% (and, less, RVOL and today's range) | rank correlation with next-day range: ATR% +0.61 IS / +0.40 OOS; RVOL +0.21 / +0.25 | Watchlist ranking. Top 3 moved 3.28% vs 2.57% (IS) and 3.26% vs 2.77% (OOS); beat the average on 93% / 78% of days |
| `RSI2_REVERSION` long is the one swing technique positive in both halves | +0.18R IS (105 trades), +0.18R OOS (59 trades), PF 1.61 overall | Kept; carries the largest measured edge |
| Losing in both halves: `REVERSAL_CANDLE` long, `RSI2_REVERSION` short, `TREND_PULLBACK` short, `BEAR_FLAG` | −0.44R / −0.44R; −0.82R / −0.16R; −1.0R (2) / −0.46R (19); −0.34R / −0.66R | Default `scanner.disabled` (still graded by `scan-eval`) |
| The classic context factors (trend, market regime, RS, volume, confluence) did not predict outcomes | their sign flips between IS and OOS | Kept at small weights (§4) |
| ADX ≥ 35 at the signal loses | −0.45R IS (31), −0.59R OOS (12) | −10 score |
| The swing period matters more than any factor: the whole catalog lost IS and made money OOS | all techniques: IS −0.15R (PF 0.72), OOS +0.04R (PF 1.10) | Re-run `scan-eval` regularly; the edge term follows the data |

### 6.3 Ranking, out of sample

Edges learned from IS only, applied to OOS:

| Selection | Trades | Avg R | Hit +1R | PF |
| --- | --- | --- | --- | --- |
| All techniques, every idea | 230 | +0.04 | 0.40 | 1.10 |
| All techniques, top-scored idea per day | 85 | +0.11 | 0.42 | 1.27 |
| Default disabled list, every idea | 156 | +0.24 | 0.47 | 1.68 |
| Default disabled list, top-scored idea per day | 80 | +0.25 | 0.47 | 1.77 |

The disabled list was chosen after looking at both halves, so the last two rows are **not** a clean out-of-sample test; read them as optimistic. Score buckets are not monotonic out of sample: the score is a sensible ordering with a measured edge term, not a proven predictor. Paper-trade the ideas (TEST_PLAN stage 4) before trusting them with money.
