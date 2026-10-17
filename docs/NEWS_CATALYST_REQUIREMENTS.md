# News Catalyst Requirements (NC)

**Version:** 1.0 (October 16, 2026) · Status: **built, advisory; outlooks stay UNCALIBRATED until the NC-34 gate passes** · Gaps vs desk practice: [GAP_REVIEW.md](GAP_REVIEW.md) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md), [DATA_SPEC.md](DATA_SPEC.md), [SCANNER_REQUIREMENTS.md](SCANNER_REQUIREMENTS.md) and [OPTIONS_REQUIREMENTS.md](OPTIONS_REQUIREMENTS.md)

The news catalyst feature answers, per stock:

1. **How strong is the news?** A 0–100 catalyst strength score, with the factors behind it.
2. **What may happen next?** For **1 day, 1 week (5 sessions), 2 weeks (10 sessions) and 1 month (21 sessions)**: the probability of an up move, the typical move, and a likely up/down range in percent.
3. **What position may fit?** A stance (bullish, bearish, neutral or no trade) and a vehicle (stock, call spread, put spread or wait), with entry, invalidation and size from the existing risk rules.
4. **What does the crowd say?** A social sentiment indicator read alongside the news, including where it agrees and disagrees.
5. **What else agrees or disagrees?** Technicals, options pricing, analysts, earnings history, insiders, short interest, and market and sector regime.

It is **advisory**. It produces views, ranges and suggested plans; it never places an order (ADR-006).

```bash
./run.sh catalyst NVDA            # one stock: strength, outlook, position view (add --telegram)
./run.sh catalyst                 # every universe ticker with a live catalyst, strongest first
./run.sh catalyst-eval            # walk-forward grading of past outlooks; writes reports/catalyst_eval.json
./run.sh backfill-catalysts 365   # one-time history for calibration (§6)
```

On Windows, `run.cmd` takes the same commands. In Telegram or the console, `CATALYST [TICKER]` runs it on request. trader-core runs it at 8:45, 10:45, 12:15 and 13:45 ET, at 16:25 (the only run that may alert), grades at 16:40, and re-grades the whole history every Sunday at 18:30.

### What the answer looks like (illustrative numbers, not a forecast)

```
NVDA · catalyst 78/100 STRONG · earnings beat + raised guidance (2 h ago, 14 sources, novel)
News   +0.62 (FinBERT, 14 items)   Social +0.41, mentions 3.8x normal (agrees)   Day-0 +6.1% on 2.9x volume (confirms)
Context uptrend · RS vs SPY +9% · IV rank 71 → 38 after the event · analysts 3 PT raises · next earnings in 91 days

Horizon   P(up)   Typical   Likely range (10%–90%)   Based on
1 day      56%    +0.4%     −2.9% … +3.6%            implied move + 61 similar events
1 week     58%    +1.1%     −5.0% … +7.2%            post-earnings drift, n=61, calibrated
2 weeks    57%    +1.5%     −6.8% … +9.4%
1 month    55%    +1.8%     −9.9% … +13.1%           low confidence (wide)

View   BULLISH, swing (1–2 weeks). Vehicle: stock, or call debit spread (IV now normal).
Plan   buy stop 189.40 (above day-0 high) · invalidation 176.80 (day-0 low − 0.25 ATR) · size from risk 0.5%
Watch  holding above the day-0 low keeps the drift case alive; a close below it voids the view.
Not advice. Track record: 61 graded outlooks, P(up) Brier 0.238 vs 0.250 baseline.
```

---

## 1. What exists today and what is missing

| Area | Today | Gap this feature fills |
| --- | --- | --- |
| News ingest | A3 (news-sentiment) pulls Finnhub company news, EDGAR 8-K, analyst-rating RSS, and Reddit **if keys are set**; text is cleaned (DQ-4) | Items are scored one at a time. Nothing groups them into **stories**, measures **novelty**, or says what **type** of catalyst it is |
| Sentiment | FinBERT `P(pos) − P(neg)` per item; Qwen writes a 3-sentence summary of the top items | No **strength** score, no aggregate per ticker over time, no "surprise" measure |
| Flags | `EARNINGS_TODAY/3D`, `NEW_8K`, `RATING_CHANGE`, `SOCIAL_SPIKE` (posts ≥ 10 and > 3× trailing) | Flags are yes/no. They don't say how big or which way |
| Social | Reddit posts via PRAW in 4 subreddits, counted for `SOCIAL_SPIKE` only. **On this machine `REDDIT_CLIENT_ID` is empty, so there is no social data** | A real social indicator: volume, direction, agreement with news, crowding |
| Price reaction | Scanner context: trend, RS vs SPY, relative volume, ADX (TS-6) | No event study: how stocks actually moved after similar news |
| Options | A13 stores ATM IV, IV rank and the 30-day expected move per day (`options_iv_daily`) | Not linked to news; the implied move isn't used as a forecast range |
| Forecast | None | Probability and ranges per horizon, graded against what happened |
| Position | Scanner ideas and A14 options suggestions | A catalyst-driven view that hands off to the same sizing and options code |

