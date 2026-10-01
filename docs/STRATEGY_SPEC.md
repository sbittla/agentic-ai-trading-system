# Strategy Specification

**Version:** 1.0 (September 30, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md) sections 5 and 6

This document turns each playbook setup into exact, codeable rules, so backtest, replay and live trading make the same decision on the same bars. Where this document and the prose in REQUIREMENTS.md differ, this document wins and REQUIREMENTS.md is corrected.

---

## 1. Conventions

| Term | Definition |
| --- | --- |
| Bar | Regular-session bar from the IEX feed. 5-min bars start at 9:30, 9:35, … A bar is **closed** when its end time has passed and the bar message has arrived. |
| Decision time | Rules are evaluated only on closed bars. No rule uses the current, unfinished bar except the entry fill check. |
| OR5, OR15 | High, low and midpoint of 9:30–9:35 and 9:30–9:45 |
| ATR5 | 14-period Wilder ATR on 5-min bars, carried across days |
| ATR14d | 14-period Wilder ATR on daily bars, as of the prior close |
| VWAP | Session VWAP from 9:30 on 1-min bars |
| HOD | High of day so far |
| Tick buffer `b` | 0.05 × ATR5 (stops sit this far beyond the level) |
| R | entry − stop, per share |
| Trend day | At least 70% of closed 5-min bars since 9:30 closed above VWAP |
| Resistance | Nearest of: prior day high, pre-market high, HOD (for setups below HOD), 20-day high, above entry. "Setups below HOD" are VWAP_RECLAIM, FIRST_PULLBACK and VWAP_TREND; for breakout setups (ORB, GAP_AND_GO, POWER_HOUR_BREAKOUT, swing setups) the breakout bar's own high is not resistance |

**Rounding:** prices to $0.01; stops rounded down, entries and targets rounded to the nearest cent.

## 2. Common entry mechanics (all setups)

1. When a setup's trigger condition is met on a closed bar, A4 emits `SetupTriggered` with `trigger` (the level broken), `stop` and targets.
2. Entry order: limit buy at `trigger + 0.10 × ATR5`, plus the configured marketable offset. Never above `trigger + 0.25 × ATR5` (chase limit, checklist item 6).
3. If the last price is already above `trigger + 0.25 × ATR5` when the decision is made, the order is still placed at the limit above, waiting for a pullback, and is cancelled after 3 minutes or if price reaches `trigger + 0.50 × ATR5` (EX-3).
4. Two bracket orders, whole shares: leg A = half the quantity (rounded down) with take-profit at T1; leg B = the rest with take-profit at T2. Both share the same stop. If total quantity is 1 share, only leg B is used.
5. Day trades use `time_in_force = day`; swing trades `gtc`.

## 3. Checklist item 8 (clarified)

REQUIREMENTS.md item 8 is replaced by:

> **8.** The nearest resistance is at least **1.5R** above entry, and T2 is at least **2R** above entry.

T1 (first partial exit) stays at 1R for day trades and 1.5R for swing trades. The 1.5R resistance test ensures there is room for the first partial and the move beyond it.

## 4. Morning setups

### 4.1 ORB — Opening range breakout

| Rule | Definition |
| --- | --- |
| Window | Bars closing 9:40–10:15 |
| Range | OR5. If OR5 width < 0.3 × ATR5 (too narrow, noise), use OR15 and start at the 9:50 bar |
| Trigger | A 5-min bar closes above OR high, and that bar's volume ≥ the average volume of the opening-range bars |
| Trigger level | OR high |
| Stop | OR low − b. If R > 1.0 × ATR5, use OR midpoint − b. If R is still > 1.0 × ATR5, **no trade** |
| T1 / T2 | entry + 1R / entry + 2R |
| One-shot | Only the first trigger per ticker per day; a failed ORB is not retried |
| Skip if | Gap down > 1% (ORB longs after big gap-downs fail often); price below VWAP at trigger |

### 4.2 GAP_AND_GO

