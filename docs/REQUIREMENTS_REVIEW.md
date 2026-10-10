# Requirements Review: Do They Help a Day Trader?

**Date:** October 10, 2026 · **Scope:** REQUIREMENTS.md, STRATEGY_SPEC.md, ALERTS.md, UI_REQUIREMENTS.md, OPTIONS_REQUIREMENTS.md, MONITORING.md · **Status:** findings for owner review; no requirement is changed yet

## Verdict

The requirements are strong on **discipline and safety**:

- hard loss limits, a no-chase rule, bracket orders, wash-sale handling
- "no setup, no trade" (NT-1)
- full journaling and staged go-live gates

These are what most retail day traders lack.

The gaps are in **speed, data quality, the information the trader gets at the moment of decision, and how fast the system learns**. Six of them (P1) could make the system miss most of its own setups or act on misleading data. Fix them before paper trading (M8) is used to judge the strategy.

| Priority | Count | Meaning |
| --- | --- | --- |
| **P1** | 6 | Can make the system miss trades or act on wrong data; fix before judging paper results |
| **P2** | 7 | Large value for a day trader; plan into the next milestones |
| **P3** | 8 | Quality of life, completeness |

---

## P1: fix before judging paper results

### G1. No time budget from trigger to order (and the existing timers conflict)

- **What the requirements say:**
  - CK-5: the trigger fires on a *closed* 5-minute candle.
  - CK-6: the entry may be at most 0.25 ATR(5m) above the trigger (never chase).
  - A5-2 and A6-1: a Qwen proposal, then a Qwen review, each allowed up to 60 s (QW-3).
  - Approve mode adds up to 90 s.
  - A5-4: the proposal expires after 2 minutes.
  - EX-3: the entry is cancelled after 3 minutes or 0.5 ATR.
- **Problem:** on the reference laptop (8B model, 8 GB GPU) the two model calls alone can take 20–40 s, and an approval adds more. On a breakout, the price often moves past the 0.25 ATR limit within that time. The system will then **cancel or refuse many of the trades it set out to take**, and the paper results will measure the delay, not the strategy. Nothing states an end-to-end target, and the 2-minute expiry can run out before an approval within its 90 s window arrives.
- **Proposed requirements:**
  - **LAT-1** Latency budget, measured and reported: trigger (candle close) → order sent, p95 ≤ 10 s in auto mode; ≤ the owner's response time + 5 s in approve mode.
  - **LAT-2 Pre-arming.** When price is within 0.3 ATR(5m) of a game-plan trigger, A5 and A6 run **before** the candle closes, on the expected trigger. At the close only code checks run (CK-5, CK-6, A12, A7), so the model calls leave the critical path.
  - **LAT-3** CK-6 is re-checked at the moment the order is sent, not only when the proposal is made. The approval message shows the live distance from the trigger.
  - **LAT-4** Expiries line up: proposal expiry ≥ approval window + model time. Missed trades are logged with a reason (`EXPIRED`, `CHASE_AFTER_APPROVAL`, `MODEL_TIMEOUT`) and graded (G7).

### G2. The free IEX feed drives triggers, RVOL and the spread filter

- **What the requirements say:** all bars, quotes, the opening range, RVOL and the spread check MF-4 (< 0.05%) come from Alpaca's free IEX feed, about 2–3% of US volume (ADR-003).
- **Problem:**
  - IEX highs and lows can differ from the consolidated tape that every chart shows, so the opening range and breakout levels may not match the levels the trader sees.
  - IEX quotes are often one-sided or wider than the national best bid and offer, so MF-4 can fail on liquid stocks or pass on stale quotes.
  - RVOL on 2% of volume is noisy in the first minutes.
- **Proposed requirements:**
  - **DQ-6** During paper trading, compare IEX against consolidated (SIP) data. Alpaca's free plan has historical SIP bars delayed 15 minutes, enough for after-the-fact checks. Report the opening-range and trigger divergence per day.
  - **DQ-7** Go-live gate: either measured divergence below a set threshold, or the paid SIP feed (`data.alpaca_feed: sip`).
  - **DQ-8** MF-4 uses quotes only when both sides are present and less than 2 s old; otherwise the trade is skipped with reason `NO_RELIABLE_QUOTE`, never assumed to pass.

### G3. The alerts bot and trader-core can't run together, and they disagree

- **Problem:**
  - Alpaca allows one market-data WebSocket per account, so `trader alerts` and trader-core can't run on the same keys at once.
  - When they do run (on two accounts), the bot sends raw signals (for example "NVDA ORB breakout") with no market filter, no checklist and no lock check. trader-core may correctly refuse the same move.
  - The trader gets two conflicting streams.
- **Proposed requirements:**
  - **AL-1** When trader-core runs, it produces the alerts itself from its own A2 stream (one WebSocket) and sends them through the notifier. The standalone bot is only for when the core is off.
  - **AL-2** Every alert is labelled either **"raw signal"** or **"qualified setup (checklist 8/10: failed MF-1 QQQ below VWAP)"**, so the trader knows which ones the system would trade.