Data on hand (checked October 16, 2026): 1,841 Finnhub items since October 1, 3 EDGAR filings, 0 Reddit posts, IV history for 10 tickers, and about 2 years of daily bars in the Parquet store (`data/bars`). **16 days of news isn't enough to calibrate anything.** §6 adds a one-time history backfill.

## 2. Constraints from the current design (must hold)

| Constraint | Source | Effect here |
| --- | --- | --- |
| Free data only | ADR-003 | Every new source must be free and allowed for programmatic use; paid feeds are out of scope (§12) |
| Only code sizes and places orders | ADR-006, SEC-5 | Advisory only. The LLM never sets a price, probability, range or size |
| Long-only stock | ADR-008 | A bearish view means puts or put spreads (ADR-015), never a short sale |
| Defined-risk options, advisory first | ADR-015 | Option vehicles go through A14/A15 unchanged |
| Untrusted text | DQ-4, SEC-4 | Social posts and headlines are cleaned before any model sees them; text can't change a number |
| 8 GB GPU, one loaded model | QW-1, HARDWARE.md | No second GPU model. New models run on CPU in news-sentiment |
| No look-ahead | TS-10 style | An outlook for time T uses only data published before T; tested by cutting history |
| Container roles | ADR-010, DATA_SPEC §3 | news-sentiment writes news-side tables; trader-core writes outlooks; console reads |

## 3. Model decisions

The task splits into reading text, which suits language models, and estimating moves, which suits statistics. No model "knows" the future price. The honest goal is a **calibrated** range: of the times it says 60% up, about 60% should end up.

| Job | Model | Decision |
| --- | --- | --- |
| News item sentiment | **FinBERT** (`ProsusAI/finbert`, CPU, existing) | **Keep.** Trained on financial news sentences; good for headlines and articles |
| Social post sentiment | **New: a finance-social classifier on CPU.** First choice `StephanAkkerman/FinTwitBERT-sentiment` (BERT-base trained on financial tweets); fallback `cardiffnlp/twitter-roberta-base-sentiment-latest` (general tweets) | **Add.** FinBERT reads slang, tickers, emojis and sarcasm poorly ("puts printing 🚀"). ~0.5 GB RAM, ~20 ms per post on 4 threads. Verify the licence and pin the revision hash during the build |
| Story grouping and novelty | **New: `sentence-transformers/all-MiniLM-L6-v2`** (Apache-2.0, 80 MB, CPU) | **Add.** Groups 14 headlines about one earnings report into one story, and tells a new story from a rehash. Novelty is one of the strongest strength factors |
| Catalyst type and short explanation | **Qwen3 8B** (existing, Ollama) | **Keep, new prompt `prompts/catalyst/v1.md`.** Rules (regex: "beats", "guidance", "FDA", "acquire", 8-K item numbers) classify first; Qwen handles only what rules can't, with schema-constrained JSON (QW-2). It also writes the plain-English "why" from computed numbers (like TS-13) |
| Move probability and ranges | **No ML model in phase 1.** Implied move (A13) and realized volatility give the width; a pooled, shrunk **event study** (§5.6) gives the tilt | **Chosen.** Explainable, works with little data, can be graded. Daily stock returns are mostly noise; a complex model would fit the noise |
| Same, phase 6 | **LightGBM quantile regression** (MIT, CPU) on the §5.3–§5.5 features | **Not built (October 16, 2026).** There is no graded history to train on yet. Build it only if it beats the event study on walk-forward grading (§6, NC-33) |
| Price forecasting foundation models (Chronos, TimesFM, Lag-Llama) | — | **Rejected.** They forecast from the price series alone, ignore the news, and show no reliable edge on single-stock daily returns. They'd also need the GPU |
| Bigger finance LLM (Fin-R1, FinGPT, Qwen3 14B) | — | **Rejected.** Doesn't fit 8 GB alongside the trading agents (HARDWARE.md §2); reading text better doesn't make price numbers better |
| Paid sentiment APIs | — | **Out of scope** (ADR-003) |