| Rule | Definition |
| --- | --- |
| Pre-conditions | Gap up between 1% and the smaller of 3 × ATR14d (in %) or 6%; A3 catalyst is not "none" |
| Window | Bars closing 9:40–10:00 |
| Trigger | A 5-min bar closes above max(pre-market high, OR5 high) |
| Stop | OR5 low − b; R must be ≤ 1.0 × ATR5 |
| T1 / T2 | entry + 1R / entry + 2R |
| Data caveat | If the free feed has no pre-market bars, use OR5 high only and tag the setup `GAP_AND_GO_NO_PM` (tracked separately in reviews) |

### 4.3 VWAP_RECLAIM

| Rule | Definition |
| --- | --- |
| Window | Bars closing 9:45–10:15 |
| Pre-conditions | At least one 5-min close below VWAP after 9:35, and QQQ above its VWAP (MF-1, no exception) |
| Trigger | A 5-min bar closes above VWAP with volume above the prior bar; trigger level = that bar's high |
| Entry | On a 1-min trade above the trigger level (break of the reclaim bar's high) |
| Stop | Lowest low since the first close below VWAP − b; R ≤ 1.0 × ATR5 |
| T1 / T2 | entry + 1R / entry + 2R |

### 4.4 FIRST_PULLBACK

| Rule | Definition |
| --- | --- |
| Window | Bars closing 9:45–10:15 |
| Impulse | HOD ≥ OR5 high + 0.5 × ATR5, made with RVOL ≥ 2 |
| Pullback | 1 to 3 consecutive 5-min bars with lower highs; their lows stay ≥ min(VWAP, EMA9) − 0.1 × ATR5 |
| Trigger | Price trades above the high of the last pullback bar |
| Stop | Lowest pullback low − b; R ≤ 1.0 × ATR5 |
| T1 / T2 | entry + 1R / max(entry + 2R, HOD) |
| One-shot | First pullback of the day only |

## 5. Afternoon setups

### 5.1 POWER_HOUR_BREAKOUT (day)

| Rule | Definition |
| --- | --- |
| Window | Bars closing 15:05–15:40 |
| Pre-conditions | Trend day; RVOL ≥ 1.5; price above VWAP |
| Trigger | A 5-min bar closes above the HOD set before 15:00 |
| Stop | Lowest low of the last three 5-min bars − b; R ≤ 1.0 × ATR5 |
| T1 / T2 | entry + 1R / entry + 2R (time exit at 15:55 regardless) |

### 5.2 VWAP_TREND (day)

| Rule | Definition |
| --- | --- |
| Window | Bars closing 15:05–15:40 |
| Pre-conditions | Trend day; price has not closed below VWAP since 14:00 |
| Trigger | A 5-min bar's low ≤ VWAP + 0.1 × ATR5 and it closes above VWAP; trigger level = that bar's high |
| Stop | VWAP − 0.25 × ATR5 |
| T1 / T2 | entry + 1R / HOD if ≥ entry + 2R, else entry + 2R |

### 5.3 SWING_CLOSE_NEAR_HIGH (swing)

| Rule | Definition |
| --- | --- |
| Evaluation | At 15:45 using today's bar so far as the daily candle |
| Trend | Close > EMA20d > EMA50d |
| Pullback | In the last 5 sessions, a daily low ≤ EMA20d × 1.005 (or EMA50d × 1.005) |
| Strength | Day range position ≥ 0.75; day change ≥ 0 |
| Trigger level | Last price at 15:45 |
| Stop | Lowest low of the last 5 sessions − 0.1 × ATR14d. If R > 1.5 × ATR14d, **no trade** |
| T1 / T2 | entry + 1.5R / entry + 3R, remainder trails the 10-day EMA |
| Exclusions | Earnings within 3 trading days; FOMC tomorrow |

### 5.4 SWING_BASE_BREAKOUT (swing)

| Rule | Definition |
| --- | --- |
| Evaluation | At 15:45 |
| Trigger | Last price > highest close of the prior 20 sessions; projected day volume ≥ 50-day average volume |
| Projected day volume | IEX volume so far ÷ average fraction of daily IEX volume done by 15:45 (from the last 20 sessions) |
| Stop | Breakout level − 0.5 × ATR14d |
| T1 / T2 | entry + 1.5R / entry + 3R, remainder trails the 10-day EMA |
| Exclusions | As 5.3 |

## 6. Sizing algorithm (A7-2)

```
risk_pct   = 0.25 if equity <= high_water_mark * (1 - 0.03) else 0.50
risk_pct  *= 0.5 if not planned                       # DR-7
risk_usd   = equity * risk_pct / 100
R          = entry - stop
qty_risk   = floor(risk_usd / R)
qty_cap    = floor(equity * 0.25 / entry)             # max one position 25%
qty_bp     = floor(min(buying_power, equity * 1.0 - open_exposure) / entry)   # 1x leverage
qty        = min(qty_risk, qty_cap, qty_bp)
if qty < 1: no trade (reason SIZE_TOO_SMALL)
```

## 7. Worked example (illustrative numbers)

NVDA, morning, ORB. Account equity $25,000, not in drawdown, planned setup.

| Item | Value |
| --- | --- |
| OR5 high / low / mid | $190.40 / $188.90 / $189.65 |
| ATR5 | $0.85 → b = $0.04 |
| OR5 width | $1.50 (≥ 0.3 × ATR5, so OR5 is used) |
| Trigger | 9:45 bar closes at $190.62 above $190.40, volume above OR average |
| Entry limit | 190.40 + 0.085 = **$190.49** (chase limit $190.61) |
| Stop with OR low | 188.90 − 0.04 = $188.86 → R = $1.63 > ATR5 → use midpoint |
| Stop with OR mid | 189.65 − 0.04 = **$189.61** → R = **$0.88** (≤ ATR5, OK) |
| Resistance | Prior day high $192.80 ≥ entry + 1.5R ($191.81) → item 8 passes |
| T1 / T2 | **$191.37** / **$192.25** |
| qty_risk | floor(125 / 0.88) = 142 shares |
| qty_cap (25%) | floor(6,250 / 190.49) = **32 shares** ← binding |
| Orders | Leg A 16 sh, TP $191.37; leg B 16 sh, TP $192.25; both stop $189.61 |
| Actual risk | 32 × 0.88 = $28.16 (0.11% of equity) |

**Correction found during the build (2026-10-01):** as printed, this example fails checklist item 7, because R = $0.88 is wider than 1.0 × ATR5 = $0.85. The code enforces R ≤ 1.0 × ATR5, so read the example with ATR5 ≥ $0.88 (the tests use $0.90). The example also leaves out the marketable offset (`entries.limit_offset_pct`). The sizing arithmetic (32 shares, legs 16/16, $28.16 risk) is unchanged and tested.

**Lesson from the example:** with a $25K account, the 25% position cap, not the 0.5% risk rule, limits size on high-priced stocks with tight stops. Actual risk per trade will often be well under 0.5%. This is intended: the cap protects against gaps through the stop.

## 8. Edge cases

| Case | Rule |
| --- | --- |
| Trading halt during a position | No new orders for that ticker until 10 min after resumption; existing brackets stay; flatten rules still apply |
| Stock gaps through the stop (e.g. news) | Stop fills at market; journal records slippage; counts as a loss for all limits |
| Partial fill on entry | Brackets attach to the filled quantity; the unfilled rest is cancelled at the 3-min rule |
| Both T1 and stop touched in the same 1-min bar (replay) | Assume the stop filled first (conservative) |
| Early-close days | Only the morning window; morning flatten still 10:30 |
| Ticker split or ticker change | A1 blocks the ticker until history is adjusted |
| Missing pre-market data | Setups needing it use the `_NO_PM` variant or are skipped |
| Two setups on the same ticker at once | Take the higher signal score; DR-4 prevents a second entry in the window |
| Signals on several tickers at the same time | Rank by signal score; take until max trades / correlation limits are reached |

## 9. Change control

- **SC-1:** Any change to a number in this document is a new version, replayed on the full recorded-day library before use.
- **SC-2:** New setups start in paper only and need 30 paper trades with positive expectancy before live use.
