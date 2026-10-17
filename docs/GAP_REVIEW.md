# Gap Review Against Professional Practice

**Date:** October 16, 2026 · **Scope:** every requirements document ([REQUIREMENTS.md](REQUIREMENTS.md) 1.3, [STRATEGY_SPEC.md](STRATEGY_SPEC.md), [SCANNER_REQUIREMENTS.md](SCANNER_REQUIREMENTS.md), [OPTIONS_REQUIREMENTS.md](OPTIONS_REQUIREMENTS.md), [NEWS_CATALYST_REQUIREMENTS.md](NEWS_CATALYST_REQUIREMENTS.md), [TEST_PLAN.md](TEST_PLAN.md), [MONITORING.md](MONITORING.md), [SECURITY.md](SECURITY.md)) · **Status:** gaps marked *Closed* are built and tested in this change; the rest are deferred with the reason

The earlier review ([REQUIREMENTS_REVIEW.md](REQUIREMENTS_REVIEW.md)) asked whether the requirements help a day trader at the moment of decision. This one asks a different question: **what would a prop-trading risk desk, or a careful trader running their own money, expect that the requirements still leave out?**

A second review against the owner's *Trading Ideas Platform* supplement (October 17) is in [IDEAS_PLATFORM_GAPS.md](IDEAS_PLATFORM_GAPS.md). Open items from both reviews are ordered in [BACKLOG.md](BACKLOG.md).

## Verdict

The system is already stricter than most retail setups. It has per-trade risk, daily, weekly and monthly loss limits, broker-side stops, a kill switch, wash-sale control, walk-forward testing and staged go-live gates.

The gaps were in three places:

1. **Risk was measured one trade at a time, not across the book.** Three "independent" positions in mega-cap tech are close to one bet, and a swing position held overnight can gap straight through its stop.
2. **There was no independent last check before an order leaves.** Desks run fat-finger and price-band checks separately from the strategy logic (in the spirit of SEC Rule 15c3-5).
3. **Edge was judged on averages alone.** A desk asks how sure we are (confidence interval), what drawdown bad luck alone could cause (Monte Carlo), and whether the result beats simply holding the market (benchmark).

| Priority | Gaps | Status |
| --- | --- | --- |
| **P1** risk across the book and order controls | 5 | all closed |
| **P2** evidence and measurement | 6 | 5 closed, 1 partly |
| **P3** governance | 2 | closed (documentation) |
| **News catalyst research practice** | 6 | closed |
| Deferred | 6 | reasons below |

## P1: risk across the book and order controls

| # | Gap | What desks do | Closed by |
| --- | --- | --- | --- |
| G1 | Each trade risks 0.5%, but nothing capped the **total** risk to stops across open positions | Cap portfolio "heat": the loss if every open stop is hit, plus the new trade | **PF-1** `risk.max_open_risk_pct` (1.5%). The new trade is sized down to fit; a stop locked above the price adds nothing |
| G2 | Correlation was a hand-written list (NVDA, AVGO, TSM). AAPL, MSFT, GOOGL, AMZN and META often move together too | Measure it: rolling return correlation | **PF-2** 60-session correlation from daily bars; a new name at ≥ 0.8 with an open one is refused (`NT_PF_CORRELATION`), on top of the configured groups |
| G3 | Market value capped per position (25%), but a 2.0-beta stock carries twice the market risk of a 1.0-beta one | Beta-weighted exposure | **PF-3** sum of beta × market value ≤ 150% of equity (`max_beta_exposure_pct`) |
| G4 | Swing trades are sized from the stop, but overnight news can open the stock **through** the stop | Size for the stress loss, not only the stop | **PF-4** stressed loss = stop distance + the stock's 99th-percentile adverse overnight gap (last year); capped at 1.5% of equity |
| G5 | The order path had a duplicate guard and a rate guard, but no price-band, volume or notional check | Independent pre-trade controls (fat-finger) | **OC-1** limit within 1% of the last trade · **OC-2** shares ≤ 1% of 20-day average volume · **OC-3** prices in order (stop < entry < T1 ≤ T2) · **OC-4** optional absolute $ cap. A failure is `EXEC_SANITY` and an `ORDER_SANITY` incident |

## P2: evidence and measurement

| # | Gap | What desks do | Closed by |
| --- | --- | --- | --- |
| G6 | Reports had expectancy, profit factor and drawdown, but no risk-adjusted return | Sharpe, Sortino, Calmar; SQN for systems traders | **PA-1** in the weekly and monthly reports (`None` until 20 days of data, never a misleading number) |
| G7 | Results were never compared with the market | Report excess return over a benchmark | **PA-2** SPY return over the same period and the excess; **PA-3** both in the weekly and monthly reports |
| G8 | "Expectancy > 0.1R over 100 trades" can pass on luck | Confidence interval of the edge | **SV-1** seeded bootstrap 95% interval of mean R |
| G9 | Loss limits were set without knowing what drawdown the strategy produces by chance | Monte Carlo of the trade sequence | **SV-2** 95th-percentile drawdown over 100 resampled trades at the configured risk |
| G10 | Go-live gates used averages only | Statistical gate | **SV-3** walk-forward pass bar and reports now also need: ≥ 100 trades, the lower end of the interval > 0, and the Monte Carlo drawdown inside the monthly limit |
| G11 | The universe is today's ten largest stocks, chosen with hindsight, so any backtest over the past flatters the strategy (selection bias) | Point-in-time universes; compare with buy-and-hold of the same names | **Partly:** PA-2 benchmark and this caveat in TEST_PLAN. A point-in-time universe is deferred (D1) |