**Net new models: two small CPU models** (social sentiment and sentence embeddings), about 0.6 GB more RAM in news-sentiment, and no GPU change. Both go into the news-sentiment image with pinned hashes, like FinBERT.

## 4. Data sources

Existing sources stay as they are. The new ones are all free; each says "verify" where the free-tier terms must be checked against the provider's docs during the build.

| Source | What | Use | Budget / note |
| --- | --- | --- | --- |
| Finnhub (existing key) | `company-news` | Stories; **history up to 1 year back** for calibration (verify the free depth) | Shares the < 60/min budget with A3 |
| Finnhub | `stock/earnings` (actual vs estimate EPS), `stock/recommendation` (buy/hold/sell counts), `calendar/earnings` | Earnings surprise %, analyst trend, next earnings date | Daily; verify each is on the free plan. A 401/403 turns that endpoint off with one log line (like ratings.py) |
| Analyst-rating RSS (existing, ADR-016) | Upgrades, downgrades, price-target changes | Strength factor; PT gap vs price | As today |
| SEC EDGAR (existing) | 8-K with item numbers; **new: Form 4** insider transactions | 8-K item → catalyst type; insider open-market buys and sells | 10 requests/s limit with User-Agent, as today |
| Reddit via PRAW (existing code) | Posts and comment counts mentioning the ticker | Social indicator | **Needs `REDDIT_CLIENT_ID/SECRET` in `.env.news`**, currently empty. < 100 queries/min |
| Stocktwits public symbol stream | Messages per ticker, some carrying a user-set Bullish/Bearish tag | Social indicator (labelled data helps calibrate the classifier) | **Verify** the endpoint is still public and allowed; optional, off by default |
| ApeWisdom | Reddit mention counts and rank per ticker | Fallback social **volume** when Reddit keys are missing | **Verify** terms; volume only, no text |
| FINRA | Bi-monthly short interest; daily Reg SHO short-sale volume files | Short interest % float, days to cover, squeeze risk | Files are free downloads; daily after the close |
| A13 (existing) | ATM IV, IV rank, expected move to each expiry | Range width; "priced in" check; IV change after the event | From `options_iv_daily` and live chains |
| Parquet bar store (existing) | 2 years of daily bars; SPY and sector ETFs | Day-0 reaction, realized vol, event-study outcomes | Already loaded by the scanner (TS-16) |

## 5. Functional requirements

Two parts, split by container: **A3 is extended** (news-sentiment) to build stories, types and social numbers. A new **A17 News catalyst agent** (trader-core, code plus Qwen for words only) scores, forecasts and suggests positions.

### 5.1 Stories (A3 extension)

| ID | Requirement |
| --- | --- |
| `NC-1` | Every cleaned news item is embedded (MiniLM) and joined to an open **story** for the same ticker if cosine similarity ≥ `catalyst.story_join` (default 0.75) within 72 hours; otherwise it opens a new story. A story keeps its first-seen time, item count, distinct-source count and member items. |
| `NC-2` | **Novelty** 0–1 = 1 − max similarity to the ticker's stories in the prior 30 days. A rehash of last week's story scores low even if it has many items. |
| `NC-3` | **Source weight**: SEC filing and company press release 1.0; major wire and outlet 0.8; other outlet 0.6; aggregator or rewritten copy 0.4; social 0 (social is counted separately, §5.4). The source table lives in config and is editable. |
| `NC-4` | A story is a **catalyst** when its strength (§5.3) is ≥ `catalyst.min_strength` (default 40) or it carries a hard event (earnings, 8-K item 1.01/2.02/5.02/8.01, rating change, guidance). |

### 5.2 Catalyst type

| ID | Requirement |
| --- | --- |
| `NC-5` | Each story gets one type from the table below and a direction (positive, negative or mixed). Rules run first; Qwen is asked only when no rule matches, with the cleaned headlines in a fenced DATA block (PR-3) and the answer limited to the enum (SEC-5). |
| `NC-6` | The type, the rule or model that set it, and the prompt version are journaled so a grade (§6) can be traced to the classifier. |

