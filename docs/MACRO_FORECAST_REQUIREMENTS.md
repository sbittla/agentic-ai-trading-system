# Macro, Forecast and Technical Additions (MA, FC, TS-20..)

**Version:** 1.0 (October 16, 2026) · Status: **built, advisory** · Companion to [NEWS_CATALYST_REQUIREMENTS.md](NEWS_CATALYST_REQUIREMENTS.md), [SCANNER_REQUIREMENTS.md](SCANNER_REQUIREMENTS.md) and [GAP_REVIEW.md](GAP_REVIEW.md)

Three additions that make the system's views depend on more than one stock's own chart and news:

1. **Macro agent (A18):** US economic data, Fed and economy news, and a daily **macro regime** (risk-on, neutral or risk-off) with the reasons behind it.
2. **Forecasts:** the predictions that markets let you make with some reliability. These are volatility ranges, gap behaviour, reactions to scheduled macro events and sector leadership, each graded on history.
3. **Technicals:** accumulation and distribution, a base-breakout (VCP) technique, and the market's distribution-day count.

All of it is advisory. Nothing here sizes or places an order (ADR-006). The regime and forecasts feed the existing views: the catalyst confluence panel, the outlook's event variance and the scanner context.

```bash
./run.sh macro                 # today's macro regime, latest releases, Fed and economy headlines
./run.sh macro-backfill        # once: history of every series (free FRED files, no key)
./run.sh forecast [TICKER]     # volatility range, gap odds, macro-event reactions, sector ranks
./run.sh forecast-eval         # grade every forecast walk-forward; writes reports/forecast_eval.json
```

`run.cmd` takes the same commands. In Telegram or the console, `MACRO` shows the regime. trader-core refreshes the regime at 8:40 ET and after 4:30 PM, and the news service pulls data and headlines at 7:00 ET and every 2 hours in the session.

## 1. Decisions

| Question | Decision | Why |
| --- | --- | --- |
| Where do economic numbers come from? | **FRED public CSV files** (`fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>`), no API key | Free, official (St. Louis Fed), covers BLS, BEA, Treasury, ICE BofA spreads and CBOE VIX. Checked reachable from the containers on October 16, 2026 |
| Fed news | **Federal Reserve press RSS** (`federalreserve.gov/feeds/press_all.xml`) | Statements, minutes and speeches at the source. The BLS RSS returned 404, so BLS numbers come through FRED |
| Economy headlines | **Finnhub general news** (`/news?category=general`, the existing key) | Same feed and budget as company news |
| Consensus expectations | **Not available free.** A release's surprise is measured against its own trend (the average change of the prior 6 releases) and labelled so | Consensus feeds are paid (ADR-003) |
| Regime model | **Fixed, published rules** (yield curve, credit spreads, VIX term structure, Sahm rule, inflation trend, trend and breadth of the market), not a fitted model | Few regimes in any history; a fitted model would learn the noise. The rules are graded by what followed (MA-9) |
| Volatility forecast | **Three estimators graded walk-forward** (EWMA, Parkinson range, HAR); the report uses the one with the lowest QLIKE loss | Volatility is predictable; the best estimator differs by stock |
| New models | None | Statistics only; FinBERT (existing) scores macro headlines |

## 2. Macro agent (A18)

