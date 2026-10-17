# Reliability, Trade Lifecycle and Analysis Requirements

**Version:** 0.1 (October 17, 2026) · Status: **specified, not built** (every ID is `Planned` in [TRACEABILITY.md](TRACEABILITY.md)) · Follows [IDEAS_PLATFORM_GAPS.md](IDEAS_PLATFORM_GAPS.md) and [GAP_REVIEW.md](GAP_REVIEW.md)

This document specifies what the system needs to become a **reliable and consistent** trade alert system. It covers four areas, all equally important:

1. **Evidence and infrastructure:** the reasons it is not yet reliable, and the requirements that would fix them (§2, §3).
2. **Every way a trade can go wrong:** a catalogue of failure scenarios, each with how it is detected, the alert it raises and the required response (§4).
3. **The trade lifecycle from the trader's side:** entering or avoiding (§6), stop-loss alerts (§7) and riding a winner (§8), with the human factors that decide whether the plan is followed (§9).
4. **Analysis gaps:** what is still missing to analyse a stock fully (§10).
5. **Learning from outcomes:** every trade gets a cause. It may be a technical error, an execution or human mistake, stock news, a sector, macro or market event, a regime mismatch, a stock that did not behave as predicted, or plain variance. Lessons become tested rules, never silent tuning (§11).
6. **Decision confidence:** a confidence grade on every alert, size that follows the evidence, statistics that respect clustering, a capital plan, a comparison with doing nothing, and the expected range of outcomes before the first trade (§12).

**Scope.** The engine's own trades already have most lifecycle protections: the entry checklist CK-1..CK-10, no-chase CK-6 and LAT-3, bracket orders EX-2, stop-market stops EX-5, exit rules XR-1..XR-6, co-pilot tracking CP-2, and shadow trades SH-1. The main gap is that **the advisory alerts the owner trades by hand** do not get the same protection: momentum (A19), the scanner, events (A20), catalysts (A17) and options suggestions. These requirements apply to every alert source unless a row says otherwise. Where an existing rule already covers a case, it is cited, not repeated.

**Alert priorities used below.**

| Priority | Meaning | Delivery |
| --- | --- | --- |
| **P1: act now** | Money is at risk this minute: a stop hit, a gap below a stop, a missing broker stop, a halt on a held stock | Bypasses caps, digests and quiet hours. Sent on two channels and repeated every 60 s until acknowledged (`ACK <id>`) or the position is flat (IR-3) |
| **P2: act today** | A decision is due before a deadline: a trigger, a raised stop, a trail exit at the next open, earnings ahead | One channel, inside the alert caps, with the deadline in the message |
| **P3: information** | No action: hold status, watchlist, reports | Digest |

## 1. Why it is not the most reliable system today

| # | Finding | Evidence |
| --- | --- | --- |
| W-1 | **No forward record.** Every number is a backtest. The paper stage (M8) has not started. | BUILD_STATUS |
| W-2 | **The proven edges are small.** The only robust swing setup is `DONCHIAN_55` (+0.15R). The swing alert stream made 8.2% a year, below SPY. The rotation model's edge over equal weight is pre-tax, on a survivorship-biased list. The catalyst tilt and the event patterns show no edge. | MOMENTUM §9, NEWS_CATALYST, EVENT §4 |
| W-3 | **Siloed modules.** The scanner, momentum, catalysts, events and options each alert on their own, can contradict each other, and grade by different outcomes. | IDEAS_PLATFORM_GAPS G-01, G-15, X-2 |
| W-4 | **Advisory trades are not tracked from the owner's fills.** Momentum follows a theoretical position. Scanner and event ideas are not followed at all after the alert. | §6–§8 below |
| W-5 | **Concentration.** 10 tradable mega-cap tech names: most alerts are one factor bet. | REQUIREMENTS §2 |
| W-6 | **Operations on a desktop.** Docker Desktop on Windows has crashed and drifted its clock here. Nothing reports a job that did not run. | TROUBLESHOOTING §1, A10 drift |
| W-7 | **Gates flap, and the out-of-sample half wears out.** | IDEAS_PLATFORM_GAPS X-4 |
| W-8 | **Results are before tax and price-only, and are not compared with the owner's actual fills.** | X-6, X-7 |

## 2. Evidence and consistency (RL)

| ID | Pri | Requirement |
| --- | --- | --- |
| `RL-1` | P0 | **One signal ledger.** Every module writes every candidate to one `signals` table: source, rule and version, ticker, side, entry plan, stop, targets, time stop and the outcome contract (RL-2). Each signal gets an immutable id and every state change (§5). Rejections are kept with their reason codes, as NT-1 does for the engine. Nothing is deleted or overwritten. The module tables (`momentum_signals`, `event_ideas`, `catalyst_outlooks`, `option_suggestions`) link to it. |
| `RL-2` | P0 | **Outcome contract per strategy family.** Each rule declares its target (+1R before −1R; positive net P&L at exit; return beyond SPY over N sessions), its entry and exit policy, how same-bar ambiguity is resolved (stop first, as MO-9) and its timeout. A signal is graded only by its own contract, and the contract's version is stored with it. |
| `RL-3` | P0 | **Forward evidence before alerts.** Each alert stream runs as `paper` for a declared period before it may send entry alerts. Pass criteria are written into config before the period starts, for example: at least 50 graded swing signals; the 95% interval of mean net R above 0; the forward average R at least half the backtest's; drawdown inside the SV-2 Monte Carlo limit. A stream that fails stays `paper`. The criteria are never edited after the start: a new version starts a new period. |
| `RL-4` | P0 | **Lifecycle per strategy:** `research → paper → limited → full → paused → retired`. A transition needs its evidence (RL-3, RL-5), is journaled with who made it and why, and can be reversed automatically (RL-6). Only `limited` and `full` send entry alerts; `limited` sends them at half size. |
| `RL-5` | P0 | **Frozen holdout and hysteresis.** The most recent 12 months are held out and never used for choosing rules, factors or defaults. They are graded once per version change. A weekly gate switches a strategy off below its bar, and back on only above bar × 1.15 for two consecutive grades. |
| `RL-6` | P1 | **Automatic demotion.** A `limited` or `full` stream moves to `paused` when, over its last 30 forward signals, mean net R is below 0 with the upper end of its interval below +0.1R, or its drawdown passes the SV-2 limit, or its data inputs fail (IR-7). Resuming takes an owner command and a new paper period. |
| `RL-7` | P0 | **One idea per ticker per day.** Signals from all modules on the same ticker and day are consolidated into one alert. It shows the agreeing sources, the disagreeing ones and the strongest record. A caution (an event `TRIM_BEFORE`, a negative catalyst, earnings) against a buy turns the alert into an *avoid* unless the buy's own contract was graded with that condition present. |
| `RL-8` | P1 | **Rule version on every alert.** An open position keeps the rules of its entry (stop logic, trail, time stop) even when config changes later, unless the change is a safety tightening. The alert and the ledger show the version. |
| `RL-9` | P1 | **Scorecard per stream:** backtest, paper (theoretical fills), the owner's fills (RL-10) and live, side by side. Each shows trades, win rate, average and median net R, profit factor, drawdown, average hold, slippage against plan, after-tax return (RL-11) and a total-return benchmark. Shown on the console and in the weekly report. |
| `RL-10` | P0 | **The owner's fills are the truth.** Fills come from the Alpaca account, from the `FILLED` command (CP-2) or from an import (WS-9). They are matched to the signal that prompted them. Unmatched fills are flagged as discretionary. Grading reports theoretical and actual results separately. |
| `RL-11` | P1 | **After tax and total return.** Reports give returns after an owner-set short-term and long-term rate (TX-1 rates), with the holding period of each lot. Benchmarks are total-return (dividends reinvested). Bars used for long-horizon grading are dividend-adjusted. |
| `RL-12` | P1 | **Cost realism.** Costs by liquidity tier: spread from the quote history, plus slippage scaled to size against average volume. Every gate is also run at 2× costs, and a stream that fails at 2× is marked fragile. |
| `RL-13` | P1 | **Uncorrelated edges.** New strategy families come before new variants of momentum: trend-following on liquid ETFs across asset classes (equity, bond, commodity, currency ETFs), and short-term mean reversion. All of them go through the same gates. The correlation of each stream's returns with the others is reported, and the alert portfolio caps one family's share of open risk at 50%. |
| `RL-14` | P2 | **Universe breadth.** The tradable universe is configurable beyond the 10 names, with a sector cap: at most 40% of open risk in one sector (sector map: TA-4). |
| `RL-15` | P1 | **Point-in-time data for grading.** Backtests use a constituents history that includes delisted names (D1; a paid source if needed), and total-return adjusted prices. Until then, every result keeps the survivorship caveat and the equal-weight comparison (MO-1). |