| Type | Examples | Typical behaviour in published research (to be measured here, §6) |
| --- | --- | --- |
| `EARNINGS` | beat/miss, EPS and revenue surprise | Biggest moves; **post-earnings drift** in the surprise direction for weeks (PEAD) |
| `GUIDANCE` | raised/cut outlook | Strong, often bigger than the EPS surprise |
| `ANALYST` | upgrade, downgrade, initiation, PT change | Small day-0 move, partly fades in large caps |
| `M_AND_A` | acquirer, target, rumour | Target: jump then flat; acquirer: often down |
| `PRODUCT` | launch, approval, contract win | Varies; often "sell the news" for widely expected events |
| `REGULATORY_LEGAL` | FDA, antitrust, lawsuit, probe | Fat tails both ways |
| `MANAGEMENT` | CEO/CFO change (8-K 5.02) | Mostly negative when unexpected |
| `CAPITAL` | buyback, dividend, offering, split | Offering negative; buyback mildly positive |
| `INSIDER` | Form 4 cluster buying or selling | Open-market buying weakly positive over weeks |
| `MACRO_SECTOR` | sector news, peer read-through, tariffs | Moves with the sector ETF; use the abnormal return |
| `SOCIAL_ONLY` | heavy chatter, no news story | Weakest; frequent reversals (§5.4 crowding) |
| `OTHER` | anything else | Low weight |

### 5.3 Strength score

