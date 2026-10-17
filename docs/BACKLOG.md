# Backlog

**Version:** 1.0 (October 17, 2026) · The single order of work (SQ-3). Requirement documents say *what*; this file says *when*. Gap reviews ([GAP_REVIEW.md](GAP_REVIEW.md), [IDEAS_PLATFORM_GAPS.md](IDEAS_PLATFORM_GAPS.md)) are history, and every open item from them is mapped to an epic here (§5).

**Rules.**

- An epic starts when its dependencies are done and its requirements are *ready* (SQ-1).
- An epic closes when every listed ID is *done* (SQ-2) and its acceptance check passes.
- `tools/check_traceability.py` fails CI when a planned requirement is not listed in some epic (SQ-5).
- Sizes are for one developer: **S** ≤ 1 week, **M** 2–3 weeks, **L** 4–6 weeks. **T** means elapsed time (forward evidence), not effort.

## 1. Owner decisions needed

These block or shape epics. Each is answered once and recorded in [DECISIONS.md](DECISIONS.md).

| ID | Decision | Blocks | Source |
| --- | --- | --- | --- |
| D-01 | Account: size; cash, margin or IRA; which broker(s) | B08, B15 | REQUIREMENTS §12, AC-1/AC-2, TP-1 |
| D-02 | Trader profile: the hours you can act (`eod_only`, `market_open_only`, `intraday`), alerts and new positions per day, drawdown tolerance | B08, B09, B15 | TP-1..TP-3 |
| D-03 | Capital split across sleeves (rotation, swing alerts, options, cash) | B09, B15 | DC-6 |
| D-04 | Data spend: a paid point-in-time dataset and its budget ceiling; SIP before live; OPRA for options | B13, B17, B18 | DA-2, DQ-7, OPTIONS Q2 |
| D-05 | Production host: a Linux box or a cloud VM, and its budget | B03 | IR-1 |
| D-06 | Ledger anchors: keep them local, push to a remote, add OpenTimestamps | B11 | PR-5 |
| D-07 | StockTwits and ApeWisdom: confirm their terms for programmatic use, or keep them off | — | D5, X-9, DA-5 |
| D-08 | Loss limits (0.5% per trade, 1.5% daily, 3% weekly, 6% monthly) and the wash-sale policy | B05, B15 | REQUIREMENTS §12, OPTIONS Q4 |
| D-09 | Universe: keep SPCX and TSM; allow in-play rotation (UN-2); turn on the scan universe (UB) | B17 | REQUIREMENTS §12 |
| D-10 | Execution style per window (approve, auto, co-pilot); the morning hard exit at 10:30 or 11:00 | B18 | REQUIREMENTS §12 |
| D-11 | Options: approval level, days-to-expiry policy, its own risk budget, options lines in the alerts bot | B15 | OPTIONS §13 |
| D-12 | A long-term fundamental engine: wanted, or not | B14 | G-13 |
| D-13 | `SMA200_PULLBACK`: keep watch-only (default) or allow alerts | — | MOMENTUM §9.6 |

## 2. The minimum reliable system (MVP)

**Epics B01–B10.** At the end of B10, the system protects the owner's real positions and tells them when to enter, avoid, hold and exit. It records every alert provably and alerts only from streams that earned it forward. Nothing after B10 is needed to trust an alert; everything after B10 is needed to make the edge bigger.

## 3. Epics, in dependency order