## 3. Infrastructure and delivery (IR)

| ID | Pri | Requirement |
| --- | --- | --- |
| `IR-1` | P0 | **Always-on host.** Production runs on a Linux host with Docker Engine (not Desktop), a UPS, automatic restart and NTP time sync. The Windows desktop stays a development machine. |
| `IR-2` | P0 | **Missed-job check-ins.** Every scheduled job pings an external check-in service when it finishes. Covered jobs: the 16:50 momentum close, the 5-minute intraday monitor, the 17:05 events job, the 07:30 fundamentals, the news poll and the morning plan. A missing ping sends a P1 to the owner's phone on a channel independent of the host. |
| `IR-3` | P0 | **Alert delivery guarantees.** P1 alerts go to two channels (Telegram and a push or SMS service) and are repeated every 60 s until acknowledged or resolved. Every alert has a delivery receipt in the ledger. Delivery latency (signal to receipt) is measured; p95 above 30 s for P1 or 5 min for P2 is an incident. |
| `IR-4` | P0 | **Fail closed on inputs.** Each input has a freshness limit: intraday quotes 2 s for orders (DQ-8) and 2 min for monitoring; daily bars, the final session by 17:30 ET; the earnings calendar 4 days (MO-18); news 30 min in the window. A stale input blocks the alerts that depend on it and says so. It never produces an alert from old data. |
| `IR-5` | P1 | **Catch-up after downtime.** A job missed because the host was down runs on restart. Its alerts are labelled `LATE` with the original time, and deadlines that passed are shown as passed (a buy-stop window that closed is not re-sent as live). |
| `IR-6` | P0 | **Owner position reconciliation.** Every 5 minutes in market hours, and at 9:25 and 16:05 ET, the owner's broker positions and open orders are compared with the ledger. Any mismatch is P1: a position with no protective stop (SL-2), a stop for a position that no longer exists, a share-count difference, or an unknown position. |
| `IR-7` | P1 | **Calendar and reference expiry.** Warn 60 days before a hand-kept calendar ends (the macro calendar, corporate events), and when an estimated event date is still unconfirmed 10 sessions before its window. A calendar that has ended disables the features that need it. |
| `IR-8` | P1 | **Idempotent jobs.** Re-running a job for the same session changes nothing and sends nothing twice (alert keys, as MO-15). |
| `IR-9` | P1 | **Config change log.** Every config change is versioned, journaled and diffed in the next daily report. It never applies retroactively to open positions (RL-8). |
| `IR-10` | P1 | **Clock check.** Host clock drift above 1 s against NTP is an incident, and above 5 s pauses alerts that depend on time windows. |
| `IR-11` | P2 | **Second data source for prices.** End-of-day closes are cross-checked against a second source (DQ-6 style). A difference above 0.5% marks the bar suspect, and alerts on that ticker wait until the bars agree or the owner confirms. |

## 4. Every way a trade can go wrong (FM)

For each scenario: how it is detected, the alert, and the rule. "Held" means a position the owner holds (from fills, RL-10) or a tracked paper position.

### 4.1 Before entry