| ID | Requirement |
| --- | --- |
| `MA-1` | **Series.** Stored per date in `macro_series`: CPI and core CPI (`CPIAUCSL`, `CPILFESL`), core PCE (`PCEPILFE`), unemployment (`UNRATE`), payrolls (`PAYEMS`), initial claims (`ICSA`), industrial production (`INDPRO`), retail sales (`RSAFS`), consumer sentiment (`UMCSENT`), fed funds (`DFF`), 2- and 10-year Treasury (`DGS2`, `DGS10`), curve 10y−2y and 10y−3m (`T10Y2Y`, `T10Y3M`), high-yield spread (`BAMLH0A0HYM2`), VIX and 3-month VIX (`VIXCLS`, `VXVCLS`), dollar index (`DTWEXBGS`), WTI oil (`DCOILWTICO`). The list is config (`macro.series`). |
| `MA-2` | **Collection.** news-sentiment downloads each series at 7:00 ET and after 4:30 PM ET (once a day per series is enough; FRED updates daily at most). A failing series is logged and skipped. `macro-backfill` loads the full history once. |
| `MA-3` | **Releases.** A new observation of a monthly or weekly series is a release. Its surprise is (change − mean change of the prior 6 observations) ÷ their standard deviation, labelled "vs trend, not consensus". |
| `MA-4` | **Macro news.** Fed press releases (RSS) and Finnhub general news are cleaned (DQ-4), scored by FinBERT and typed by rules into themes: `FED`, `INFLATION`, `JOBS`, `GROWTH`, `RATES`, `CREDIT`, `TRADE`, `GEOPOLITICS`, `FISCAL`, `ENERGY`, `OTHER`. Stored in `macro_news` with source, theme, sentiment and a risk tone (risk-on, risk-off, neutral). |
| `MA-5` | **Regime indicators.** Each indicator gives −1 (risk-off), 0 or +1 (risk-on) with its value and the threshold it crossed: curve (10y−3m < 0 is −1); credit (HY spread above its 1-year median by > 0.5 pts is −1, below by > 0.3 is +1); VIX level (> 25 is −1, < 15 is +1); VIX term structure (VIX ÷ VIX3M > 1 is −1, the stress sign); labour (Sahm rule ≥ 0.5 is −1); inflation (3-month annualised core CPI above the 12-month rate by > 0.5 pts is −1); dollar (63-day change > +4% is −1); oil (63-day change > +25% is −1); market trend (SPY above its 200-day is +1, below −1); distribution days (TS-22: ≥ 5 in 25 sessions is −1). |
| `MA-6` | **Regime.** Score = sum of indicators ÷ the number with data × 100 (−100…+100). `RISK_ON` ≥ +25, `RISK_OFF` ≤ −25, else `NEUTRAL`. Stored daily in `macro_daily` with every indicator; computed from data published before the as-of day (no look-ahead). |
| `MA-7` | **Use.** The regime is a check in the catalyst confluence panel (NC-16), in the scanner context (shown, not scored until MA-9 shows it helps), and in the morning message. It never blocks a trade by itself; RG-1/RG-2 keep that job. |
| `MA-8` | **Outputs.** `trader macro` (text, `--json`, `--telegram`), the `MACRO` command, the console **Macro** page (regime, indicators, latest releases with surprise, Fed and economy headlines) and `reports/macro.json`. |
| `MA-9` | **Grading.** `trader macro --eval` reports, for each past regime label, the next 21-session SPY return and realized volatility, and how often each label occurred. A label that does not separate outcomes is reported as not useful. |

## 3. Forecasts (FC)

| ID | Requirement |
| --- | --- |
| `FC-1` | **Volatility.** For each ticker, the next-session and next-5-session volatility from three estimators: EWMA (λ 0.94) of close-to-close returns, Parkinson (high-low range, 20 sessions) and HAR (daily, weekly and monthly Parkinson variance, weights fitted by least squares on the past only). Output: σ and the 80% move range (±1.28σ) for 1 and 5 sessions. |
| `FC-2` | **Volatility grading.** Walk-forward over the stored history: QLIKE loss per estimator and coverage of the 80% range. The estimator with the lowest loss per ticker is the one reported (`forecast_eval.json`). |
| `FC-3` | **Gap odds.** From daily bars: for opening gaps grouped by size in ATR (0.25–0.5, 0.5–1, 1–2, > 2) and direction, the share that **filled** the same day (traded back to the prior close) and the share that **closed beyond the open** (gap-and-go), per ticker and pooled, with counts. |
| `FC-4` | **Macro-event reactions.** For each ticker, the average absolute return and absolute excess return on CPI, jobs-report and FOMC days, vs other days, as a multiplier. Dates come from the macro calendar, including its `history` section. The catalyst outlook uses the multiplier instead of a fixed 1.0 σ for events in its horizon (NC-18); with fewer than 5 events it stays 1.0. |
| `FC-5` | **Sector rotation.** For the 11 SPDR sector ETFs plus SMH: momentum = 6-month return excluding the last month, and 3-month relative strength vs SPY. Ranked; each universe ticker shows its sector's rank. Graded walk-forward: the forward 21-session return of the top 3 vs the bottom 3, month by month. |
| `FC-6` | **Outputs.** `trader forecast [TICKER]` (text and `--json`), `reports/forecast.json`, a forecast card on the console Macro page, and `trader forecast-eval`. Forecasts never change an order; the ranges are shown next to stops and targets so the owner sees whether a target is inside the usual move. |