| ID | Epic | Requirements | Depends on | Acceptance | Size | MVP |
| --- | --- | --- | --- | --- | --- | --- |
| B01 | Owner decisions, round 1 | D-01..D-06, D-08 | — | Answers recorded in DECISIONS.md; config updated | S | yes |
| B02 | Spec hygiene | SQ-1..SQ-6, CA-1, CA-2, CA-5 | — | CI fails on a missing traceability row, an unmapped planned ID or an unregistered threshold; `config/thresholds.yaml` holds every CA-5 row | S | yes |
| B03 | Production host and unattended running | IR-1, IR-2, IR-8, IR-10, FM-50, FM-53, FM-55, FM-57 | B01 (D-05) | 20 consecutive sessions on the production host. Every job's check-in arrives, or a P1 is raised within 10 minutes. Clock drift under 1 s. A restart mid-job sends nothing twice | M | yes |
| B04 | Signal ledger, lifecycle and provable chain | RL-1, RL-2, RL-8, LC-1..LC-3, PR-1..PR-4, PR-6, FM-56 | B02 | Every module writes lifecycle events with a contract version. `verify-ledger` catches a changed byte, a deleted row and a reordering. An alert chained after its window opens is `LATE`. The app role cannot update or delete | L | yes |
| B05 | Protect the owner's positions | RL-10, IR-3, IR-6, SL-1..SL-13, FM-24..FM-35, FM-42..FM-44, TA-18 | B04 | Drill on the paper account (OPS-2). A position without a stop raises a P1 within 5 minutes. A stop hit repeats every 60 s until flat or acknowledged. A pre-market gap below a stop raises a P1 by 9:00. A split fixture forces a stop re-check. P1 goes out on two channels | L | yes |
| B06 | Fail closed on inputs and calendars | IR-4, IR-5, IR-7, IR-9, IR-11, FM-51, FM-52, FM-54, FM-58 | B03 | Chaos test: each input, made stale, blocks its dependent alerts with the reason. A missed job catches up with `LATE` labels. A calendar 59 days from its end warns | M | yes |
| B07 | Enter or avoid; one idea per ticker | EN-1..EN-8, RL-7, HU-6, TA-8, TA-9, TA-19, FM-1..FM-23 | B04, B06 | Replay of a month. Every buy alert carries the full plan and an order ticket. Fixtures produce `ENTER`, `TOO LATE`, `INVALIDATED`, `AVOID` and `EXPIRED` correctly. No ticker gets two action alerts in a day. Every idea shows its analysis coverage | L | yes |
| B08 | Ride the trade and follow the plan | RD-1..RD-4, HU-1..HU-5, TP-1, TP-2, TP-4, FM-36..FM-41, FM-45..FM-49, FM-59..FM-65 | B05, B07, B01 (D-01, D-02) | A daily `HOLD` message per open position names the rule that would sell. Early and ignored exits are logged with shadow R. An `eod_only` profile receives no intraday entry alerts, only resting orders. Cool-off and loss locks apply to manual fills | M | yes |
| B09 | Evidence gates and grades | RL-3..RL-6, DC-1..DC-3, DC-5..DC-7, OV-1..OV-6, LM-1..LM-3, LM-5, LM-7, CA-3, DA-8 | B04, B01 (D-02, D-03) | Every stream shows a grade with intervals, n and DSR. A 200-strategy noise family fails, and a planted edge passes. Pre-cutoff LLM results never reach a gate. A fragile gate is marked. Survivorship-biased streams cap at B | L | yes |
| B10 | Forward paper period (merges M8) | OV-7 | B05..B09 | At least one stream meets its preregistered RL-3 criteria over at least its MinTRL, with `verify-ledger` clean for the whole period. Failing streams stay `paper` | T (6–12 months) | yes |
| B11 | Scorecards and reports | RL-9, RL-11, RL-12, DC-8, OV-8, OV-9, PR-5, PR-7, PR-8, LN-17, LN-18, CA-4 | B04, B09, B01 (D-06) | The weekly report shows backtest, paper, owner fills and live per stream, after tax, against total-return benchmarks, at 1× and 2× costs. Expectation ranges appear before trading. The track-record export verifies against the anchors | M | no |
| B12 | Learning loop | LN-1..LN-16, LN-19 | B04, B11 | Every closed trade has a cause class and a component breakdown that sums to its return. A scripted CPI-day loss is classed as macro, a bad print as a technical error. Candidates go only through LN-9 | L | no |
| B13 | Data plan | DA-1..DA-7, RL-15 | B01 (D-04) | The data inventory is complete. With the paid dataset: delisted names present, total-return series, `available_at` everywhere. Without it: the decision is recorded and the B cap (DA-8) stays | M | no |
| B14 | Edge research | ER-1..ER-14, LM-4, LM-6 | B09, B13 | At least five preregistered hypotheses from ER-3 run through purged and embargoed validation, with results logged, passing or failing. The model ladder runs to its first refusal or pass. The research log lists N per family | L | no |
| B15 | Portfolio, profile and diversification | PC-1..PC-10, TP-3, TP-5..TP-7, RL-13, RL-14 | B09, B11, B01 (D-01..D-03, D-08, D-11) | The rotation sleeve produces a target book within caps, no-trade bands and tax rules. The combined plan's 95th-percentile drawdown is within the profile's tolerance. One new uncorrelated family is graded through B09 | L | no |
| B16 | Analysis depth | TA-1..TA-7, TA-10..TA-17, EN-9..EN-12, RD-5..RD-10, DC-4 | B07 | Each component is computed point-in-time, shown on ideas, and graded as a context factor before any rule uses it. The scanner has 10 years of history | L | no |
| B17 | Universe breadth | UB-1..UB-7 | B09, B13, B01 (D-09) | `universe_daily` is built every day. The close job finishes within its budget at 1,500 symbols. Setups are re-graded on the wider universe before they alert. Alert counts stay within the caps | M | no |
| B18 | Small live, approve mode (M9) | existing TEST_PLAN stage 5 | B10, B01 (D-04, D-10) | The TEST_PLAN go-live gate record | T (4 weeks) | no |
| B19 | Deferred items | D2, D3, D6 (GAP_REVIEW) | as noted there | Re-assessed after B15; each built or closed with a reason | — | no |