| ID | Requirement |
| --- | --- |
| `NC-7` | **Strength 0–100** per story and per ticker-day (max over live stories, decayed with `catalyst.half_life_h`, default 24 h). Computed by code from the factors below; every factor and its points are shown so the score explains itself. |
| `NC-8` | Factors and starting weights (tuned by §6, then frozen in config): type materiality (0–20, by type and 8-K item); **surprise size** (0–20: EPS/revenue surprise in standard deviations, guidance change %, PT change % vs price, rating step size); novelty (0–15); source quality and breadth (0–10: source weights, distinct sources); news sentiment strength (0–10: \|mean FinBERT\| weighted by source); **market confirmation** (0–15: day-0 abnormal return vs SPY/sector in ATR units, relative volume, close location in the day's range); social confirmation (0–5, §5.4); persistence (0–5: the story keeps getting coverage the next day). |
| `NC-9` | Labels: 0–39 WEAK, 40–59 MODERATE, 60–79 STRONG, 80–100 MAJOR. The **direction** is separate from the strength: a strong negative catalyst is "STRONG, bearish". |
| `NC-10` | **Priced-in check**: when the day-0 move already exceeds 1.5× the implied move (A13) or 3 ATR, the outlook marks the catalyst "largely priced in" and shrinks its drift (§5.6). |

### 5.4 Social sentiment indicator

| ID | Requirement |
| --- | --- |
| `NC-11` | Per ticker and day, from Reddit (and Stocktwits/ApeWisdom when enabled): **mention volume** and its z-score against the trailing 20 sessions; **net sentiment** −1…+1 from the social classifier (Stocktwits user tags used when present); **bull share** %; **distinct authors**. |
| `NC-12` | Guards against noise and manipulation: one vote per author per day; posts shorter than 20 characters or with no ticker match ignored; spam-like duplicates (embedding similarity ≥ 0.95) counted once; at most `catalyst.max_posts_per_author` per day; accounts younger than 30 days weighted 0.5 when the source exposes age. |
| `NC-13` | **Agreement** with the news: `AGREES`, `DISAGREES` or `QUIET` (z < 1). Disagreement is shown prominently (for example good news while social is bearish). |
| `NC-14` | **Crowding warning**: mention z ≥ 3 with bull share ≥ 80% and price ≥ 2 ATR above EMA20 (or the mirror for bearish) flags `CROWDED` and lowers the 1-week drift. Extreme retail one-sidedness tends to come before reversals in published work; §6 measures whether it does here. |
| `NC-15` | With no social source configured, the indicator shows "no social data (set Reddit keys: ACCOUNT_SETUP.md)" and the strength simply leaves out its 5 social points. Nothing fails. |

### 5.5 Other indicators (confluence)

| ID | Requirement |
| --- | --- |
| `NC-16` | Each outlook carries a **confluence panel** of independent checks, each marked supports / against / neutral, with its number: |

| Group | Indicators | Why it matters |
| --- | --- | --- |
| Technical (scanner, TS-6) | trend vs SMA50/200, RS vs SPY (63d), RVOL, ADX, nearest support/resistance, any active scanner idea | News in the direction of the trend lasts longer; breakouts on news with volume follow through more often |
| Day-0 reaction | gap %, close vs open, close location in range, abnormal return vs sector ETF | The market's first verdict; a gap up that closes near its low is a warning |
| Options (A13) | IV rank, implied move to each horizon, IV crush after the event, put/call volume ratio where the feed has it | Width of the range; whether options are cheap or expensive for the vehicle |
| Analysts | consensus trend (Finnhub recommendation), recent actions, mean PT vs price | Revisions cluster after catalysts |
| Earnings | last 4 surprises, days to next earnings | PEAD; an upcoming report inside the horizon is binary risk (§5.7) |
| Insiders | Form 4 open-market buys/sells, 90 days | Cluster buying is a known weak-positive signal |
| Short interest | % of float, days to cover, change since the last report, daily short-volume ratio | Squeeze potential on good news; crowded shorts |
| Regime | SPY vs SMA50, sector ETF trend, VIX level, macro events in the horizon (FOMC, CPI, jobs) | Good stock news fights a falling market |

### 5.6 Horizon outlook (1 day, 1 week, 2 weeks, 1 month)

| ID | Requirement |
| --- | --- |
| `NC-17` | For each horizon h ∈ {1, 5, 10, 21} sessions, from the close of the as-of day: **P(up)**, the **median move %**, the **10th–90th percentile range %**, the same as **excess return vs SPY**, a **confidence** label (HIGH / MEDIUM / LOW) and the **sample size n** behind it. |
| `NC-18` | **Width** (volatility for h): the implied move to the matching expiry when A13 has it, else EWMA realized volatility of daily returns (λ 0.94) scaled by √h; blended per `catalyst.iv_weight` (default 0.6 implied). Earnings, FOMC or CPI inside the horizon add their historical event variance. |
| `NC-19` | **Tilt** (drift and skew): a pooled **event study** over the history store: for every past story of the same type, direction and strength bucket across all universe tickers (and the 40 large caps in `news/names.py` for more samples), the forward abnormal returns at h, measured in **volatility units** so different stocks can be pooled. The mean is **shrunk toward zero** by sample size (empirical Bayes: weight n / (n + k), `catalyst.shrink_k` default 30). Small or noisy buckets therefore show almost no tilt rather than a confident guess. |
| `NC-20` | Adjustments, each used **only if §6 shows it improves grading**: PEAD (earnings surprise sign × size), priced-in shrink (NC-10), crowding (NC-14), trend agreement (NC-16), market regime. |
| `NC-21` | Ranges are percentiles of the pooled, vol-scaled outcome distribution times the width, so fat tails from past events show up. They aren't a normal curve. |
| `NC-22` | **Scenarios** in words, from code: base, bull and bear cases, each with a price level that confirms it (for example "a close above the day-0 high keeps the drift case"). Qwen may rewrite the text for readability; it gets the numbers and may not change them (TS-13 rule). |
| `NC-23` | Every outlook says "not a prediction of a certain outcome" and shows the track record (§6). Until the gate passes (NC-34), it's labelled **UNCALIBRATED**. |

### 5.7 Position view

| ID | Requirement |
| --- | --- |
| `NC-24` | **Stance** from the outlook: `BULLISH` when P(up) ≥ `catalyst.stance_p` (default 0.56) at the chosen horizon and the median excess return ≥ 0.25 × the horizon's volatility; `BEARISH` for the mirror; otherwise `NEUTRAL`. `NO_TRADE` when confidence is LOW, strength is WEAK, or the next earnings date falls inside the horizon and the catalyst isn't earnings. The best horizon is the one with the highest median-move-to-width ratio. |
| `NC-25` | **Vehicle** (suggestion only): bullish → stock (long, ADR-008) or a call debit spread when IV rank < 60; bearish → put debit spread (ADR-015), never a short sale; neutral → nothing, or "wait for confirmation" when the day-0 reaction disagrees with the news. Event risk inside the horizon → defined-risk only. Option structures come from A14 and pass A15, unchanged. |
| `NC-26` | **Plan numbers by code**: entry trigger above the day-0 high (bullish) or below the day-0 low (bearish), or the scanner's trigger if a scanner idea agrees; invalidation at the other side of the day-0 range ± 0.25 ATR; T1/T2 at 1.5R/3R, capped at the horizon's 90th percentile move; size from the existing sizer and risk limits (`risk:` section, 0.5% per trade), never from the model. Wash-sale (WS) and compliance (A12) checks run as for any idea. |

### 5.7a Research practice (added after [GAP_REVIEW.md](GAP_REVIEW.md))

| ID | Requirement |
| --- | --- |
| `NC-41` | **Point in time.** News published at or after the 4 PM close belongs to the next session (its day 0). A run as of a past date sees only items published before that date ends. |
| `NC-42` | **One event per ticker per day 0** (the strongest story) in the event study; the evaluation states that overlapping horizons on one ticker are not independent. |
| `NC-43` | **Abnormal return** = return − beta × SPY return, with a 120-session beta from the bars before day 0. |
| `NC-44` | When an adjustment applies, the range **shape** comes from past events that share the flag (≥ 30), not from the mixed population. |
| `NC-45` | Items flagged as instruction-like by A3 (A3-4) never become stories. |
| `NC-46` | Stories store their item ids, whether a rule or the model typed them, and the prompt version. |
| `NC-47` | **Relevance.** An item counts only if its headline or first 400 characters name the company (symbol or a name in `news/names.py`). Feeds file articles about other companies under a ticker; on October 16, 2026, 60% of stored items failed this check. Filings, rating actions and insider forms always count. |
| `NC-48` | **Non-events.** Previews ("to report", "what to expect"), opinion and listicles ("should you buy", "3 reasons", "could double", "may") are typed OTHER, with no materiality points: the event, if any, comes later. |
| `NC-49` | **Tilt gate.** The event-study tilt must beat no tilt at all on the walk-forward Brier score, or it is switched off and outlooks show the volatility range with P(up) near 50%. Found on the first backfill (October 16, 2026): over 5,322 events the tilt scored 0.253–0.258 vs 0.25 for a coin, while the ranges held 79–80% of outcomes. |

### 5.8 Outputs

| ID | Requirement |
| --- | --- |
| `NC-27` | **Console**: a **Catalysts** page (live catalysts ranked by strength, filters by type and direction, the confluence panel, social chart of mentions and net sentiment over 30 days, outlook table, track record) and a catalyst panel on Stock lookup (UI §3.2). The Ask tool gains `catalyst <ticker>`. |
| `NC-28` | **Telegram**: a message when a catalyst ≥ `catalyst.alert_strength` (default 60) appears on a universe ticker, in the format shown at the top; at most one per ticker per story; quiet hours as the notifier's. **Off until the gate passes (NC-34)** unless the owner turns it on. `/catalyst <T>` command on request. |
| `NC-29` | **CLI**: `run.sh catalyst [TICKER] [--date D] [--telegram]`, `run.sh catalyst-eval`, `run.sh backfill-catalysts [days]`, and the same in `run.cmd` (the Windows twin). |

## 6. Grading and calibration

| ID | Requirement |
| --- | --- |
| `NC-30` | **History backfill**: `backfill-catalysts 365` pulls up to a year of Finnhub news and EDGAR 8-K/Form 4 for the universe and the 40 large caps, builds stories and types (NC-1..6) with **as-of timestamps**, and stores them. Social history isn't available for free, so social factors are graded only from the day they're collected (logged going forward). |
| `NC-31` | **Walk-forward**: outlooks for each past day use only data before that day (no look-ahead, tested by truncating history as TS-10 does). Event-study buckets are refit monthly on the past only. |
| `NC-32` | **Metrics per horizon**: Brier score of P(up) vs the 50/50 and the "same as SPY" baselines; **coverage** of the 10–90% range (target 80% ± 5%); reliability table (predicted vs actual in deciles); mean excess return of BULLISH and BEARISH stances vs all days; per catalyst type and per strength bucket. |
| `NC-33` | **Feature gate**: a strength factor or adjustment (NC-8, NC-20) stays in only if it improves the walk-forward Brier score or coverage. Otherwise its weight is set to 0, and the report says so. |
| `NC-34` | **Alert gate**: Telegram alerts (NC-28) and the "calibrated" label switch on only after ≥ 100 graded outlooks per horizon with Brier better than baseline and coverage within 75–85%. The gate is re-checked weekly; failing it switches alerts back off with a notice. |
| `NC-35` | Every live outlook is stored and graded when its horizon ends (like A16 for options), so the track record builds itself. |

## 7. Agent design

| Agent | Container | Type | Runs (ET) | Does |
| --- | --- | --- | --- | --- |
| **A3** (extended) | news-sentiment | Code + FinBERT + **social model** + **MiniLM** | Every 15 min (as today); fundamentals at 7:30 AM | Embeddings per item (NC-1), social scores per post (NC-11), earnings, analyst consensus, Form 4, Reg SHO short volume (NC-16) |
| **A17 News catalyst** (new) | trader-core | Code; Qwen types stories rules miss (≤ 20 per run) | 8:45, 10:45, 12:15, 13:45 and 16:25 ET, and on request (`CATALYST`) | Stories and novelty (NC-1..6), strength (NC-7..10), social indicator (NC-11..15), confluence, outlook, stance and plan (NC-16..26) |
| **A17 grader** | trader-core | Code | 16:40 daily; Sundays 18:30 full walk-forward | Grades ended horizons (NC-35); feature and alert gates (NC-33, NC-34) |

The 16:25 run sends Telegram alerts only when `catalyst.telegram` is on **and** the gate passes. Bearish views are described for puts; A14 builds the actual structure on request (OP-13).

No new container. Nothing from A3 or A17 reaches the order path: A17 output is ideas and alerts, as with the scanner (TS) and options (A14) flows.

## 8. Storage (journal)

| Table | Writer | Columns (main) |
| --- | --- | --- |
| `news_items` (+2 columns) | news-sentiment | kind (news, 8-K, rating, post), publisher (the outlet behind the feed, NC-3) |
| `news_embeddings` | news-sentiment | item_id, ticker, model, vec (base64 float16) |
| `social_posts` | news-sentiment | item_id, ticker, ts, source, author (salted hash), score, tag, length, text_hash, model |
| `fundamentals_daily` | news-sentiment | date, ticker, kind (earnings, recommendation, earnings_calendar, insider, short_volume, apewisdom), json |
| `news_stories` | trader-core | story_id, ticker, first_seen, last_seen, ctype, direction, type_source, prompt_version, novelty, items, sources, sentiment, strength, headline, item_ids |
| `catalyst_outlooks` | trader-core | outlook_id, ts, as_of, ticker, story_id, strength, label, direction, ctype, factors, confluence, social, horizons, stance, vehicle, plan, calibrated, text |
| `catalyst_grades` | trader-core | outlook_id, as_of, ticker, horizon, p_up, p10, p90, median, stance, realized, realized_excess, in_range, up, graded_at |

The social indicator is computed from `social_posts` when needed, so there is no `social_daily` table. `trader migrate` adds the two `news_items` columns to existing databases (additive only).

Grants follow DATA_SPEC §3: the console role reads all of these; `trader migrate` applies them.

## 9. Non-functional

| ID | Requirement |
| --- | --- |
| `NC-36` | **Resources**: +≤ 0.8 GB RAM in news-sentiment (both models loaded once, threads capped as FinBERT is); no GPU change; Qwen calls for typing ≤ 20 per cycle and only for stories rules can't type. |
| `NC-37` | **Latency**: a story's strength within 2 minutes of its first item; `run.sh catalyst NVDA` answers in < 10 s from stored data. |
| `NC-38` | **Determinism**: the same inputs give the same scores and ranges; models pinned by hash; Qwen at temperature 0 with a fixed seed (QW-2), cached for replay (RP-2). |
| `NC-39` | **Security**: social text is untrusted (DQ-4); cleaned before any model; never shown raw in Telegram; per-author caps (NC-12). |
| `NC-40` | **Tests**: unit tests per factor and the event-study math; a look-ahead test; injection tests on social text; a recorded-day replay of a catalyst; strict typing and lint as the rest of the codebase. |

## 10. Configuration (new `catalyst:` section)

```yaml
catalyst:
  enabled: true
  horizons: [1, 5, 10, 21]          # sessions
  story_join: 0.75                  # NC-1 cosine similarity (sentence embeddings)
  story_join_hash: 0.45             # NC-1 the same with word-hash vectors (no model)
  story_window_h: 72
  novelty_days: 30                  # NC-2
  min_strength: 40                  # NC-4
  alert_strength: 60                # NC-28
  half_life_h: 24                   # NC-7
  iv_weight: 0.6                    # NC-18
  shrink_k: 30                      # NC-19
  stance_p: 0.56                    # NC-24
  max_posts_per_author: 3           # NC-12
  social_sources: [reddit]          # add stocktwits, apewisdom after verification
  telegram: false                   # NC-28; the gate (NC-34) must also pass
  history_universe: extended        # universe | extended (adds the 40 names in news/names.py)
  adjustments: []                   # NC-20: informational; catalyst-eval decides (NC-33)
  gate_min_graded: 100              # NC-34
  gate_coverage: [0.75, 0.85]
  embed_model: sentence-transformers/all-MiniLM-L6-v2
  social_model: StephanAkkerman/FinTwitBERT-sentiment
```

Source weights (NC-3) are `catalyst.source_weights` (publisher substring → weight) with `default_source_weight: 0.6`. The adjustments actually used come from `reports/catalyst_eval.json`, written by the walk-forward, not from this key.

## 11. Phases

| Phase | Builds | Status (October 16, 2026) |
| --- | --- | --- |
| **1. Stories and strength** | NC-1..10, `news_stories`, MiniLM, rules + Qwen typing, console list, CLI | Built |
| **2. History and outlook** | NC-17..23, NC-30..33, event study, `catalyst-eval` | Built; needs `./run.sh backfill-catalysts 365` once to have history |
| **3. Social** | NC-11..15, social model, Reddit (and Stocktwits/ApeWisdom if verified) | Built; **no data until Reddit keys are set** |
| **4. Confluence and position** | NC-16, NC-24..26, `fundamentals_daily`, sizing by the live risk rules | Built |
| **5. Alerts and gate** | NC-28, NC-34, NC-35, `CATALYST` command | Built; alerts stay off until the gate passes and `catalyst.telegram` is on |
| **6. Optional ML** | LightGBM quantile model | Not built: no graded history to train on yet |

## 12. Out of scope

- Paid news, sentiment or options data (ADR-003); X/Twitter (no free API).
- Placing orders from catalysts (ADR-006); shorting stock (ADR-008).
- Intraday tick-by-tick news trading; the playbook keeps the live day entries.
- Price-target forecasts beyond 1 month.

## 13. Owner decisions (October 16, 2026: the proposed defaults)

1. **Reddit keys:** still to be created. Until `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` are in `.env.news`, the social indicator says "no data" and strength leaves out its 5 social points.
2. **Stocks:** the universe by default; any ticker on request (`./run.sh catalyst AMD`, `CATALYST AMD`). History uses the universe plus the 40 large caps in `news/names.py` (`catalyst.history_universe: extended`).
3. **History:** a one-time `./run.sh backfill-catalysts 365`.
4. **Alerts:** off until the gate passes (NC-34) **and** `catalyst.telegram: true`.
5. **Horizon:** the code picks the horizon with the largest typical move for its uncertainty.
6. **Stocktwits and ApeWisdom:** built, off by default (`catalyst.social_sources: [reddit]`) until their terms are confirmed.

## 14. First results on real data (October 16, 2026)

Backfill: about 70,000 articles for 37 tickers (Finnhub free plan, back to October 2025), embedded with MiniLM; 60% of stored items failed the relevance check (NC-47). That gave **5,359 events** with known outcomes.

| Horizon | Graded | Range hit (target 80%) | Brier with news tilt | Brier, tilt off (used) |
| --- | --- | --- | --- | --- |
| 1 day | 5,271 | 79% | 0.2526 | 0.2500 |
| 1 week | 5,043 | 80% | 0.2552 | 0.2498 |
| 2 weeks | 4,753 | 80% | 0.2576 | 0.2501 |
| 1 month | 4,230 | 79% | 0.2582 | 0.2501 |

**What it means:**

- The **ranges are well calibrated**: the 10–90% band holds the outcome 79–80% of the time at every horizon. That is the useful output (stops, targets, option strikes).
- **News direction gave no edge.** Pooled across 37 large caps, the tilt from past similar stories scored worse than a coin. The NC-49 gate switched it off, so outlooks say P(up) 50%, and the view is NEUTRAL or NO_TRADE unless a future re-grade finds an edge.
- The alert gate (NC-34) is not passed, and cannot be while there is no directional edge, so Telegram catalyst alerts stay off.
- The weekly re-grade (Sundays 18:30) keeps testing. Social data (once Reddit keys exist) and a longer history may change this; that will show in the walk-forward numbers before anything changes in the outlooks.