## P3: governance

| # | Gap | Closed by |
| --- | --- | --- |
| G12 | No single list of every model, what it may decide, and how it is checked (desks keep a model inventory, e.g. the Fed's SR 11-7 guidance) | **MR-1** model inventory below |
| G13 | Many techniques and adjustments are tested; some will look good by chance (multiple testing) | Already covered: scanner edges are shrunk by sample size (TS-8); catalyst adjustments must improve the walk-forward result or are switched off (NC-33). Recorded here so new features keep the rule (**MR-2**) |

### MR-1 Model inventory

| Model | Where | May decide | May never decide | Checked by | Fallback |
| --- | --- | --- | --- | --- | --- |
| Qwen3 8B (Ollama) | A5, A6, A11, A3 summary, scanner and catalyst wording, catalyst type for unruled stories | words, setup choice among code-valid setups, review verdict, catalyst type enum | price, size, stop, probability, range | schema-constrained JSON (QW-2), number check (A11-3, TS-13), prompt versions, eval-prompts, CachedLLM replay | trade skipped (never trade without review); catalyst type OTHER |
| FinBERT (ProsusAI) | A3 news sentiment | item sentiment −1..+1 | anything on the order path | catalyst-eval (sentiment is one strength factor) | lexicon scorer |
| FinTwitBERT sentiment | A3 social posts | post sentiment −1..+1 | anything on the order path | catalyst-eval once social history exists | the news scorer |
| all-MiniLM-L6-v2 | A3 story embeddings | story grouping and novelty | anything on the order path | NC-1/NC-2 tests | word-hash vectors |
| Event study (statistics) | A17 outlook | P(up), ranges | orders | walk-forward Brier and coverage (NC-32), alert gate (NC-34) | volatility range with no tilt |
| Technique engine | scanner | ideas | orders | TA-Lib parity, walk-forward (TS-11) | — |
| Macro regime (fixed rules) | A18 | regime label, a confluence check | orders, trade blocks | what followed each label (MA-9) | no label (check left out) |
| Volatility estimators (EWMA, Parkinson, HAR) | forecast | the usual 1- and 5-day move | orders | walk-forward QLIKE and 80% coverage (FC-2) | EWMA |

**MR-2:** a new model or adjustment ships only with (a) an entry in this table, (b) a walk-forward or replay test showing it helps, and (c) a fallback that fails safe.

## News catalyst research practice

The catalyst requirements (draft 0.1) were compared with how event-driven desks run news research. These additions are now in NEWS_CATALYST_REQUIREMENTS.md and built:

| ID | Practice | Why |
| --- | --- | --- |
| NC-41 | Point-in-time: news after the 4 PM close belongs to the next session; an as-of run sees nothing published later | Otherwise after-hours news is credited to a move that happened before it |
| NC-42 | One event per ticker per day 0; overlapping horizons stated as not independent | Fourteen reprints of one earnings report are one event, not fourteen |
| NC-43 | Abnormal return = return − beta × SPY (market model, 120-day beta) | A stock up 2% on a day the market is up 2% had no news reaction |
| NC-44 | When an adjustment applies, the range shape comes from the matching subgroup | A mixed-population shape gave miscalibrated ranges (found by the walk-forward test) |
| NC-45 | Items flagged as instruction-like (A3-4) never become stories | Untrusted text cannot steer a view |
| NC-46 | Stories keep their item ids, rule or model, and prompt version | Every outlook can be traced and replayed |

## Deferred

| # | Item | Why not now |
| --- | --- | --- |
| D1 | Point-in-time universe for backtests (index membership by date) | Needs a constituents history; free sources are unreliable. The selection-bias caveat is in TEST_PLAN BT-7 |
| D2 | Historical stress scenarios across open positions (e.g. replay March 2020) | With at most 3 positions, hard stops and PF-4 the added value is small; revisit with options execution |
| D3 | Transaction cost analysis against the arrival price | Needs consolidated quotes (paid SIP, DQ-7); EXQ-1 already measures slippage against the trigger |
| D4 | Short interest as % of float | FINRA's bi-monthly API needs registration; the daily Reg SHO short-volume share is used instead and labelled as such |
| D5 | Stocktwits and ApeWisdom feeds | Built, off by default, until their terms are confirmed for programmatic use (owner decision, §13 of the catalyst requirements) |
| D6 | LightGBM quantile model for catalysts (phase 6) and options flow | No graded history to train on yet; options flow needs paid OPRA data (ADR-003) |