### G4. Alerts are not actionable

- **Problem:** an alert says *what happened* ("VWAP reclaimed") but not *what to do*. A day trader needs the plan in the same message.
- **Proposed requirement:** **AL-3** Every directional alert carries:
  - the trigger and the invalidation (stop) level
  - T1/T2
  - the share size for 0.5% risk at the current equity
  - the R:R to the next resistance
  - the RVOL, and whether the stock is above or below VWAP and QQQ
  - These are computed with the same A4/A7 code as real trades, so alert and trade numbers always match.

### G5. Intraday news is 15 minutes slow

- **What the requirements say:** A3 refreshes every 15 minutes ("Done when: flags under 15 minutes old").
- **Problem:** an 8-K, a downgrade or a halt can hit an open day trade within seconds. Fifteen minutes is fine for the game plan and far too slow while in a position.
- **Proposed requirements:**
  - **NW-1** During windows, poll news every 60 s for tickers that have an open position or a game-plan setup. Finnhub's free tier, 60 calls a minute, covers 10 tickers.
  - **NW-2 Shock rule:** a move of more than 1.5 ATR(5m) within one minute on an open position triggers an alert and moves the stop to the 1-minute low (longs). A new negative 8-K or a downgrade on an open ticker → alert, and no new entries in that ticker today.

### G6. 10:00 AM economic releases fall inside the morning entry window

- **What the requirements say:** TW-5 handles only CPI and the jobs report (8:30) and FOMC.
- **Problem:** many market-moving releases come out at **10:00 ET**: ISM, JOLTS, consumer sentiment, new home sales, durable goods revisions. Morning entries run until 10:15, so a fresh breakout at 9:58 can reverse on the 10:00 print.
- **Proposed requirement:** **TW-6** The macro calendar includes 10:00 releases and scheduled Fed speeches. On those days, no new entries 9:57–10:05, and open day trades get their stop moved to break-even if at +0.5R or better before 9:59.

---

## P2: large value, plan into the next milestones

### G7. The system learns too slowly: no shadow trades

- **Problem:**
  - Ten tickers, two windows, strict filters and at most 3 trades a day may give a few trades a week. RC-3 (20 trades per setup) and the go-live gate (100 paper trades) could take most of a year.
  - Nothing records what would have happened to the setups that triggered but were not taken: filtered out, vetoed by A6, owner said NO, timed out, or blocked by limits.
- **Proposed requirement:** **SH-1** Shadow-trade every triggered setup with a simulated bracket, on the same fill model as replays. Report:
  - expectancy of taken vs not-taken setups
  - **A6 veto accuracy** (does the Qwen review add value or only cost time and trades?)
  - the value of each filter (MF-1 to MF-5)
  - the cost of owner NO and timeouts
  - This multiplies the sample size and tests whether each rule earns its keep.

### G8. Approving on the phone in 90 s with text only

- **Problem:** the trader must decide from a text message in 90 seconds. A chart is how a day trader decides.
- **Proposed requirements:**
  - **AP-1** The approval message includes a small **chart image**: 5-minute candles, VWAP, the opening range, entry, stop and T1. It is rendered by code and sent with Telegram `sendPhoto`, about 50 KB.
  - **AP-2 Inline buttons:** `YES ABCD` / `NO ABCD` buttons carry the one-time code. Telegram callback queries still arrive through `getUpdates` (add `callback_query` to `allowed_updates`), so the bot stays outbound-only. SEC-2 is kept: the code is unique and single-use, and only allow-listed chats see it.
  - **AP-3** The message is edited live: price now, distance from the trigger in ATR, and seconds left. It shows **"would now be a chase: will be refused"** once CK-6 would fail.

### G9. No co-pilot (manual) mode

- **Problem:**
  - Many day traders execute themselves: in another broker, or by hand while they build trust.
  - The system either trades through Alpaca itself or only sends ideas. Nothing tracks a trade the owner placed by hand, so there are no exit alerts, no journal entry and no stats for it. The same applies to the options advisories.
