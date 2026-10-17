# Trading Ideas Platform: Gap Review 2

**Date:** October 17, 2026 · **Input:** *Trading Ideas Platform: Gaps, Additional Requirements & Validation Roadmap* (owner's supplement, 17 October 2026) · **Follows:** [GAP_REVIEW.md](GAP_REVIEW.md) (October 16)

The supplement lists 18 gaps (G-01..G-18), 14 functional and 10 non-functional requirements, and a six-phase roadmap. It is sound, but generic: it does not say what this system already does. This review records the status of each item here, adds the gaps the supplement missed, and lists what was fixed in this change.

**Verdict.** About half the supplement is already covered by earlier work (GAP_REVIEW, the walk-forward gates, PF-1..PF-4, SV-1..SV-3, NC-32..NC-49). The real open items are one outcome definition and one candidate ledger across modules, an untouched holdout, forward paper criteria for the advisory streams, and de-duplication across modules. Nine gaps the supplement did not name are in §3. Two of them, in the newest modules, are fixed in this change (§4).

## 1. The supplement's gap register against this system

| ID | Gap | Status here | Where / what is left |
| --- | --- | --- | --- |
| G-01 | Unified outcome definitions | **Open** | Each module grades its own way: R-multiples (momentum), T1/T2 hits (scanner), CAGR (rotation), P(up) and ranges (catalyst), % moves net of SPY (events), option P&L. One outcome contract per strategy family is the first step of the roadmap. |
| G-02 | Point-in-time data | Partly | News: `received_at` and the after-close rule (NC-41). Event baselines are trailing (EV-4). Not point-in-time: the universe (D1), and fundamentals before collection started. |
| G-03 | Common alert ledger | **Partly, improved** | `momentum_signals` (armed, alerted and unproven, but not `busy` or `regime_off`), `catalyst_outlooks`, `option_suggestions`, now `event_ideas` (EV-9). The scanner's daily file is overwritten. There is no single ledger with rejection reasons across modules. |
| G-04 | Probability calibration | Covered where probabilities exist | Catalyst Brier score and coverage gate (NC-32, NC-34); volatility QLIKE and coverage (FC-2). Momentum and the scanner show measured win rates, not probabilities. |
| G-05 | Net expectancy after costs | Partly | 10 bps per side (MO-11), a pessimistic fill-day variant, EXQ-1 slippage against the trigger. Missing: spread by liquidity and a cost-sensitivity sweep. |
| G-06 | Promotion and demotion | Partly | Gates re-run weekly (MO-14, NC-34, TS-11) and demote automatically. Missing: a lifecycle (experimental → paper → limited → paused) with an audit record, and hysteresis (§3, X-4). |
| G-07 | Representative universe | Partly | Survivorship bias is stated, and results are compared with equal weight (MO-1, BT-7). A point-in-time universe is deferred (D1). |
| G-08 | Multiple testing, holdout | Partly | Sample-size shrinkage (TS-8), adjustments must earn their place (NC-33, MR-2), the event chance count (EV-5). Missing: a frozen final holdout. The weekly re-grade reuses the out-of-sample half (§3, X-4). |
| G-09 | Portfolio risk gates | Covered | PF-1..PF-4 for trades; MO-17 heat, slot and correlation caps for momentum alerts. Not combined across modules (G-15). |
| G-10 | Forward paper validation | Partly | TEST_PLAN stage 4 for the trading engine (20+ paper days). The advisory streams (momentum, scanner, events) have no predeclared forward criteria. |
| G-11 | Catalyst calibration | Covered | Walk-forward gate; the news tilt is off for lack of edge (NC-49). |
| G-12 | Options payoff and probability | Not reviewed here | OPTIONS_REQUIREMENTS (A13–A16); the outcome labels need a separate review. |
| G-13 | Long-term fundamental engine | Not a gap: a new product | No valuation or multi-year engine exists. Decide separately whether one is wanted. |
| G-14 | Drift and kill criteria | Partly | Weekly re-grade, kill switch, loss locks. Missing: live-versus-backtest drift for the alert streams. |
| G-15 | De-duplication, alert fatigue | **Open** | One signal per ticker inside momentum only. The scanner, momentum, catalysts and events can all alert the same ticker, and can contradict each other (§3, X-2). |
| G-16 | Execution realism | Partly | Gap-through-stop, next-session fills, no chasing (MO-8). Missing: spread, latency and partial fills. |
| G-17 | Challenger baselines | Covered | Equal weight, SPY, the no-tilt catalyst baseline, single factors against the composite (MO-13). |
| G-18 | Evidence provenance | Partly | Catalyst stories keep their items, rule and prompt version (NC-46); the LLM number check (A11-3). Other modules have no LLM claims. |

Non-functional requirements: NFR-003 (secrets), NFR-005 (determinism), NFR-006 (recovery) and NFR-002 (observability: Grafana, health checks) are covered. NFR-001 (fail closed) was not, for momentum's earnings check; it is fixed now (§4). NFR-008 (cost per candidate) and NFR-007 (p99 latency for end-of-day jobs) add little for a one-person advisory system. NFR-009 (licensing) has one live case: the StockTwits terms (D5).

## 2. What the supplement gets right, and what to skip

**Adopt:** the outcome contract per strategy (FR-002), the immutable lifecycle ledger (FR-001), a promotion state machine (FR-007), the cost-sensitivity tests (T-005), and the "abstain is a feature" principle, which this system already follows.

**Defer:** LightGBM and ensembles (§9), a strategy registry with owners and dependencies (FR-006), regime routing (FR-008, see X-8), and the cost and latency SLOs. They suit a team; here they add weight before the basics above are in place.

## 3. Gaps the supplement missed

| ID | Gap | Why it matters here | Status |
| --- | --- | --- | --- |
| X-1 | **Stale or missing inputs fail open.** Momentum treated a missing or stale earnings calendar as "no earnings", and alerts went out. | NFR-001 states the principle, but the code did not follow it | **Fixed:** MO-18 holds entry alerts back when the calendar is older than `earnings_max_age_days` (4) or missing, and says so when no journal is available |
| X-2 | **Modules ignore each other's facts.** The events module dated an NVIDIA buy window (November 13–30) across NVIDIA's earnings (November 17). No past window held earnings. | An idea outside the conditions of its own history | **Fixed for events:** EV-10 blocks it. Cross-module conflicts in general (an event caution against a momentum buy, say) are open: G-15 |
| X-3 | **Look-ahead and selection bias in the event study.** The baseline used the whole period, and the event list was picked after the fact. | Overstated patterns | **Fixed:** trailing baseline (EV-4); 3 patterns in 33 tests is now reported as chance level. Selection bias is documented, not removable |
| X-4 | **Gates flap, and the out-of-sample half wears out.** The weekly re-grade reuses the same out-of-sample half. A setup at the bar (`SMA200_PULLBACK`, PF 1.20 against 1.2) can switch on and off from week to week. | Alerts that come and go with noise; an out-of-sample half that slowly becomes in-sample | Open: hysteresis (switch off below the bar, back on only above bar + margin), and a frozen holdout period that is never used for choices |
| X-5 | **Hand-kept calendars expire silently.** The macro calendar ends in 2027; company event dates are typed in, and some are estimated. | A stale calendar gives wrong windows without an error | Open: warn 60 days before a calendar runs out, and when an estimated date is still unconfirmed 10 days before its window |
| X-6 | **Taxes and dividends.** Rotation gains are almost all short-term (two-week rebalances). Benchmarks are price-only. | The rotation's 23.3% vs 19.4% edge (MO-13) is pre-tax; total-return SPY is about 1.3% a year higher | Open: report after-tax returns at a configurable short- and long-term rate, and total-return benchmarks |
| X-7 | **The human in the loop.** Alerts are advisory; you place the orders. Backtests assume a fill at the trigger or the next open. | Real results depend on acting late, or skipping alerts | Open: grade against your actual fills (`external_trades`, WS-9) and report backtest / alert / your-fill side by side |
| X-8 | **Concentration and regime coverage.** The tradable universe is 10 mega-cap tech names; the 10-year history has only two real drawdown regimes (2020, 2022). | One factor bet; too few regimes to learn regime routing (FR-008) | Documented; the honest response is caution in untested regimes, not a routing model |
| X-9 | **Data terms.** StockTwits (no key, terms unconfirmed), and redistribution of Alpaca and Finnhub free-tier data inside alerts. | NFR-009, made concrete | Open (owner decision, D5) |

## 4. Fixed in this change

| Item | Change | Tests |
| --- | --- | --- |
| X-1 | MO-18 fails closed: `earnings_status` fresh / stale / unchecked; stale holds entry alerts back as `earnings_unknown`; the report and the console say so | `test_earnings_calendar_fails_closed` |
| X-2 | EV-10: buy ideas with earnings inside the window are blocked, without a covering calendar they are held; Telegram sends only `ok` ideas | `test_earnings_inside_a_window_block_buy_ideas` |
| X-3 | EV-4: trailing 504-session baseline; events with under 250 prior sessions dropped; results and conclusions rewritten (EVENT_REQUIREMENTS §4) | `test_trailing_baseline_uses_no_later_data` |
| G-03 (events) | EV-9: `event_ideas` keeps every idea and grades it once | `test_every_idea_is_kept_and_graded_once` |

`./run.sh restart` rebuilds the images and runs `trader migrate`, which creates `event_ideas` in the deployed database.

## 5. Suggested order for the rest

The full requirements, including the trade lifecycle, stop-loss alerts and analysis gaps, are in [RELIABILITY_REQUIREMENTS.md](RELIABILITY_REQUIREMENTS.md).


1. **One ledger and one outcome contract** (G-01, G-03, FR-001, FR-002): a `signals` table that every module writes, holding candidates, rejections with reasons, alerts and outcomes, each graded by its declared contract.
2. **Frozen holdout and hysteresis** (G-08, X-4).
3. **Cross-module de-duplication and conflicts** (G-15, X-2): one idea per ticker per day, with the modules that agree and disagree.
4. **After-tax, total-return and your-fill reporting** (X-6, X-7).
5. **Calendar expiry warnings** (X-5) and forward paper criteria for the advisory streams (G-10).
