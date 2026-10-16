# Trading Decisions: Coverage Map

**Version:** 1.0 (October 16, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md), [SCANNER_REQUIREMENTS.md](SCANNER_REQUIREMENTS.md) and [UI_REQUIREMENTS.md](UI_REQUIREMENTS.md)

The decisions a disciplined day and swing trader makes on every trade, and where this system makes each one: the rule, the code that enforces it, and where the owner sees it in the Owner Console (http://localhost:8080). Two paths take decisions:

- **Live trades** (trader-core playbook): every entry passes the 10-item checklist (CK-1..CK-10), the loss limits and the behaviour rules. Code enforces them; no prompt can change them.
- **Scanner ideas** (advisory, [SCANNER_REQUIREMENTS.md](SCANNER_REQUIREMENTS.md)): every swing idea carries the same decisions as checks (TS-17), a size from the risk manager and a verdict (TS-18), shown on the **Technical scan** page (TS-19, UI-37).

"✓" = enforced or checked; "—" = not applicable to that path.

## 1. Before the trade: context

| Decision | Live trades | Scanner ideas | Console |
| --- | --- | --- | --- |
| Is the market with me? | MF-1 QQQ above VWAP, not down > 1% ✓ | `market`: SPY above its 50-day average ✓ | Today (SPY/QQQ/SMH), Stock lookup checklist, Technical scan |
| Is the sector with me? | MF-2 SMH for NVDA/AVGO/TSM ✓ | `sector`: SMH above a rising 50-day average ✓ | Stock lookup checklist, Technical scan |
| What kind of day is it? | RG-1 normal / high-vol; RG-2 halves risk; RG-3 trend day ✓ | — (daily bars) | Today (regime), Controls |
| Does the higher timeframe agree? | daily levels in the game plan ✓ | `trend` (daily) and `weekly` (rising 10-week average) ✓ | Technical scan |
| Is the stock in play and liquid? | MF-3 RVOL ≥ 1.5, MF-4 spread, MF-5/MF-6 clean data, UN-1 in-play scan ✓ | day watchlist ranked by expected range (TS-5) ✓ | Scanner, Technical scan |
| Is it a leader? | relative strength in the setup score ✓ | `rs`: 63-session strength vs SPY ✓ | Scanner, Technical scan |
| Any event risk? | CK-1 earnings lock, TW-5 CPI/jobs/FOMC windows, TW-6 releases and speeches, NW news shock ✓ | `earnings` (hard, 3 sessions), `macro` (FOMC/CPI/jobs next session) ✓ | Compliance, Today (events), Technical scan |

## 2. The setup and the plan

| Decision | Live trades | Scanner ideas | Console |
| --- | --- | --- | --- |
| Is it a named, tested setup? | CK-4 playbook setups only; unplanned at half size (DR-7) ✓ | 9 published techniques (TS-4) with a walk-forward grade (TS-12) ✓ | Next-day ideas, Technical scan scorecard |
| Has the trigger actually fired? | CK-5 closed candle, no anticipation ✓ | next-session stop entry at a fixed trigger ✓ | Approvals, Technical scan |
| Am I chasing? | CK-6 ≤ 0.25 ATR past the trigger, LAT-3 at send, EX-3 cancel ✓ | grading cancels an open > 0.5R past the trigger (TS-11) ✓ | Latency, Trades |
| Is the stop structural and sane? | CK-7 ≤ 1 ATR(5-min) ✓ | R between 0.25 and 2.5 ATR (TS-7) ✓ | Trades, Technical scan |
| Is there room to the target? | CK-8 resistance ≥ 1.5R, T2 ≥ 2R ✓ | `room` ≥ 1.5R (CK-8), T1 1.5R / T2 3R ✓ | Technical scan |
| Is the trend overextended? | — | `overheated`: ADX ≥ 35 lost money on the history ✓ | Technical scan |
| Does the setup have an edge? | RC-3 negative expectancy → paper; SH-2 shadow trades; EXQ-1 slippage ✓ | `edge`: measured average R per technique (scan-eval) ✓ | Reports, Technical scan scorecard |
| Does a second opinion agree? | CK-9 review agent (Qwen) ✓ | — (code only; Qwen may only summarise, TS-13) | Trades (story) |
| Long or short? | long only (ADR-008); bearish views via options ✓ | bearish ideas are options-only views (`direction`, ADR-008) ✓ | Options (preview link from each bearish idea) |

## 3. Size and portfolio

| Decision | Live trades | Scanner ideas | Console |
| --- | --- | --- | --- |
| How much to risk? | LL-1 0.5% per trade; RC-1 0.25% in drawdown; RG-2 high-vol ✓ | `size`: RiskManager.size in trader-core (same rules) ✓ | Controls, Technical scan |
| How big can one position be? | LL-9 25% of equity, LL-11 1x leverage ✓ | inside `size` ✓ | Technical scan |
| Am I already in it? | DR-4 one trade per ticker per window, DR-1 never average down ✓ | `held` ✓ | Today (positions), Technical scan |
| Am I concentrated? | LL-8 max 3 open, LL-10 one of NVDA/AVGO/TSM ✓ | `correlation`: the higher-ranked idea takes the slot ✓ | Technical scan |
| Can I legally buy it now? | WS-1..WS-9 wash sales, RC-4 loss streak, AC-1 settled cash ✓ | `locks` ✓ | Compliance, Technical scan |
| Is it on my list? | UR-1 universe only ✓ | `tradable`: scanner.symbols are watch only ✓ | Config, Technical scan |

## 4. Managing and exiting

| Decision | Live trades | Scanner ideas | Console |
| --- | --- | --- | --- |
| Is the stop at the broker? | EX-2 bracket from the fill, EX-5 stop-market ✓ | — (advisory) | Trades |
| When do I take profit? | XR-2 half at 1R (day) / 1.5R (swing), stop to break-even ✓ | same model in grading (TS-11) ✓ | Trades |
| How do I let a winner run? | XR-3 trail (9-EMA 5-min / 10-day EMA) ✓ | — | Trades |
| When is the trade wrong? | XR-4 failed-trade exit; XR-1 stop never moved down; DR-2 never widen ✓ | stop first on an ambiguous bar (TS-11) ✓ | Trades |
| How long do I give it? | XR-5 time stop (15 min / 10:30 / 3:55 / 10 days) ✓ | `scanner.max_hold_days` in grading ✓ | Trades |
| Hold through earnings? | XR-6 close before the release ✓ | blocked at entry (`earnings`) ✓ | Compliance |

## 5. Discipline and review

| Decision | Live trades | Scanner ideas | Console |
| --- | --- | --- | --- |
| Have I lost enough today/this week? | LL-2..LL-7 trade count, daily/weekly/monthly loss, two losses in a row, profit give-back ✓ | — (advisory) | Today, Controls |
| Am I revenge trading? | DR-3 lock after a loss, 10-minute cooldown ✓ | `locks` (loss-streak) ✓ | Compliance |
| No setup today? | NT-1 no trade is logged with the closest candidate; NT-2 no minimum ✓ | "none qualify today" ✓ | Today, Technical scan |
| Did I follow the plan? | DR-8 overrides tagged; owner journal tags (UI-29) ✓ | — | Journal |
| Is the system still good enough? | §6.4 expectancy > 0, PF ≥ 1.3, win/loss ≥ 1.5, 0 rule breaches ✓ | `scan-eval` weak flags; `scanner.disabled` ✓ | Reports, Technical scan scorecard |

## 6. What is deliberately not automated

| Decision | Why | Where it lives |
| --- | --- | --- |
| Turning a weak setup off | A rule that cost money is flagged, never changed on its own (SH-2) | Owner edits `scanner.disabled` / setup config |
| Going live, or adding size | Gates in TEST_PLAN stage 4 and the go-live checklist | RUNBOOK, Controls |
| Shorting stock | Out of scope (ADR-008); bearish views use defined-risk options | Options page (advisory) |
| Option orders | Advisory only until the options gates (OPTIONS_REQUIREMENTS §9) | Options page |