| ID | Scenario | Detection | Alert | Rule |
| --- | --- | --- | --- | --- |
| `FM-1` | Price falls through the stop level before the trigger fires | Last trade ≤ invalidation price while armed | P2 `INVALIDATED: do not enter` | Signal → `INVALIDATED`; the buy-stop order is cancelled (EN-6) |
| `FM-2` | Gap open above the no-chase limit | Opening print > max entry (EN-1) | P2 at 9:31 `TOO LATE: skip` | Signal → `SKIPPED` (MO-8 rule, for every source) |
| `FM-3` | Gap open below the stop | Pre-market quote, then the open, < stop | P2 `INVALIDATED` | Same as FM-1 |
| `FM-4` | Earnings date announced or moved into the hold window | Calendar change (daily) | P2 `AVOID: earnings <date> inside the plan` | Re-checked at arming and at trigger (EN-8, EV-10) |
| `FM-5` | Material news after the alert (guidance, downgrade, legal, management change, offering) | A3 story for the ticker, strength ≥ NC-4 | P2 with the headline | Entry needs an owner re-confirm; a negative catalyst blocks it (RL-7) |
| `FM-6` | Market regime flips (SPY under its 200-day, a high-vol day, a RISK_OFF macro label) | MO-6, RG-1, A18 | P2 | New entries follow the regime rules; the alert says which rule applied |
| `FM-7` | Sector breaks down (the sector ETF under its 50-day, or the ticker's relative strength collapses) | TA-4 | P3 → P2 if armed | Armed signals on that sector are held for owner confirmation |
| `FM-8` | Trigger hit on weak volume (a probable false breakout) | Volume at the trigger below the setup's graded threshold (TA-16) | P2 `weak trigger` | Enter only if the setup's record holds for weak-volume triggers; else skip |
| `FM-9` | Trigger hit in the last 10 minutes of the session | Time of trigger | P2 | Follow the setup's graded rule for late triggers (enter at the close, or skip) |
| `FM-10` | Trigger reached only in pre-market or after hours | Extended-hours quote | P3 | Not a trigger: regular-session prints only |
| `FM-11` | A correlated position is already open | MO-17 / PF-2 on the owner's holdings | P2 `correlated with X` | Size down or skip, by rule |
| `FM-12` | Heat or position caps already used | PF-1, MO-17 on actual holdings | P2 | Skip, with the reason |
| `FM-13` | Ex-dividend date inside the hold (price drops by the dividend) | Ex-date calendar (TA-9) | P3 | Stops are not adjusted for normal dividends; the alert notes the expected drop |
| `FM-14` | Halt or LULD pause on an armed ticker | Status stream (MF-5) | P2 | No entry until 10 minutes after the resume, and a re-check |
| `FM-15` | The alert arrives late (delivery delay) | Receipt time − signal time (IR-3) | Label `DELAYED n s` | If price has passed the no-chase limit meanwhile, it is sent as `TOO LATE` instead |
| `FM-16` | Conflicting alerts from two modules | RL-7 | One consolidated alert | Default to avoid on a conflict |
| `FM-17` | Account limits: day-trade count rules, unsettled cash (T+1), buying power | Broker account and §6.5 rules | P2 | Size to what the account allows, or skip; never a free-riding violation |
| `FM-18` | Ticker locked (wash sale, loss streak, kill switch) | CK-1 | P3 | No entry alert |
| `FM-19` | The owner is unavailable (no acknowledgement of a P2 within its deadline) | IR-3 receipts | none | Treated as not entered; shadow-graded (EN-9) |
| `FM-20` | Analysis incomplete for the ticker (new listing, missing data) | TA-19 coverage | P3 | No entry alert unless its critical components are present |

### 4.2 At entry

| ID | Scenario | Detection | Alert | Rule |
| --- | --- | --- | --- | --- |
| `FM-21` | Fill far from the plan (slippage) | Owner fill vs trigger | P2 if R changed > 25% | Recompute R and size; if the stop is now under 0.25 ATR away or over the max loss, say so (EN-7) |
| `FM-22` | Partial fill | Fill qty < plan | P3 | Stop and targets apply to the filled quantity; the remainder's order is cancelled at the window end |
| `FM-23` | Order rejected (buying power, halts, price bands) | Broker response | P2 | Reason shown; no automatic retry at a worse price |
| `FM-24` | Entered without a protective stop | IR-6: position without a stop order | **P1** `NO STOP AT BROKER` | Repeat until a stop order exists (SL-2) |
| `FM-25` | Wrong size (fat finger) or wrong ticker or side | Fill vs signal | **P1** if risk > 1.5× plan | Show the excess risk and the shares to sell to get back to plan |
| `FM-26` | Broker outage during entry | Broker API errors / status page | P1 | Instructions for the broker's phone desk; the ledger marks the position unverified |

### 4.3 In the trade

| ID | Scenario | Detection | Alert | Rule |
| --- | --- | --- | --- | --- |
| `FM-27` | Stop hit intraday | Last trade ≤ stop (SL-4) | **P1** `STOP HIT: SELL ALL` | Broker stop expected to fill; a fill is confirmed, and no fill within 60 s is escalated |
| `FM-28` | Gap through the stop overnight | Pre-market quote < stop (SL-5) | **P1** before the open | Sell at the open (EX-5 rule); the loss beyond −1R is reported as gap slippage |
| `FM-29` | Trading halt on a held stock (news pending, LULD) | Status stream | **P1** | Explain the reopen process; re-check the stop at the reopen; no new orders while halted |
| `FM-30` | Market-wide circuit breaker (7%, 13%, 20% S&P 500 declines) | Index level, market status | **P1** | Hold the plan: broker stops stay; no new entries that day |
| `FM-31` | Flash crash or bad print hits a stop | Price outside the ±10% band of the 1-min VWAP, reverting within 5 min | P2 post-event | Report it; stops stay stop-market (EX-5). A broken trade (exchange cancels) is reconciled (IR-6) |
| `FM-32` | Surprise news while holding (pre-announcement, guidance cut, downgrade, legal) | A3 story on a held ticker | P2 (P1 if price moves > 2 ATR) | The stop does the work; no discretionary sale advice. Earnings pulled forward triggers the event policy (RD-9) |
| `FM-33` | Merger or acquisition announced (the stock becomes pinned) | News and a price-range collapse | P2 | Exit rule: a pinned stock has no momentum; sell at the next open unless the owner keeps it as an arbitrage hold |
| `FM-34` | Stock split or reverse split | Corporate-action feed / price ratio | **P1** `RE-ENTER YOUR STOP` | Levels are rescaled; brokers may cancel or adjust open stops on corporate actions, so the stop is re-verified (SL-9) |
| `FM-35` | Special dividend, spin-off or rights issue | Corporate-action feed | P2 | Levels adjusted by the ex-date factor; the stop is re-verified |
| `FM-36` | Secondary offering or lock-up expiry (new listings such as SPCX) | News, the lock-up calendar (TA-9) | P2 ahead of the date | The policy is set at entry: hold, tighten or exit before the date |
| `FM-37` | Index deletion or addition | Index announcement calendar (TA-9) | P3 | Context; no rule change unless a graded study says so |
| `FM-38` | ADR home-market shock (TSM: Taiwan session, Taiwan holidays, geopolitics) | TWSE close vs the ADR's prior close (TA-13) | P2 pre-market if the implied gap > 1 ATR | Gap-risk check (PF-4) uses the home-market move |
| `FM-39` | The trade stalls (no progress) | Time stop (XR-5, MO-9) | P2 on the day it fires | Exit at the next open |
| `FM-40` | Correlated holdings all drop together | Open-risk sum (PF-1) on actual holdings | P2 if heat > the cap after a move | No adding; stops stay; no new entries until heat is back under the cap |
| `FM-41` | Climax run (extended far above the averages) | MO-9 climax | P2 `SELL HALF at the open` | As MO-9, for every source |
| `FM-42` | The stop order expires at the broker (GTC limits, for example 90 days at Alpaca) | Order age | P2 5 sessions before | Re-enter the stop |
| `FM-43` | The owner widens or cancels the stop | IR-6 order check | **P1** | DR-1 forbids it; the alert repeats until a stop at or above the plan exists |
| `FM-44` | The owner adds to a loser | Fill on a held ticker below entry | **P1** | DR-2 forbids it; shows the risk now at stake |

### 4.4 At exit

| ID | Scenario | Detection | Alert | Rule |
| --- | --- | --- | --- | --- |
| `FM-45` | Exit rule fired but the owner did not sell | Rule event + position still open after its deadline | Repeat P2, then P1 at the deadline | Escalate until done or overridden (an override is logged as discretionary, HU-2) |
| `FM-46` | Owner sells before any rule (early exit) | Sell fill without a rule event | P3 confirmation | Logged as discretionary; the rule's outcome keeps being graded as a shadow (RD-2) |
| `FM-47` | Trail exit decided on the close, then a gap the next morning | Close-basis rule + pre-market | P2 at 15:45 (SL-7) and pre-market | Sell at the open as planned; the gap is reported |
| `FM-48` | Exit into poor liquidity (pre-market, the first minute, a halt reopen) | Time and spread | P3 | Exits use marketable limits after the first minute, except stop-market stops (EX-5) |
| `FM-49` | Target hit after hours | Extended-hours prints | P3 | Not acted on; regular-session rules only |

### 4.5 System and data

| ID | Scenario | Detection | Alert | Rule |
| --- | --- | --- | --- | --- |
| `FM-50` | A scheduled job did not run | IR-2 check-in | **P1** | Catch up (IR-5) |
| `FM-51` | Stale or partial bar treated as final | Bar end-time and volume checks (MO-22 rule, everywhere) | P2 | Wait for the final bar |
| `FM-52` | Bad print or unadjusted corporate action in the history | Jump guard (MO-22), second source (IR-11) | P2 | Ticker suspect until resolved |
| `FM-53` | Market calendar error (holiday, half day) | A10-1, XNYS calendar | P2 | Half days shift the close jobs |
| `FM-54` | Database, notifier or model server down | A10-2, NF-1 | P1 | Fail closed (IR-4); broker stops keep protecting |
| `FM-55` | Duplicate alerts after a restart | Alert keys (IR-8) | none | Never sent twice |
| `FM-56` | Config changed with positions open | IR-9 | P3 | Open positions keep their entry rules (RL-8) |
| `FM-57` | Clock drift | IR-10 | P2 / P1 | Pause time-window alerts |
| `FM-58` | Alert storm (many signals at once) | Caps (AL-5) | Digest | P1 never suppressed; P2 ranked (AL-6) |

### 4.6 The human side

| ID | Scenario | Detection | Alert | Rule |
| --- | --- | --- | --- | --- |
| `FM-59` | Alert not seen | No acknowledgement (IR-3) | Escalation | P1 repeats; P2 expires as `not entered` |
| `FM-60` | FOMO entry after a missed trigger | Fill above max entry, or after expiry | P2 `outside the plan` | Logged as discretionary; half-size rule (CK-4) and a stop are still required |
| `FM-61` | Revenge trade after a stop-out | New fill on the same ticker within the cool-off (HU-3) | P2 | Cool-off rule |
| `FM-62` | Over-sizing | Risk per trade > plan | P1 if > 1.5× | FM-25 |
| `FM-63` | Overtrading after losses | Trades per day / losses in a row (LL rules) | P2 | Loss-streak lock applies to manual trades too (HU-3) |
| `FM-64` | Holding a loser past its stop ("hope") | FM-27 unresolved | P1 | Repeat until flat |
| `FM-65` | Selling a winner too early ("fear") | FM-46 | Weekly cost report | RD-2 |

## 5. Signal lifecycle (applies to every source)

| ID | Pri | Requirement |
| --- | --- | --- |
| `LC-1` | P0 | **States:** `CANDIDATE → ALERTED → ARMED → ENTERED → MANAGING → EXITED`, with the side exits `REJECTED` (reason code), `AVOID`, `INVALIDATED`, `SKIPPED`, `EXPIRED` and `NOT_ENTERED`. Each transition is a ledger event (RL-1) with its time, price and cause. |
| `LC-2` | P0 | **Theoretical and actual tracks.** Every alerted signal is followed in two tracks: the plan's theoretical fills, and the owner's actual position (RL-10). Each track is graded by the contract (RL-2). |
| `LC-3` | P0 | **Monitoring coverage.** Every `ARMED` and `MANAGING` signal is checked every 5 minutes in market hours (MO-15 cadence), pre-market at 8:00 and 9:00 ET for gaps, and at 15:45 ET for close-basis rules. |

## 6. Entering, or avoiding (EN)

| ID | Pri | Requirement |
| --- | --- | --- |
| `EN-1` | P0 | **A complete entry plan on every buy alert:** the valid-from and valid-until sessions; the trigger; the **max entry** (trigger + 0.5R, the no-chase limit); the **invalidation price** (the stop: if traded before the trigger, the idea is void); the stop; the first target; the trail rule; the time stop; and the size (shares, $ at risk, % of equity, after the caps). |
| `EN-2` | P0 | **A ready order ticket.** The alert gives the exact order to place in advance, so the owner does not have to watch: a buy stop-limit (stop = trigger, limit = max entry), with an attached stop-loss order where the broker supports it. Day or GTC duration is set to the valid-until session. |
| `EN-3` | P0 | **Pre-session plan** (8:45 ET): every armed idea for the session with its ticket. Ideas newly invalid since the alert are marked `AVOID` with the reason (FM-1..FM-7). |
| `EN-4` | P0 | **Trigger alert** (P2) when the trigger trades: `ENTER: within plan` with the live price and the room to max entry. It becomes `TOO LATE: do not chase` once price is above max entry, and `INVALIDATED` if price trades at the invalidation level first. |
| `EN-5` | P0 | **Expiry.** An idea not triggered by its valid-until close is `EXPIRED`, and the owner is told in the evening report, never silently. |
| `EN-6` | P0 | **Avoid alerts.** Each idea that the checks turn down after it was alerted gets an explicit `AVOID` with the reason: earnings or another event in the window, a negative catalyst, a regime or sector change, a correlation or heat cap, liquidity, a halt, or incomplete analysis. The owner is told to cancel any resting order. |
| `EN-7` | P0 | **Entry confirmation.** On the owner's fill, the plan is recomputed from the actual price: R, the shares against caps, the stop distance in ATR. A fill that breaks a rule (above max entry, stop now too close or too far) is flagged at once. The confirmation repeats the stop to place (SL-1). |
| `EN-8` | P0 | **Checks at the trigger, not only at the alert:** earnings and events (EV-10, MO-18), news in the last hour, spread and quote age (MF-4, MF-6), halts, the live regime, sector, correlation and heat on actual holdings, account limits (FM-17). |
| `EN-9` | P1 | **A missed entry gets a protocol, not a chase.** A triggered idea the owner did not take is followed as a shadow (SH-1 style). A *second chance* alert is sent only when a rule defines one: price returns to within 0.25 ATR of the trigger within 3 sessions, with the stop intact and the setup's graded record holding for second-chance entries. Otherwise the idea is closed as `NOT_ENTERED`, with a short "no action: do not chase" note. |
| `EN-10` | P1 | **Re-entry after a stop-out** needs a fresh setup from a rule, not the same alert. The cool-off is HU-3. |
| `EN-11` | P1 | **Gap-risk check for overnight holds** (swing): if the stock's 99th-percentile adverse overnight gap (PF-4) or its options-implied move to the next event (TA-6) is larger than the stop distance, size down to the stressed loss, or avoid. |
| `EN-12` | P2 | **Entry timing studies.** Each setup's record is split by trigger type (gap through, opening range, intraday break, close) and time of day. Entry rules follow what the record supports (FM-8, FM-9). |

## 7. Stop-loss alerts (SL)

| ID | Pri | Requirement |
| --- | --- | --- |
| `SL-1` | P0 | **Every alert states its stop three ways:** price, % from entry, and $ at risk for the size. The entry confirmation (EN-7) asks the owner to place it at the broker as a **stop-market GTC** order (EX-5). Mental stops are not a supported mode. |
| `SL-2` | P0 | **Broker-stop verification.** For Alpaca-held positions, IR-6 checks that a sell stop exists for the full quantity at or above the plan's stop. Missing, short or below plan → **P1** `NO STOP AT BROKER`, repeated until fixed. For other brokers, the owner confirms with `STOP <TICKER> <PRICE>`; without a confirmation within 15 minutes of the fill, it escalates to P1. |
| `SL-3` | P1 | **Approaching the stop: information, not advice.** At −0.75R a P3 note says "near the stop; the stop does the work; no action". It is sent once per position per day. It must never suggest selling early. |
| `SL-4` | P0 | **Stop hit.** When the last trade is at or below the stop, a **P1** `STOP HIT: SELL ALL` goes out with the price, the R and the $ result. If the broker stop filled, it is confirmed. If the position is still open 60 s later (no broker stop, or a gap), it repeats every 60 s and adds the marketable-limit instruction. |
| `SL-5` | P0 | **Gap below the stop.** Pre-market at 8:00 and 9:00 ET, a swing position quoted below its stop gets a **P1**: "will open below the stop; sell at the open", with the estimated loss in R (EX-5). |
| `SL-6` | P0 | **Raise-only stop changes.** Break-even moves, trail raises and the partial-profit stop are P2 `RAISE STOP to X`, with the order change to make. A broker order below the new level gets a P1 after the next session's open (IR-6). A stop is never lowered (DR-1). |
| `SL-7` | P1 | **Close-basis rules get a warning before the close.** For trails decided on the close (EMA10, EMA20, SMA50, the daily 20-EMA failed-trade exit), at 15:45 ET a P2 says: "if it closes below X, the rule sells at the next open; you can sell at the close instead". The next morning's alert confirms what the rule decided. |
| `SL-8` | P0 | **Halts.** A halt on a held stock is P1 (FM-29). At the reopen, the stop is re-checked, and if price reopens below it, SL-4 applies. |
| `SL-9` | P0 | **Corporate actions.** On a split, reverse split, special dividend or spin-off, every level is rescaled by the adjustment factor, and the owner is told to verify or re-enter the broker stop: **P1** on the ex-date pre-market (FM-34, FM-35). |
| `SL-10` | P1 | **Stop order expiry.** A GTC stop older than the broker's limit minus 5 sessions gets a P2 to re-enter it (FM-42). |
| `SL-11` | P1 | **Daily stop audit** (evening report): each open position with its stop, the distance in % and ATR, the $ at risk, the gap-stress loss (PF-4), and whether the broker order is verified. |
| `SL-12` | P1 | **After a stop-out:** the result is journaled. The cool-off (HU-3) starts, and the re-entry rule (EN-10) is stated in the stop-hit alert itself. |
| `SL-13` | P0 | **Delivery.** All stop alerts are P1 or P2 and follow IR-3. A stop alert is never digested, capped or delayed by quiet hours. |

## 8. Riding the position (RD)

| ID | Pri | Requirement |
| --- | --- | --- |
| `RD-1` | P0 | **A daily "hold" message for every open position** (after the close, P3). It gives the R so far, the current stop and trail level, the exact rule that would sell and how far away it is (in % and ATR), the time-stop date, and the next event. It says plainly: **"No action. The plan is to hold."** Most days the right action is nothing, and the system says so. |
| `RD-2` | P0 | **Exits come only from rules.** The system never suggests a sale outside a rule event. A sale before any rule fires is logged as discretionary, and the rule's outcome keeps being graded as a shadow. The weekly report shows the **cost of early exits** (shadow R minus realized R) and the cost of ignored exits. |
| `RD-3` | P0 | **Expectations from the record.** The entry alert and the hold messages show the setup's winners' history. Typical heat: the median and 80th-percentile adverse move (MAE) of trades that ended as winners. Typical run: the median favourable move (MFE) and the hold length of winners. A pullback inside the winners' usual heat is labelled "normal for this setup; within plan". |
| `RD-4` | P0 | **Partial profits set at entry.** The partial rule (for example 1/3 at +2R, MO-9; half at 1.5R, XR-2) is in the plan from the start, so the rest can ride with the stop at break-even. The partial alert repeats that the remainder rides on the trail. |
| `RD-5` | P1 | **Trail levels published each evening** for the next session, raise-only. Intraday trails (chandelier, Donchian) are live levels with SL-6 alerts. |
| `RD-6` | P1 | **Adding to winners** (optional, off by default). Only on a fresh rule-defined setup while the position is above +1R, with the stop on the original shares at or above break-even, and the combined open risk within the per-trade cap. Never add to a loser (DR-2). |
| `RD-7` | P1 | **Events during the hold are decided at entry.** The policy for earnings and other events inside the hold (exit before, reduce, or hold) comes from the setup's graded record and is stated in the entry alert. A P2 comes 3 sessions before the event, repeating the policy (XR-6 for the engine). |
| `RD-8` | P1 | **Regime changes do not force exits.** Exits ignore the regime (MO-6). The hold message notes the regime, so a falling market does not trigger a sale outside the rules. |
| `RD-9` | P1 | **Re-entry after a trail exit:** if the trend resumes, a fresh setup alert is sent (EN-10). The trail exit alert says this is possible, to lower the urge to hold through a broken trail. |
| `RD-10` | P2 | **Milestones.** At +1R, +2R and +3R, a P3 note gives the new stop, the open profit protected, and the setup's share of winners that went on to the next milestone. |

## 9. Human factors (HU)

| ID | Pri | Requirement |
| --- | --- | --- |
| `HU-1` | P1 | **Take or pass.** Each buy alert can be answered `TAKE <id>` or `PASS <id> <reason>`. Passes are kept and shadow-graded, as SH-1 does for the engine, so the owner sees what passing cost or saved. |
| `HU-2` | P1 | **Discretion log.** Every action outside the plan (early exit, ignored exit, FOMO entry, a stop widened) is logged with its R impact. The weekly report shows the **adherence rate** and the cost of each kind. |
| `HU-3` | P1 | **Cool-off and loss-streak rules apply to manual trades:** no re-entry on a ticker for 2 sessions after a stop-out, and LL locks on daily, weekly and monthly losses, counted from the owner's fills. |
| `HU-4` | P1 | **Daily limits on new positions** and the heat cap (PF-1) on actual holdings, across all sources. |
| `HU-5` | P2 | **Quiet hours** for P2 and P3 only, never for P1. |
| `HU-6` | P2 | **Plain messages.** Every alert's first line is the action: `ENTER`, `AVOID`, `HOLD`, `RAISE STOP`, `SELL ALL`, `SELL 1/3`, `TOO LATE` or `NO ACTION`. Details follow. |

## 10. Analysis gaps for a stock (TA)

What is still missing to analyse a ticker fully. Each component feeds ideas as context first. It becomes a rule only after it improves a walk-forward result (MR-2).

| ID | Pri | Requirement |
| --- | --- | --- |
| `TA-1` | P1 | **Multi-timeframe.** Weekly trend (weekly close against the 10- and 30-week averages, weekly relative strength) on every daily idea. A daily buy against a falling weekly trend is flagged, and graded both ways. |
| `TA-2` | P1 | **Support and resistance zones for swing trades.** Swing pivots plus a volume profile over 60 and 120 sessions: the point of control (POC), the value area, and high- and low-volume nodes. Used for the room to the next resistance (the CK-8 idea, extended to swing ideas) and for targets. |
| `TA-3` | P2 | **Anchored VWAP** from the last earnings day, the 52-week high and low, and the listing day for new issues. Shown as levels. |
| `TA-4` | P1 | **Sector and industry.** Every ticker mapped to a sector and industry (SEC SIC code, free) and a sector ETF (XLK, SMH, XLY, XLC, XLF, ...). Relative strength against the sector as well as against SPY; sector trend; the RL-14 sector cap. |
| `TA-5` | P1 | **Breadth and internals** from the 115-stock universe: the % above the 50- and 200-day averages, new highs minus new lows, an advance-decline line, and the distribution days already in TS-22. Shown with the regime (MO-6, A18). |
| `TA-6` | P1 | **Volatility state and implied moves.** ATR percentile, Bollinger-width percentile, and IV rank and percentile from `options_iv_daily` (A13). Before events, the options-implied move compared with the stop distance (EN-11). |
| `TA-7` | P1 | **Gap behaviour per ticker:** frequency and size of overnight gaps, the share filled the same day, and the adverse 95th and 99th percentiles. Used by EN-11, PF-4 and FM-28. |
| `TA-8` | P0 | **Liquidity for every ticker and size:** 20-day average dollar volume, the typical spread from the quote history, and the estimated impact of the planned size. Ideas whose size exceeds 1% of average volume (OC-2) are cut down or avoided. |
| `TA-9` | P0 | **One event calendar per ticker:** earnings (exists), ex-dividend dates, splits, index rebalance and reconstitution dates, monthly options expiration and quarterly triple witching, lock-up expiries (new listings), shareholder meetings, company events (A20) and macro days (A18). Every idea checks its window against it. |
| `TA-10` | P2 | **Short interest** as % of float and days to cover (FINRA, bi-monthly; D4). Context for squeeze and crowding risk. |
| `TA-11` | P2 | **Ownership and estimates:** insider trades (exists), quarterly 13F institutional changes (SEC, free), and the analyst estimate revision trend (Finnhub). Context. |
| `TA-12` | P2 | **Fundamental quality** for swing and position ideas: revenue and earnings growth, margins, free cash flow, debt and dilution, point-in-time from SEC XBRL company facts. A context flag ("unprofitable", "diluting"), graded before any use as a filter. |
| `TA-13` | P1 | **ADR handling (TSM).** The home-market close (TWSE) and its move since the ADR's last close; Taiwan holidays (the ADR trades while the home market is shut, so gaps are wider); the implied pre-market gap. Feeds FM-38 and PF-4. |
| `TA-14` | P1 | **New listings (SPCX).** Rules for short histories: no 200-day average until 200 sessions; IPO-base and anchored-VWAP-from-listing levels; the lock-up calendar. The idea states which components are unavailable (TA-19). |
| `TA-15` | P1 | **Intraday detail for daily triggers:** how the trigger traded (a gap through, an opening-range break, a mid-day break, the close), the relative volume at the trigger and the time of day, from 1-minute bars. Feeds FM-8, FM-9 and EN-12. |
| `TA-16` | P2 | **Seasonality per ticker and month**, with its sample size, labelled weak evidence. Context only. |
| `TA-17` | P1 | **Factor and cluster exposure of the open ideas:** momentum, growth, semis, mega-cap. Shown with the correlation caps (MO-17, PF-2). |
| `TA-18` | P1 | **Corporate-action and bad-print guard in every module:** the MO-22 jump guard, applied to the scanner, catalysts, events and options as well as momentum. |
| `TA-19` | P0 | **Analysis coverage on every idea:** which components are present and which are missing, for example "coverage 15/18: no short interest, no sector map, no IV". Critical components (price history for the rule, the earnings date, liquidity, the stop inputs) must be present, or the idea is not alerted (FM-20). |

## 11. Learning from outcomes (LN)

Every closed trade, theoretical and actual, wins as well as losses, gets a cause. The aim is to separate four things that look alike on a P&L line:

- **A bad decision:** a rule, data or execution error, or a deviation from the plan. Fix it.
- **A wrong prediction:** the process was right, but the stock did something the setup's history did not cover. Study it.
- **An outside shock:** stock news, a sector or macro event, or a market shock moved the price. Decide whether it was knowable at entry.
- **Expected variance:** a normal loss for a setup that wins 50% of the time. Change nothing.

**Self-learning here means a disciplined loop: attribute, aggregate, hypothesise, test, adopt through the gates.** It never tunes live rules from recent losses, because that is how a system overfits to noise.

### 11.1 Attribution of every outcome

| ID | Pri | Requirement |
| --- | --- | --- |
| `LN-1` | P0 | **An attribution record for every closed signal** (both tracks, LC-2), written when it is graded: the expected outcome at entry (the setup's mean R, its win rate, the forecast range), the realized outcome, the error (realized − expected), the component breakdown (LN-2), the events in the window (LN-3), the cause class (LN-4), the decision-quality cell (LN-5), and whether it was knowable (LN-6). Stored in an `outcome_attribution` table linked to the ledger (RL-1). |
| `LN-2` | P0 | **Return decomposition over the hold**, daily, from entry to exit, which adds up to the trade's return. Four parts: **market** (beta × SPY, with the 120-day beta of NC-43); **sector** (beta to the sector ETF's return net of SPY, TA-4); **stock-specific on news days** (the residual on sessions with a stock story of strength ≥ NC-4, earnings or a filing); **stock-specific on quiet days** (the rest). Execution (fill vs plan) and the human deviation (actual vs theoretical track) are added as two more parts for the owner's track. The breakdown shows what moved the price, in % and in R. |
| `LN-3` | P0 | **Event tags over the window, aligned to the bars that did the damage.** The tags: earnings and pre-announcements; stock stories with their catalyst type (NC); 8-Ks; analyst actions (UI-33); sector news; macro releases with their surprise (A18: CPI, jobs, FOMC and the rest); market shocks (an SPY move beyond 2× its forecast daily range, FC); the halts, corporate actions and gaps of §4. Each tag records the largest adverse and favourable bar within one session of it, so a tag that coincided with no move is not blamed. |
| `LN-4` | P0 | **Cause class, by code, in this priority order**, the first that applies: **(1) technical error:** a data error (bad print, stale or partial bar), a rule or code bug (the signal should not have fired, as a replay of the fixed code shows), a calendar error, a missed job or late delivery (FM-50..FM-58). **(2) Execution:** slippage beyond the cost model (RL-12), a partial fill, a rejected order. **(3) Human deviation:** any FM-59..FM-65 action, or an exit outside the rules. **(4) Event-driven:** the decomposition's largest adverse part lies on a tagged event; subclassed as *stock news*, *earnings*, *sector*, *macro* or *market shock*. **(5) Regime mismatch:** the market, volatility or breadth regime at entry or during the hold was one the setup was not validated in (TA-5, MO-6, RG-1). **(6) Behaviour anomaly:** no event and no regime cause, and the outcome falls outside the setup's usual range (beyond the 10th percentile of its graded R, or an adverse excursion past its winners' 90th-percentile heat, RD-3): the stock did not behave like the setup's history. **(7) Expected variance:** none of the above, and the outcome lies inside the setup's usual range. Wins are classed the same way: a win from a takeover pop is an *event windfall*, not skill. |
| `LN-5` | P1 | **Decision quality against outcome quality.** Each trade goes into a 2×2. *Good process, good outcome*: credit. *Good process, bad outcome*: variance; **change nothing**. *Bad process, good outcome*: luck; **fix anyway**. *Bad process, bad outcome*: fix. Process is good when there was no class 1–3 cause and all checks passed. The weekly report shows the four counts and the R in each. |
| `LN-6` | P0 | **Was it knowable at entry?** For event-driven losses, a point-in-time check: was the event's date, or news about it, in the system before the entry time (received_at ≤ entry; NC-41)? A *knowable* event that no check caught is a **check gap**: it becomes a proposed check (LN-8). An *unknowable* one (a surprise pre-announcement, a sudden macro shock) is outside risk. The response is size and stops, not a new filter. |
| `LN-7` | P1 | **Margin of error by component.** Over rolling windows (30 and 100 trades) and per setup, the share of total error from each part of LN-2, and the share of outcomes in each class of LN-4. This answers where the misses come from. If quiet-day residuals dominate, it is noise and there is little to fix. If macro dominates, macro-day rules are worth testing. If execution or human deviation dominates, the fix is process, not prediction. |

### 11.2 From attribution to better rules

| ID | Pri | Requirement |
| --- | --- | --- |
| `LN-8` | P1 | **Hypotheses, not changes.** Loss clusters from LN-7 with at least 20 trades become **candidate rules**, recorded with their discovery data. Examples: "BREAKOUT_20D losses cluster on CPI days"; "first pullbacks fail when breadth is under 40%"; "NVDA breakouts gap down after earnings". Each candidate carries the number of candidates tested so far, the multiple-testing count of MR-2 and EV-5. |
| `LN-9` | P0 | **Every candidate goes through the gates.** A walk-forward test on data not used to discover it, then the frozen holdout (RL-5), then a paper period (RL-3), then owner approval through the lifecycle (RL-4). A candidate that does not improve net expectancy after costs is rejected and kept in the record, so it is not re-discovered. **Nothing changes a live rule automatically.** |
| `LN-10` | P1 | **Stock behaviour profiles**, rebuilt monthly from point-in-time data. Per ticker: the follow-through rate of breakouts and pullbacks; gap behaviour (TA-7); the reaction to its own earnings (drift, gap fill); sensitivity to macro days (the mean absolute move on CPI and FOMC days against other days); sensitivity to news (the residual move per story of a given strength); typical heat and run. Ticker estimates are **shrunk toward the setup's universe-wide value** by sample size (TS-8 style), so a few trades never define a stock. Shown with each idea as "how this stock usually behaves in this setup". They affect selection or size only after LN-9. |
| `LN-11` | P1 | **Calibration feedback.** Predicted win rates, ranges and probabilities are compared with outcomes by bucket: reliability for each setup, regime and ticker group. A drifting bucket feeds RL-6 demotion, and is shown on the scorecard (RL-9). |
| `LN-12` | P1 | **Mistake catalogue.** Every recurring mistake type gets an entry: the cause class, an example trade, the rule or check added to prevent it, and **the measured effect of the fix** afterwards (R saved or lost, as SH-2 measures filters). A fix that does not help after 40 trades is flagged for removal. |
| `LN-13` | P0 | **Technical errors close only with a regression test.** Each class-1 cause opens an incident. It closes when the root cause is fixed and a test reproduces the failure (TEST_PLAN). The affected signals are re-graded with the corrected code, and both gradings are kept. |
| `LN-14` | P0 | **Post-mortem for large losses and P1 incidents.** Any loss beyond 2R or 1% of equity, any gap through a stop, and any P1 that went unacknowledged gets a written review within one session. It holds the timeline from the ledger, the LN-2 breakdown, the cause class, what would have prevented it, and the action with its owner. |
| `LN-15` | P2 | **Near misses** are recorded: a stop missing then placed, a late alert that was still in time, a chase caught by the no-chase rule. They are recorded like losses, because they show the same weaknesses without the cost. |
| `LN-16` | P2 | **Owner notes as data.** The owner's notes on a trade (UI-29), passes (HU-1) and overrides (HU-2) are linked to the attribution. Qwen may tag them with a fixed list of reasons (fear, FOMO, news, a gut call, unavailable). The tags are checked against the ledger facts and never decide a class (MR-1). |

### 11.3 Reports

| ID | Pri | Requirement |
| --- | --- | --- |
| `LN-17` | P1 | **Weekly learning report:** R by cause class, and the decision-quality 2×2 (LN-5). The margin-of-error breakdown (LN-7), with the dominant component named. Check gaps found (LN-6), technical errors opened and closed (LN-13), human deviation cost (HU-2), candidates under test and their status (LN-8, LN-9), and fixes that are paying or not (LN-12). Figures come from journal queries, never from the model (A11-3). |
| `LN-18` | P1 | **Narrative, with citations.** Qwen may write a plain summary per trade and per week from the attribution records. Every number in it must match a record (the TS-13 number check), and every cause it mentions must be the record's class. It explains; it does not decide. |
| `LN-19` | P2 | **Monthly behaviour review per ticker:** the LN-10 profile changes, the tickers whose recent behaviour departs from their history (a regime change for that stock), and whether ideas on them should carry a caution. |

## 12. Decision confidence (DC)

How confident a trader can be in an alert depends on the strength of the evidence behind it, not on the setup's average alone. On October 17, 2026, no single trade's odds are better than about 42–58% (MOMENTUM §9.2, SCANNER §6). Even a real +0.15R edge leaves roughly a 1-in-6 chance of being down after 100 trades. These requirements make that visible on every alert, and make size and capital follow the evidence.

| ID | Pri | Requirement |
| --- | --- | --- |
| `DC-1` | P0 | **A confidence grade on every alert**, by code from the evidence: **A**: forward record passed (RL-3), the 95% interval of mean net R above 0 in the backtest's both halves and in the forward period, ≥ 100 backtest and ≥ 30 forward trades, the regime coverage of DC-4, and analysis coverage complete (TA-19). **B**: the backtest criteria of A, but forward record still in progress. **C**: positive mean but the interval includes 0, or under 100 trades. **D**: research only (not alerted). The alert shows the grade, the win rate and mean R **with their intervals and sample sizes**, and the share of trades the setup lost. It never states a probability without its n (FR-003 of the owner's supplement). |
| `DC-2` | P0 | **Size follows the evidence.** Risk per trade = base risk (`risk.risk_per_trade_pct`) × a grade factor (A 1.0, B 0.75, C 0.5). For A and B it is capped by a quarter of the Kelly fraction computed from the **shrunk** expectancy (the lower half of its interval, not the point estimate). The existing halvings stack below it (high-vol RG-2, regime `CAUTION` MO-6, loss streak RC-1), down to a floor of 0.25%. The alert shows the factor and the reason. |
| `DC-3` | P0 | **Statistics that respect clustering.** The intervals of SV-1, MO-11, TS-12 and EV-5 resample whole weeks (block bootstrap), not single trades. Each result reports its **effective sample size**: trades corrected for overlap in time and correlation between names. Gates (MO-14, RL-3, DC-1) use the block interval and the effective size. |
| `DC-4` | P1 | **Regime coverage before high confidence.** A grade-A stream needs its backtest to include at least one bear market (SPY more than 20% under its high) and one high-volatility period (RG-1 high-vol on at least 20 sessions). Its results are reported by regime. The scanner gets the 10-year SIP history momentum uses (MO-22) before its techniques can rise above C. |
| `DC-5` | P0 | **Re-validate choices made after seeing both halves.** Every default chosen with both halves in view is listed with the date and the reason, and graded on the frozen holdout (RL-5) before it can support a grade above C. The list: the scanner's default disabled list (SCANNER §6.2), the momentum leader filter and default policies (MO-24), `SMA200_PULLBACK` watch-only (MOMENTUM §9.6), and any event pattern. |
| `DC-6` | P0 | **A capital plan.** Config states the split of the account between the rotation model, swing alerts, options and cash, for example 60 / 25 / 5 / 10. Each sleeve's open risk and exposure are checked against its share, and an alert that would exceed its sleeve is resized or skipped. The weekly report shows each sleeve's return against its own benchmark. The default split favours the stream with the strongest evidence (today the rotation model, §1 W-2). |
| `DC-7` | P0 | **Compared with doing nothing.** Every alert stream reports its after-tax return and Sharpe against holding QQQ and the equal-weight universe (total return, RL-11), over the same sessions, backtest and forward. A stream that does not beat the passive alternative on risk-adjusted terms in both halves stays research (grade D), as the MO-25 gate already does for the rotation model. Its alerts say what the passive alternative would have earned. |
| `DC-8` | P1 | **Expectations before the first trade.** For each stream, the console and the first alert of each month show what the next 50 and 100 trades are likely to bring, from a Monte Carlo of its graded trades (SV-2, with DC-3 blocks): the probability of being down, the median and 95th-percentile longest losing streak, the drawdown range, and the expected total R with its interval. When live results stay inside these ranges, the system says so ("a 6-trade losing streak is inside the expected range"), to stop a working plan being abandoned in a normal drawdown. |

## 13. Order of work

| Phase | Requirements | Why first |
| --- | --- | --- |
| 1. Protect money already at risk | SL-1..SL-13, IR-3, IR-6, FM-24..FM-34, LC-1..LC-3, RL-10 | Stops and reconciliation on the owner's real positions matter more than better entries |
| 2. Follow every trade end to end | EN-1..EN-8, RD-1..RD-4, HU-1..HU-2, RL-1, RL-2 | One lifecycle and one ledger for every source |
| 3. Make it run unattended | IR-1, IR-2, IR-4, IR-5, IR-7..IR-10 | Missed jobs are missed alerts |
| 4. Earn the right to alert | RL-3..RL-7, RL-9, RL-11, RL-12, DC-1..DC-3, DC-5..DC-7, then DC-4, DC-8 | Forward proof, a frozen holdout, one idea per ticker, and a grade and size that follow the evidence |
| 5. See the whole stock | TA-19, TA-8, TA-9, then TA-1, TA-2, TA-4..TA-7, TA-13..TA-15, then the P2 rows | Coverage and the critical components first |
| 6. Learn from every outcome | LN-1..LN-7, LN-13, LN-14 first, then LN-8..LN-12, LN-17, then the P2 rows | Needs the ledger (phase 2) and the event tags; attribution before any rule changes |
| 7. Broaden the edge | RL-13, RL-14, RL-15, EN-9..EN-12, RD-5..RD-10 | New uncorrelated strategies, through the same gates |

**Definition of done for "reliable":** twelve months of forward results in which every alert, entry, stop and exit is in the ledger. The owner's fills track the plan within the measured slippage. Every alerting stream is `limited` or `full` with its RL-3 criteria met. No P1 went unacknowledged. No scheduled job was missed without a P1. Every closed trade has a cause class. Technical errors and human deviations together cost under 10% of gross R. Every adopted rule change came through LN-9. The after-tax result beats the total-return benchmark it was promoted against.
