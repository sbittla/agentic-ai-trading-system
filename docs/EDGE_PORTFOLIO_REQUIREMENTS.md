# Edge Research, Portfolio and Spec-Quality Requirements

**Version:** 0.1 (October 17, 2026) · Status: **specified, not built** (every ID is `Planned` in [TRACEABILITY.md](TRACEABILITY.md)) · Build order: [BACKLOG.md](BACKLOG.md)

[RELIABILITY_REQUIREMENTS.md](RELIABILITY_REQUIREMENTS.md) makes alerts safe, measured and provable. It does not say how to **find** a better edge, how to turn ideas into a **portfolio**, how to fit the system to **the person** who acts on it, which **data** to trust, how its **thresholds** are justified, or what makes a requirement **ready**. This document closes those six gaps:

| § | Area | IDs | Gap it closes |
| --- | --- | --- | --- |
| 1 | Edge research | ER-1..ER-14 | The measured edges are small (MOMENTUM §9, SCANNER §6); nothing specifies how better ones are found without fooling ourselves |
| 2 | Portfolio construction | PC-1..PC-10 | Ideas go out as separate alerts; nothing builds a target book, controls turnover or minds taxes |
| 3 | Trader profile | TP-1..TP-7 | Every alert assumes an owner who can act intraday at any time |
| 4 | Data plan | DA-1..DA-8 | Free, survivorship-biased, price-only data caps how far any result can be trusted |
| 5 | Threshold calibration | CA-1..CA-5 | Many numbers in the planned specs are reasonable guesses, not measured choices |
| 6 | Spec quality | SQ-1..SQ-6 | 600 IDs across overlapping documents, with no definition of ready and no single order of work |

## 1. Edge research (ER)

The aim is a research process that finds edges a careful skeptic would accept. The strongest machine learning for stock selection is a **regularized ranking model across stocks**, built on point-in-time features with published evidence behind them and graded with the overfitting statistics of OV. It beats LLM-generated forecasts.