## 4. Technical additions (TS-20..TS-23)

| ID | Requirement |
| --- | --- |
| `TS-20` | **Accumulation / distribution.** Per bar: OBV slope over 20 sessions vs price slope, the Accumulation/Distribution line and Chaikin Money Flow (20). The idea context carries `accumulation` (+1 accumulation, −1 distribution, 0) and `cmf20`; shown on the idea, measured by `scan-eval`, not scored until it shows an edge (TS-8). |
| `TS-21` | **Base breakout (VCP).** A swing technique: a base of 20–60 sessions no deeper than 25% from its high, with contracting ranges (the last 10 sessions' range ≤ 60% of the base's range) and volume drying up (last 10 sessions' average ≤ 80% of the 50-day), then a close above the base high on ≥ 1.5× volume. Trigger above the bar's high; stop below the last 10 sessions' low. Mirrored as `BASE_BREAKDOWN` (bearish view, options only). Graded by `scan-eval` like every technique. |
| `TS-22` | **Distribution days.** For SPY: sessions in the last 25 that closed down ≥ 0.2% on higher volume than the session before (IBD's definition). Shown in the macro regime (MA-5) and the scanner context. |
| `TS-23` | **No look-ahead** for TS-20..TS-22, tested as TS-10. |

## 5. Storage

| Table | Writer | Columns |
| --- | --- | --- |
| `macro_series` | news-sentiment | series_id, date, value (unique per series and date) |
| `macro_news` | news-sentiment | item_id (unique), ts, source, headline, cleaned_text, theme, tone, sentiment, suspicious |
| `macro_daily` | trader-core | date (unique), score, label, indicators (JSON), releases (JSON) |

Forecasts are computed on demand from bars and written to `reports/forecast.json`; their grades go to `reports/forecast_eval.json`.

## 6. Configuration (new `macro:` and `forecast:` sections)

```yaml
macro:
  enabled: true
  series: [CPIAUCSL, CPILFESL, ...]   # MA-1 (FRED ids)
  risk_on_at: 25                      # MA-6
  risk_off_at: -25
  news_categories: [general]          # MA-4 Finnhub
forecast:
  sector_etfs: [XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLB, XLU, XLRE, XLC, SMH]
  gap_buckets_atr: [0.25, 0.5, 1.0, 2.0]
```

## 7. Out of scope

- Consensus forecasts for releases (paid). Surprises are vs trend.
- Intraday release reaction (needs minute bars around 8:30; RG/TW rules already protect the windows).
- A fitted regime classifier (too few regime changes in any free history).

## 8. First results on real data (October 16, 2026)

- **Regime:** NEUTRAL (−20). Risk-off signs: core CPI re-accelerating (3-month annualised 3.4% vs 12-month 2.6%), oil +26% in 63 days, 8 distribution days in the last 25 sessions. Risk-on: SPY above its 200-day. Curve, credit, VIX and the Sahm rule were neutral.
- **Volatility (FC-2):** EWMA had the lowest QLIKE on 9 of the 10 stocks (HAR on AVGO), and its 80% range held 79–86% of next-day moves. The range-based estimators were too narrow (68–80%), since they miss overnight gaps.
- **Sector rotation (FC-5):** top 3 minus bottom 3 +1.08% a month, but over only 7 months, with a 43% hit rate and t = 0.35. **Not significant**: shown as context, not a signal.
- **Macro days (FC-4):** FOMC reactions measured from 14 meetings (2025–2026); none of the 10 stocks moved more than 1.1× its normal day. CPI and jobs have 1 date each, so they stay at the default until past dates are added to the calendar.
- **Data notes:** FRED stalls requests whose User-Agent has no contact address, so the SEC User-Agent from config is used. FRED serves only about 3 years of the ICE high-yield spread.