## 4. What each epic delivers to the trader

| Epic | Delivers |
| --- | --- |
| B05 | You are never in a position without a stop at the broker, and you hear about a stop hit or a gap at once, until you act |
| B07 | Every idea arrives as an order you can place in advance. You are told plainly when it is too late or void |
| B08 | A daily "hold, no action" message, and a measure of what early exits cost |
| B09 | A grade and an interval on every alert, and size that follows the evidence |
| B10 | Proof, recorded before the outcomes, that a stream works forward |
| B11 | One honest scorecard: your fills, after tax, against doing nothing |
| B12 | Why each trade won or lost, and only tested lessons |
| B14, B15 | A bigger edge, and a portfolio built for your account and schedule |

## 5. Where every open gap went

| Gap | Epic |
| --- | --- |
| W-1 no forward record | B10 |
| W-2 small edges | B14, B15 |
| W-3 siloed modules | B04, B07 |
| W-4 advisory trades not tracked from fills | B05, B08 |
| W-5 concentration | B15, B17 |
| W-6 operations on a desktop | B03, B06 |
| W-7 gates flap | B09 (RL-5) |
| W-8 pre-tax, price-only, theoretical | B11, B13 |
| G-01 outcome definitions · G-03 ledger | B04 |
| G-02 point-in-time data · G-07 universe · D1 | B13, B17 |
| G-04 calibration · G-06 promotion · G-08 multiple testing · G-14 drift | B09, B12 |
| G-05 net expectancy · G-16 execution realism | B11 |
| G-09 portfolio risk | covered (PF-1..PF-4, MO-17); combined across sleeves in B15 (PC-10) |
| G-10 forward paper validation | B10 |
| G-11 catalyst calibration | covered (NC-32..NC-34, NC-49) |
| G-12 options payoff | covered (A16 grading and promotion gate, OPTIONS §7) |
| G-13 long-term engine | decision D-12, then B14 |
| G-15 de-duplication | B07 (RL-7) |
| G-17 challenger baselines | covered; ladder in B14 (ER-8) |
| G-18 evidence provenance | covered for catalysts (NC-46); LLM rules in B09 and B14 |
| X-1 fail-open earnings · X-3 event look-ahead | fixed (MO-18, EV-4) |
| X-2 modules ignore each other | fixed for events (EV-10); general case B07 |
| X-4 flapping, holdout | B09 |
| X-5 calendar expiry | B06 (IR-7) |
| X-6 tax and dividends | B11, B13 |
| X-7 the owner's fills | B05, B11 |
| X-8 concentration, regime coverage | B15, B16 (DC-4) |
| X-9 data terms | D-07, B13 (DA-5) |
| D4 short interest | B16 (TA-10) |
| D5 StockTwits, ApeWisdom | D-07 |