- **Proposed requirements:**
  - **CP-1** `mode.day_trades: copilot`: send the full trade plan (G4) at the trigger, place no order.
  - **CP-2** The owner reports a fill (`FILLED NVDA 40 @ 132.55`, or automatically from the Alpaca account's own positions and fills). The system then runs the exits as **alerts**: stop hit, T1, trail level, failed-trade, time stop at 10:30 and 3:55. It journals the trade and includes it in all stats, tagged manual (DR-8).
  - **CP-3** The same applies to options advisories: A16 reads the Alpaca account's actual option fills when the owner placed the suggested trade.

### G10. The opportunity set is narrow

- **Problem:**
  - Ten fixed mega-caps, long only and two windows mean many no-trade days. Down days give nothing on the stock side; options cover part of that.
  - Day traders look for **stocks in play**: gappers with news and unusual volume.
  - The alerts bot already watches the top 20 S&P names, but the core can't trade or plan them, and nothing scans wider.
- **Proposed requirements:**
  - **UN-1 Pre-market in-play scan** (8:30–9:25) across the top 50 S&P 500 names: gap %, pre-market volume vs average, news flag. The game plan lists the top 5 as "watch, not tradable".
  - **UN-2** A governed path to rotate them into the tradable list. A1's weekly check proposes; the owner confirms; the change applies the next week. It stays within the 30-symbol stream limit.

### G11. No volatility regime

- **Problem:**
  - Sizing and setups are the same on a quiet day and a panic day.
  - "Trend days only" (the power-hour breakout) is not defined in code terms. A day trader adjusts size and setup choice to the regime.
- **Proposed requirements:**
  - **RG-1** Daily regime from SPY: ATR(14) against its 60-day average, plus the opening gap. VIXY can serve as a proxy, since Alpaca has no index data.
  - **RG-2** High-volatility regime → half risk per trade, and no gap-and-go above 2× normal gap.
  - **RG-3** Define a trend day objectively, for example price above VWAP for ≥ 80% of 5-minute closes since 9:30 and making higher highs. Power-hour setups require it.

### G12. Slippage is monitored but never acted on

- **What the requirements say:** MONITORING.md tracks slippage and fill rate and warns.
- **Problem:** nothing changes when slippage eats into R. A setup whose average slippage is 0.15R has a different expectancy than its backtest shows.
- **Proposed requirement:** **EXQ-1** Expectancy per setup is computed *after* actual slippage. If slippage is above 0.1R on average over 10 trades, a setup automatically switches to entering at a better (passive) limit price, or returns to paper. Cancelled entries (EX-3) are counted as missed trades, not ignored.

### G13. Alert fatigue

- **Problem:**
  - 22 symbols × 7 signal types, all day, with only per-signal cooldowns.
  - Midday alerts (10:30–15:00) are noise for this playbook, which has no midday entries.
- **Proposed requirements:**
  - **AL-4** An alert schedule: `windows` (default; pre-market, 9:30–10:30, 15:00–16:00) or `all_day`.
  - **AL-5** A per-symbol cap of 3 alerts per window.
  - **AL-6** Alerts are ranked by quality (RVOL, alignment with QQQ, distance from resistance). Only the top N go out; the rest go into a 10-minute digest.

---

## P3: quality of life and completeness

| ID | Gap | Proposal |
| --- | --- | --- |
| G14 | Alerts are not graded (ideas and options are) | **AL-7** Grade every alert type: what share reached +1R before −1R within 30 minutes, by type and time of day. Turn off the types that don't work |
| G15 | No owner journal | **UI-29** Notes, tags (FOMO, hesitated, early exit) and a screenshot per trade in the console, reported weekly beside the A/B/C grades. Self-review is the habit that improves a discretionary trader |
| G16 | Wash sales in other accounts only produce a warning (WS-6) | **WS-9** Import trades from other brokers (CSV: Robinhood, Webull, Fidelity) so their losses also lock tickers here; warn before an IRA purchase of a locked ticker |
| G17 | No "what's moving now" view | **UI-30** A live scanner panel in the console: watchlist ranked by % change, RVOL, relative strength vs QQQ and distance from VWAP; click through to the stock page |
| G18 | One notification channel | **NT-3** If Telegram is unreachable for 2 minutes during a window, approve mode pauses new entries (no silent auto-reject), and a fallback e-mail or desktop toast tells the owner. Today it depends only on the notifier heartbeat |
| G19 | Options advice ignores trigger latency | **OP-20** Prefetch the option chain when a setup is pre-armed (LAT-2), so the suggestion arrives with the stock alert, not after it |
| G20 | Tax awareness | **TX-1** Monthly report: realized short-term gains and an estimated tax set-aside at a rate the owner configures (day-trading gains are taxed as ordinary income) |
| G21 | Real-history backtests of the playbook | **BT-x** Run the playbook on 2 years of real minute bars (`trader fetch-history`) alongside synthetic days. Report results by setup before paper trading starts, so the paper phase confirms them rather than discovering them |

---

## What is already strong (keep)

- Hard limits in code (§6), never changeable by a prompt; changes apply only the next day.
- A checklist with "no setup, no trade" and no minimum trade count.
- Bracket orders with broker-side stops from the moment of fill; never widen a stop; never average down.
- A strict, two-way wash-sale process, including the IRA warning.
- Journal-first design: every decision is reproducible; reports take numbers from the journal, not the model.
- Staged gates: replay → backtest → paper → live, with setups returning to paper on negative expectancy.
- Next-day ideas graded against the next session.

## Suggested order

1. **Before paper trading is used to judge the strategy:** G1, G2, G6, then G3/G4 (alerts consistency).
2. **During paper trading:** G7 (shadow trades: fastest way to learn), G5, G8, G9.
3. **After the first month of paper data:** G10–G13, then the P3 items and the console (UI phases P1–P2).

Each accepted gap becomes requirement text in REQUIREMENTS.md (or the alerts, options or UI document), with a row in TRACEABILITY.md and tests when built.