| ID | Pri | Requirement |
| --- | --- | --- |
| `ER-1` | P0 | **Preregistration.** Before any test, a hypothesis is written to `research_hypotheses`. It holds the feature or rule, the economic reason it should work, the expected sign and rough size, the label (ER-4), the universe, the test period and the pass criteria. Results are written to the same record afterwards. A hypothesis cannot be edited after its first test; a changed idea is a new hypothesis. Every test is a trial (OV-1). |
| `ER-2` | P0 | **Point-in-time feature store.** Every feature value is stored with `as_of` (the session it was computed for) and `available_at` (when its inputs were first available). Examples: an EDGAR filing's acceptance time plus one session for fundamentals; the revision time for estimates; `received_at` for news (NC-41). A feature for session D uses only inputs with `available_at` before D's close. Feature code is versioned, and a change creates a new feature version, never an overwrite. |
| `ER-3` | P1 | **Candidate families with published evidence**, tested first because they have survived academic replication. **Momentum** (Jegadeesh & Titman 1993; in A19). **Short-term reversal** (Jegadeesh 1990; Lehmann 1990). **Post-earnings drift and estimate revisions** (Bernard & Thomas 1989; Chan, Jegadeesh & Lakonishok 1996). **Profitability and quality** (Novy-Marx 2013). **Low volatility / betting against beta** (Ang et al. 2006; Frazzini & Pedersen 2014). **Accruals** (Sloan 1996). **Asset growth** (Cooper, Gulen & Schill 2008). **Insider purchases** (Cohen, Malloy & Pomorski 2012). **Option-implied signals:** volatility skew (Xing, Zhang & Zhao 2010) and the call-put implied-volatility spread (Cremers & Weinbaum 2010). **Expected decay:** published anomalies earn about half as much after publication (McLean & Pontiff 2016). Every family is tested net of costs on the current universe, and none is assumed to work. |
| `ER-4` | P0 | **Labels.** The primary label is the forward 21-session return relative to the stock's sector ETF (TA-4), and also relative to SPY. Swing research also uses a 5-session label. A label's window starts at the next session's open after the feature date (no same-close trading). Labels are winsorized at the 1st and 99th percentiles per date. |
| `ER-5` | P0 | **Leak-free validation: purged, embargoed cross-validation** (López de Prado, *Advances in Financial Machine Learning*, 2018). When a test fold's labels overlap the training data in time, the overlapping training samples are removed (purge = the label horizon), and an embargo of 5 sessions follows each test fold. This is combined with walk-forward testing and the frozen holdout (RL-5). A plain random split is never used. |
| `ER-6` | P0 | **Cross-sectional metrics.** Per date: the rank information coefficient (Spearman, score against label). Reported: the mean IC; its t-statistic with Newey–West errors (lags = label horizon); the IC information ratio; the share of dates with positive IC; quintile and decile spreads, net of costs (RL-12); turnover; capacity at the planned size (TA-8); and results by regime (DC-4), sector and size bucket. |
| `ER-7` | P0 | **A higher bar for new features.** A new feature or family passes only with a Newey–West t-statistic of at least 3.0 on its IC (the hurdle Harvey, Liu & Zhu 2016 propose for new factors), a positive net spread in both walk-forward halves, DSR at the grade bar (OV-6) and PBO ≤ 0.20 across the variants tried. |
| `ER-8` | P0 | **Model ladder.** Each step must beat the one before on the frozen holdout, after costs, with OV gates. (1) An equal-weight composite of z-scored features. (2) A regularized linear ranking model: ridge or elastic net on the cross-sectionally standardized features, or a logistic model for the top-quintile label. (3) Gradient-boosted trees (e.g. LightGBM), only if (2) is beaten with DSR ≥ 0.95 and the gain survives 2× costs. Complexity never ships on in-sample gains. |
| `ER-9` | P1 | **Stability checks.** Feature importance and coefficient signs are reported per walk-forward fold. A feature whose sign flips between halves is dropped (the scanner's context factors flipped this way, SCANNER §6.2). The IC decay curve (IC at horizons of 1, 5, 10, 21 and 63 sessions) sets the holding period. |
| `ER-10` | P1 | **LLM text features, where an LLM can add value.** From filings, guidance and earnings-call transcripts, fixed fields are extracted: guidance raised, held or cut; the tone of the outlook section; new risk-factor language; management changes. These are classified into fixed lists by schema-constrained output (QW-2). They are evaluated only after the model's training cutoff (LM-3), with the anonymization test (LM-4). They enter the ladder as ordinary features (ER-8). |
| `ER-11` | P1 | **Ranking feeds the existing machinery.** A model that passes produces a daily score per symbol. That score becomes a ranking factor for the rotation model (MO-13) and a filter or size input for swing setups. Each such use is a separate trial and goes through RL-3 paper testing before any alert uses it. |
| `ER-12` | P1 | **Time-box and kill rule.** A hypothesis gets at most 2 weeks of work. One that fails its preregistered criteria is closed with its result, so it is not re-tried under a new name. A family that fails three related hypotheses is parked for a year. |
| `ER-13` | P2 | **Research log.** `reports/research.md` is generated from `research_hypotheses` and `trials`. It lists everything tried, passed and failed, with N per family, so the multiple-testing count is always visible. |
| `ER-14` | P2 | **Reproducible runs.** Each research run stores its data snapshot ids, feature versions, code version, seed and environment lock, and `trader research-replay <run>` reproduces it exactly (NFR-005). |

**Build notes (ER).**

- **Tables:**
  - `research_hypotheses` (id, created, author, statement, reason, expected_sign, label, universe, period, criteria JSON, status, result JSON);
  - `features` (Parquet: symbol, as_of, available_at, feature, version, value).
- **Code:** `trader/research/` (features.py, labels.py, cv.py with purged and embargoed folds, metrics.py with IC and Newey–West, ladder.py).
- **Dependencies:** scikit-learn for ridge, elastic net and logistic models; LightGBM optional, at step 3 only; statsmodels for Newey–West.
- **Commands:** `trader research-run <hypothesis>`, `trader research-report`.
- **Tests:**
  - a purge removes exactly the overlapping training samples;
  - a planted look-ahead feature (tomorrow's return) is caught by the `available_at` check;
  - IC and Newey–West match a reference on fixed data;
  - a preregistered hypothesis cannot be edited after its first test;
  - the ladder refuses step 3 when step 2 is not beaten.

## 2. Portfolio construction (PC)

| ID | Pri | Requirement |
| --- | --- | --- |
| `PC-1` | P1 | **A target book, not a stream of alerts.** Each sleeve (DC-6) turns its ranked ideas into target weights on its rebalance schedule. The output is the target holdings, the changes against current holdings (RL-10), and the order list. |
| `PC-2` | P1 | **Risk-based sizing.** Weights come from a volatility target for the sleeve, using an ex-ante covariance matrix with Ledoit–Wolf shrinkage (2004) over 120 sessions. The initial target is 15% a year (CA): the value at which the 95th-percentile Monte Carlo drawdown (DC-8) stays within the owner's tolerance (TP-3). Within the target, weights are equal-risk or follow the score; the choice is a trial (OV-1). |
| `PC-3` | P0 | **Constraints.** No leverage (`risk.max_leverage`). Per-name caps by sleeve: 20% for rotation (MO-13), 10% for a score-driven sleeve. Sector at most 30% of the sleeve (TA-4). Portfolio beta within 0.7–1.3 unless the sleeve is market-neutral by design. Each order at most 1% of 20-day volume (OC-2). Minimum position $500, to avoid dust. |
| `PC-4` | P1 | **No-trade bands.** A holding is traded only when its target differs from its current weight by more than max(2% of the sleeve, 25% of its target). That cuts turnover and short-term tax. Stop and rule exits always execute, whatever the band. |
| `PC-5` | P1 | **Tax-aware selling** in taxable accounts (TP-5). When a sale is discretionary, lots are chosen to realize losses first, then long-term gains. A sale that would realize a short-term gain on a lot within 30 days of turning long-term is deferred, unless a rule exit applies. Wash-sale rules (WS) are always respected. The report shows the tax effect of each rebalance (RL-11). |
| `PC-6` | P1 | **Drawdown control by rule.** When a sleeve's drawdown passes `pc.dd_cut` (initial 15%, CA), its gross exposure halves. It is restored after it recovers half the drawdown or after 63 sessions. The rule is a trial, graded in the backtest. It never adds exposure in a drawdown. |
| `PC-7` | P1 | **End-of-day order lists** for owners who cannot trade intraday (TP-2): limit or market-on-close orders for the next session, or resting buy-stop-limits for setups (EN-2), each with its protective stop. |
| `PC-8` | P1 | **Portfolio backtest** with the same code, after costs (RL-12) and tax (RL-11), against the benchmarks of DC-7. It reports turnover, average holding period, tax drag and capacity. |
| `PC-9` | P2 | **Fractional shares** where the broker supports them, so small accounts can hold the target weights. Otherwise weights are rounded to whole shares and the rounding error is reported. |
| `PC-10` | P0 | **One risk view across sleeves.** The engine's trades, the advisory sleeves and the owner's other positions are combined for the heat, correlation, beta and gap-stress limits (PF-1..PF-4). A sleeve cannot use another's risk budget. |

**Build notes (PC).**

- **Code:** `trader/portfolio/construct.py` (targets, Ledoit–Wolf covariance, constraints solved with a small quadratic program, or iterative capping when the solver is not installed), `trader/portfolio/tax.py` (lot selection, wash-sale check reused from WS).
- **Config:** `sleeves:` (name, weight, vol_target, name_cap, sector_cap, rebalance, band).
- **Tests:**
  - the caps hold on random inputs;
  - the no-trade band suppresses small changes, but never a stop exit;
  - lot selection on fixtures (losses first, near-long-term deferral);
  - the drawdown rule halves and restores exposure on a scripted equity path;
  - weights × prices reconcile to the order list.

## 3. Trader profile (TP)

| ID | Pri | Requirement |
| --- | --- | --- |
| `TP-1` | P0 | **A versioned profile** (`profile:` in config): capital, account type (cash, margin, IRA), broker, tax rates (TX-1), the hours the owner can act (for example `eod_only`, `market_open_only`, `intraday`), the attention budget (alerts and new positions per day), allowed instruments (stock; options by approval level), and the drawdown tolerance in %. Every change is versioned (IR-9). |
| `TP-2` | P0 | **Alerts fit the schedule.** An alert type the owner cannot act on in time is not sent as an action. For an `eod_only` owner: no intraday entry alerts; every entry is a resting order placed the evening before (EN-2, PC-7); exits use broker-side orders. P1 stop alerts are still sent, because the broker stop does the work and the owner is told. |
| `TP-3` | P0 | **Drawdown tolerance sets size.** Volatility targets (PC-2) and per-trade risk (DC-2) are set so that the 95th-percentile Monte Carlo drawdown (DC-8) of the combined plan stays within the stated tolerance. If even the minimum size breaches it, the system says so instead of trading. |
| `TP-4` | P1 | **Attention budget.** At most `profile.max_alerts_per_day` action alerts (P2) and `profile.max_new_positions_per_day` entries. Candidates are ranked by grade (DC-1), and the rest go to the console. P1 alerts never count against the budget. |
| `TP-5` | P1 | **Account rules.** In an IRA, tax logic (PC-5, RL-11) is off and wash-sale logic follows the owner's policy. In a cash account, the settlement rules apply (FM-17). On margin, the pattern-day-trader rules as they currently apply to the account (§6.5). |
| `TP-6` | P1 | **Onboarding questions** (`trader profile-setup`) fill TP-1 and show the consequences before saving: which streams and alert types will be active, the expected alerts per week, and the DC-8 ranges at the chosen size. |
| `TP-7` | P2 | **Profile advice from behaviour.** From HU-2 adherence data, the system may *suggest* a profile change. For example, missing 60% of intraday triggers over 30 alerts suggests switching to `eod_only`. It never changes the profile itself. |

## 4. Data plan (DA)

| ID | Pri | Requirement |
| --- | --- | --- |
| `DA-1` | P0 | **Data inventory** in DATA_SPEC: every source with its content, point-in-time quality, coverage, licence terms, cost, freshness and the requirements that depend on it. Reviewed quarterly. |
| `DA-2` | P0 | **Decision record for a paid point-in-time dataset** (an owner decision, BACKLOG D-04). Criteria: delisted names included; daily total-return and split-adjusted prices; fundamentals with filing dates; estimate history; corporate actions; a constituents history; a licence that allows personal research use; cost within the owner's ceiling. A trial period checks it against SIP closes (IR-11) and against a list of known delistings. |
| `DA-3` | P0 | **`available_at` on everything stored:** bars (session end), news (`received_at`), filings (EDGAR acceptance time), estimates (revision time), fundamentals (filing time + 1 session) and computed features (ER-2). Backtests and grading read data only through as-of queries. |
| `DA-4` | P1 | **Total-return series** derived from dividend and corporate-action records, alongside split-adjusted price series. Long-horizon grading and benchmarks use total return (RL-11). |
| `DA-5` | P0 | **Licence compliance.** Each source's use is limited to its licence: personal research, no redistribution. Anything published (anchors PR-5, exports PR-7) holds no licensed data beyond what the licence allows. The StockTwits decision (D5, X-9) and any scraping are recorded with the terms checked. |
| `DA-6` | P1 | **Quality checks on every ingest:** missing sessions, duplicates, outliers beyond 10 standard deviations of the daily return without a matching corporate action, zero volume on trading days, and split mismatches. Failures raise DQ incidents and mark the symbol suspect (FM-52). |
| `DA-7` | P1 | **Raw vendor files kept** (immutable, with checksums) for 7 years or the licence's limit, and included in backups (OPS-1). Every derived dataset records which raw files it came from. |
| `DA-8` | P0 | **Grade cap until point-in-time data exists.** While results rest on survivorship-biased data (MO-1, D1), no stream can be graded above B (DC-1), and every report keeps the caveat. |

## 5. Threshold calibration (CA)

| ID | Pri | Requirement |
| --- | --- | --- |
| `CA-1` | P0 | **Threshold registry** (`config/thresholds.yaml`): every numeric threshold in a requirement. Each entry holds an id, the value, its unit, the requirement that uses it, a status (`policy`, `initial` or `calibrated`), the rationale, the calibration method and the last review date. Code reads thresholds from config, never from scattered literals. A test fails when a requirement's threshold is missing from the registry. |
| `CA-2` | P0 | **Policy against statistics.** *Policy* thresholds express the owner's preferences and are decided, not calibrated: loss limits, risk per trade, drawdown tolerance, the attention budget. *Statistical* thresholds are calibrated on in-sample or paper data, never on the frozen holdout (RL-5). Calibration is a trial (OV-1). |
| `CA-3` | P0 | **Sensitivity on every gate.** Each gate and grade decision is recomputed with every statistical threshold at −25% and +25%. A decision that flips is marked **fragile**, and fragile streams cannot be graded A. |
| `CA-4` | P1 | **Quarterly review.** `initial` thresholds become `calibrated` (with the evidence) or are confirmed as policy. A change goes through the lifecycle (RL-4) and is versioned (RL-8). |
| `CA-5` | P1 | **The initial thresholds in the planned specs**, all `initial` until reviewed (table below). |

| Threshold | Initial | Used by | Type |
| --- | --- | --- | --- |
| Gate hysteresis margin | bar × 1.15, 2 grades | RL-5 | statistical |
| Frozen holdout | latest 12 months | RL-5 | statistical |
| Demotion window and bar | 30 signals; upper end of interval < +0.1R | RL-6 | statistical |
| Grade size factors | A 1.0, B 0.75, C 0.5 | DC-2 | policy |
| Kelly fraction cap | ¼ of shrunk Kelly | DC-2 | policy |
| Risk floor | 0.25% | DC-2 | policy |
| DSR bars | A 0.95, B 0.90 | OV-6 | statistical |
| PBO bar | 0.20 | OV-6 | statistical |
| CSCV blocks | 16 | OV-5 | statistical |
| New-feature t-stat | 3.0 | ER-7 | statistical |
| Purge / embargo | label horizon / 5 sessions | ER-5 | statistical |
| Earnings calendar max age | 4 days | MO-18 | policy (fail closed) |
| Input freshness | quotes 2 s orders / 2 min monitoring; bars by 17:30 ET; news 30 min | IR-4 | policy |
| P1 repeat interval | 60 s | IR-3, SL-4 | policy |
| Delivery latency incident | P1 p95 30 s; P2 p95 5 min | IR-3 | policy |
| Near-stop note | −0.75R | SL-3 | policy |
| Cool-off after a stop-out | 2 sessions | HU-3 | policy |
| No-chase limit | trigger + 0.5R | EN-1, MO-8 | statistical |
| Second-chance window | 0.25 ATR within 3 sessions | EN-9 | statistical |
| Sleeve volatility target | 15% a year | PC-2 | policy, bounded by TP-3 |
| Sleeve drawdown cut | 15% | PC-6 | statistical |
| No-trade band | max(2% of sleeve, 25% of target) | PC-4 | statistical |
| Sector cap | 30% of sleeve; 40% of open risk | PC-3, RL-14 | policy |
| Scan universe filters | $10, $50M ADV, 260 sessions, 1,500 names | UB-1, UB-2 | policy |
| Close-job time budget | 25 min | UB-4 | policy |
| Large-loss post-mortem | 2R or 1% of equity | LN-14 | policy |
| Loss-cluster minimum | 20 trades | LN-8 | statistical |
| LLM evaluation start | cutoff + 30 days | LM-3 | policy |

## 6. Spec quality (SQ)

| ID | Pri | Requirement |
| --- | --- | --- |
| `SQ-1` | P0 | **Definition of ready.** A requirement may be built when it has an ID, a priority, an owning component, a named acceptance test (or a "Manual" check with its procedure), its dependencies in BACKLOG, and every threshold registered (CA-1). Requirements that are not ready stay `Planned` and cannot be scheduled. |
| `SQ-2` | P0 | **Definition of done.** The code is built. Tests are marked `@pytest.mark.rule("<ID>")`. The TRACEABILITY row's test column names the test instead of `Planned`. Measured results are written to the requirement's document. The backlog item is closed with the commit. |
| `SQ-3` | P0 | **One order of work.** [BACKLOG.md](BACKLOG.md) is the only place that orders work. Requirement documents say *what*; the backlog says *when*. Gap reviews ([GAP_REVIEW.md](GAP_REVIEW.md), [IDEAS_PLATFORM_GAPS.md](IDEAS_PLATFORM_GAPS.md)) are history and point to the backlog. |
| `SQ-4` | P0 | **Two-way traceability, enforced.** `tools/check_traceability.py` also fails when an ID defined in the first column of a table in a requirements document (`docs/*REQUIREMENTS*.md`) is missing from TRACEABILITY.md, so no requirement can be written and forgotten. |
| `SQ-5` | P1 | **Every planned item maps to a backlog epic.** A check lists planned IDs that no epic references. |
| `SQ-6` | P1 | **Measured, not asserted.** A requirement that claims an effect (for example "reduces turnover") names the metric and report where the effect will be measured once built. |
