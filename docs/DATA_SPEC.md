# Data Specification

**Version:** 1.0 (September 30, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md)

Defines every external data source, how often it is called, the internal tables the agents write, and the exact definition of every indicator used in the playbook. Endpoint paths should be verified against each provider's current docs during the build.

---

## 1. External sources

| Source | What we use | Endpoint / method | Frequency | Budget |
| --- | --- | --- | --- | --- |
| Alpaca Market Data (free, IEX) | Real-time 1-min bars, trades, quotes for 10 tickers + SPY, QQQ, SMH | WebSocket `wss://stream.data.alpaca.markets/v2/iex` | Streaming 9:25–4:05 | 13 of 30 stream symbols |
| Alpaca Market Data | Historical daily and 1-min bars (back-fill, backtests) | REST `GET /v2/stocks/bars` | After close; on reconnect | Under 200 calls/min |
| Alpaca Trading API | Account, positions, orders, calendar, clock | REST `/v2/account`, `/v2/positions`, `/v2/orders`, `/v2/calendar`, `/v2/clock` | Reconcile every minute | Low |
| Finnhub | Company news, earnings calendar, company profile (market cap) | REST `company-news`, `calendar/earnings`, `stock/profile2` | News every 15 min; calendar daily; profile weekly | ~10 calls per cycle; under 60/min |
| SEC EDGAR | Recent filings (8-K), shares outstanding | `data.sec.gov/submissions/CIK##########.json`, `companyfacts` | Every 15 min (filings); weekly (facts) | Fair-access; set User-Agent to name + email |
| Finnhub | Analyst upgrades/downgrades (UI-33) | REST `stock/upgrade-downgrade` (premium on some plans; turned off for the session on 401/403) | Every 30 min per ticker | 10 calls per 30 min |
| Yahoo Finance / Google News RSS | Rating-action headlines (UI-33), parsed by rules; the headline must name the ticker or company as the subject | `feeds.finance.yahoo.com/rss/2.0/headline?s=<T>`, `news.google.com/rss/search?q=...` | Every 30 min per ticker; `./run.sh backfill` once | 20 requests per 30 min, User-Agent set |
| Reddit (PRAW) | Posts and comment counts mentioning the tickers | r/stocks, r/investing, r/wallstreetbets, r/StockMarket search | Every 15 min | Under 100 queries/min; non-commercial |
| Finnhub | Earnings surprises, analyst consensus, next earnings date (NC-16) | REST `stock/earnings`, `stock/recommendation`, `calendar/earnings` | 7:30 AM daily; `backfill-catalysts` once | ~2 calls per ticker a day; an endpoint refused on the plan is skipped (logged once) |
| SEC EDGAR | Form 4 open-market insider trades (NC-16) | `submissions` JSON, then each Form 4 XML (read with regular expressions) | 7:30 AM daily, last 90 days, ≤ 25 filings per ticker | < 10 requests/s with the User-Agent |
| FINRA Reg SHO | Daily short-sale volume share (NC-16) | `cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt` (public file) | 7:30 AM daily, last 20 sessions | ~25 files a day |
| FRED (St. Louis Fed) | Economic series: CPI, PCE, payrolls, unemployment, claims, yields, curve, credit spreads, VIX, dollar, oil (MA-1) | Public CSV `fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>` (no key) | 7:00 ET, every 2 h in session, after 16:30; `macro-backfill` once | ~20 files per run |
| Federal Reserve | Press releases, FOMC statements, minutes, speeches (MA-4) | RSS `federalreserve.gov/feeds/press_all.xml` | With the FRED runs | 1 request per run |
| Finnhub | Market and economy headlines (MA-4) | REST `news?category=general` | With the FRED runs | 1 call per run |
| Stocktwits (off) | Messages with users' own Bullish/Bearish tags (NC-11) | `api.stocktwits.com/api/2/streams/symbol/<T>.json` | With A3 when `catalyst.social_sources` has `stocktwits` | Off until its terms are confirmed |
| ApeWisdom (off) | Reddit mention counts per ticker (NC-11) | `apewisdom.io/api/v1.0/filter/all-stocks/page/<n>` | 7:30 AM when enabled | Off until its terms are confirmed |
| Alpaca Market Data (SIP history) | 10 years of consolidated daily bars for `momentum.universe` (MO-22), stored as kind `daily_sip` | REST `GET /v2/stocks/bars?feed=sip`, the request ending 16 minutes before now (free plan) | `momentum-history` once; the 16:50 job tops up stale symbols | ~115 calls a day |
| Alpaca snapshots | Last trade and day bar for armed and open momentum signals (MO-15) | REST `GET /v2/stocks/snapshots` (IEX) | Every 5 min, 9:36–15:50 ET, only while signals are armed or open | 1 call per run |
| Company event calendar | Past and upcoming dates of product launches, developer conferences and keynotes (EV-1) | `config/corporate_events.yaml`, kept by hand from company announcements | Edited when a company announces a date | none |
| Macro calendar | CPI, jobs report, FOMC dates | Published BLS and Federal Reserve schedules, entered in config yearly | Yearly, checked monthly | — |

**Data quality rules**

- DQ-1: Every record stores `source`, `received_at` and `event_time`. Agents use `event_time`; staleness uses `received_at`.
- DQ-2: IEX-only data means partial volume. RVOL and volume filters always compare IEX volume with IEX history, never with consolidated figures from other sites.
- DQ-3: Bars with zero volume or prices more than 5% from the prior bar are flagged and excluded until confirmed by the next bar.
- DQ-4: All fetched text (news, posts, filings) is untrusted. It is stored raw, but only cleaned text (no URLs, no markup, truncated to 2,000 characters) is ever passed to Qwen.
- DQ-5: Gaps found on reconnect are back-filled from REST within 2 minutes; until then, indicators depending on them are marked incomplete and block entries.

---

## 2. Indicator definitions

All intraday indicators reset at 9:30 ET and use regular-session bars only.

| Indicator | Definition |
| --- | --- |
| VWAP | Σ(typical price × volume) / Σ(volume) from 9:30, typical price = (high + low + close) / 3, on 1-min bars |
| Opening range (OR5, OR15) | High and low of 9:30–9:35 and 9:30–9:45 |
| RVOL (time of day) | Cumulative volume since 9:30 ÷ average cumulative volume at the same minute over the last 20 sessions |
| ATR(5-min) | 14-period Wilder ATR on 5-min bars, carried across days |
| ATR(14-day) | 14-period Wilder ATR on daily bars |
| EMA 9 / 20 (intraday) | On 5-min bars, carried across days |
| EMA 20 / 50 (daily) | On daily closes |
| Pre-market high / low | From 4:00–9:30 bars if the free feed provides them; otherwise marked unavailable and setups using them are disabled |
| Gap % | (Today's open − prior close) / prior close |
| Relative strength vs QQQ | Stock % change from open − QQQ % change from open |
| Day range position | (Close − low of day) / (high of day − low of day) |
| R (risk unit) | Entry − stop, per share |

The existing alert-bot indicators (RSI, MACD, Bollinger, Keltner, OBV, CMF) are computed and stored for the strategy and review agents to read, but no playbook trigger depends on them in v1.

---

## 3. Journal tables

Live and paper trading use **Postgres 16** (schema `journal`, ADR-012), shared by the containers with one role each (`docker/postgres/init.sql`). Replay and backtest runs write a per-run SQLite file with the same schema, so runs are self-contained. Times stored in UTC; reports shown in ET.

**Writers by container:** `trader-core` writes every table except `news_items`, `event_flags` and `analyst_actions` (written by `news-sentiment`), inbound commands (written by `notifier` and `console` into `outbox`), and `owner_notes` / `console_chats` (written by `console`, which also logs its Ask calls to `llm_calls`). The `console_rw` role reads every table and is refused every other write (`trader migrate` grants, `upgrade.sql`). Backups and Grafana use the read-only role, which also reads every sequence so `pg_dump` works (found by the OPS-1 restore test).

**Outbox bus (cross-container):** table `journal.outbox` (id, created_at, source, channel, payload JSONB, consumed_at). Channels: `news_flags` (news → core), `outbound_msg` (core → notifier), `inbound_cmd` (notifier and console → core; the trigger forces `source = console` for the console role), `heartbeat` (all). A trigger enforces which role may publish on which channel and issues `NOTIFY`.

| Table | Key columns | Written by |
| --- | --- | --- |
| `config_snapshots` | id, effective_date, yaml_hash, yaml_text | A10 at start |
| `universe` | effective_date, ticker, rank, market_cap, on_owner_list | A1 |
| `bars_1m` | ticker, ts, o, h, l, c, v, vwap, source | A2 (daily history is Parquet, not a table) |
| `levels` | date, ticker, prior_high, prior_low, prior_close, pm_high, pm_low, ema20d, ema50d, atr14d | A4 |
| `news_items` | id, ticker, source, published_at, headline, cleaned_text, finbert_score | A3 |
| `analyst_actions` | ts, ticker, firm, action (upgrade, downgrade, initiate, reiterate, pt_raise, pt_cut), from_rating, to_rating, pt_old, pt_new, headline, source, url, action_hash (unique) | A3 ratings feed (UI-33) |
| `market_recap` | run_at, as_of, ticker, metrics_json (returns, strength against QQQ, ATR, levels, tags) | `trader recap` / `RECAP` (UI-34) |
| `event_flags` | date, ticker, flag (EARNINGS_TODAY, EARNINGS_3D, NEW_8K, RATING_CHANGE, SOCIAL_SPIKE, FOMC, CPI), detail | A3 |
| `game_plans` | id, date, window, ticker, setup, trigger, stop, target, notes, off_limits[] | A5 |
| `setups` | id, ts, ticker, style, window, setup_name, trigger, stop, t1, t2, score, filters_json | A4 |
| `proposals` | proposal_id, setup_id, json, valid, model, prompt_version, latency_ms, tokens | A5 |
| `reviews` | proposal_id, verdict, main_risk, rules_broken[], json | A6 |
| `compliance_checks` | proposal_id, result, locks_found[] | A12 |
| `decisions` | proposal_id, result (APPROVE/REJECT/NEEDS_APPROVAL), reason_code, qty, risk_usd, checklist_json | A7 |
| `approvals` | proposal_id, sent_at, answered_at, answer | A7 / WhatsApp |
| `orders` | client_order_id, alpaca_id, proposal_id, leg, type, qty, limit, stop, status, ts | A8 |
| `fills` | alpaca_id, ts, qty, price | A8 |
| `positions` | position_id, ticker, style, opened_at, closed_at, qty, avg_entry, avg_exit, pnl, r_multiple, exit_reason | A8 / A9 |
| `tax_lots` | lot_id, ticker, acquired_at, qty, cost, adjusted_basis, disposed_at, proceeds, disallowed_loss, wash_link_lot_id | A12 |
| `locks` | ticker, lock_type (WASH, EARNINGS, LOSS_STREAK, REVENGE), from, until, reason | A12 / A7 |
| `daily_metrics` | date, trades, wins, losses, pnl, peak_pnl, max_dd, expectancy_r, rule_breaches | A11 |
| `llm_calls` | id, agent, prompt_version, input_hash, output, valid, latency_ms, tokens | ollama client |
| `incidents` | id, ts, type, detail, resolved_at | A10 |
| `commands` | ts, sender, text, accepted, reason, source (telegram, whatsapp, local, console) | CommandAuthority (SEC-14, UI-13) |
| `status_snapshots` | ts, json (equity, health, positions, window …) | trader-core heartbeat; the console's fallback (UI-27) |
| `next_day_ideas` | run_at, plan_for, ticker, rank, status, score, screen fields, morning/afternoon setup, trigger, stop, target, outcome fields | A5-1 |
| `equity` | ts, equity, hwm | A10 |
| `latency` | proposal_id, setup_id, ticker, setup, mode, trigger_ts, order_ts, latency_s, model_s, owner_s, prearmed, outcome (ORDER/MISSED), reason_code | engine (LAT-1, LAT-4) |
| `shadow_trades` | setup_id, proposal_id, ts, ticker, setup, window, direction, taken, reason_code, entry, stop, t1, t2, filled_at, closed_at, exit_price, exit_reason, r_multiple, status | engine (SH-1, SH-2) |
| `alerts_log` | ts, ticker, kind, direction, label (raw/qualified), checklist_score, first_failed, quality, delivery, price, stop, t1, text, plan_json, hit, graded_at | core alerts (AL-1..AL-7) |
| `regime_daily` | date, regime, spy_atr_ratio, spy_gap_pct, detail | A4 (RG-1) |
| `data_divergence` | date, ticker, IEX and SIP opening-range high/low, or_diff_pct, iex_volume_share_pct, setups_checked, setups_within | engine (DQ-6, DQ-7) |
| `external_trades` | imported_at, broker, account, ts, ticker, side, qty, price, fees, source_file, row_hash (unique per import) | `trader import-trades` (WS-9) |
| `in_play` | date, ticker, gap_pct, pm_volume_ratio, news, rank, on_list | engine (UN-1, UN-2) |
| `ops_checks` | ts, kind (restore_test/drill), passed, detail | `trader restore-test`, `trader drill` (OPS-1, OPS-2) |
| `options_iv_daily` | date, ticker, atm_iv_30d, iv_rank, iv_pct, expected_move_30d, history_days | A13 (A13-3, A13-6) |
| `option_suggestions` | suggestion_id, ts, ticker, direction, source, style, structure, expiry, legs_json, contracts, entry_price (+debit/−credit), max_loss, max_gain, breakeven, underlying, invalidation, t1, iv_rank, detail_json (view, numbers, entry quotes), text, status, mark, exit_price, exit_reason, closed_at, pnl, r_multiple, stock_view_right | A14 / A16 |
| `owner_notes` | ts, ref_type, ref_id, tags, note, screenshot_b64 | console (UI-29) |
| `console_chats` | ts, day, question, answer, tool, rows_json, model_used | console (UI-8d) |
| `macro_series` | series_id, date, value (unique per series and date) | news-sentiment (MA-1) |
| `macro_news` | item_id (unique), ts, source, headline, cleaned_text, theme, tone, sentiment, suspicious | news-sentiment (MA-4) |
| `macro_daily` | date (unique), score, label, indicators, releases | trader-core A18 (MA-6) |
| `momentum_signals` | signal_id (unique: ticker:setup:asof), ticker, setup, policy, asof, state (ARMED, OPEN, CLOSED, EXPIRED, SKIPPED), entry_type, trigger, stop, rs, template, proven, alerted, shares, risk_usd, entry_date, entry_price, exit_date, r, exit_reason, position (exit state machine JSON), sent (alert keys), events, why, updated | trader-core A19 (MO-15) |
| `event_ideas` | idea_id (unique: group:event_date:window), group_id, ticker, kind, side, window, event_date, estimated, enter, exit, verdict (ok, earnings_in_window, earnings_unknown), earnings_date, basis, n, mean_pct, same_sign_rate, p, first_seen, updated, realized_pct (net of SPY), realized_raw_pct, graded | trader-core A20 (EV-9) |
| `news_embeddings` | item_id (unique), ticker, model, vec (base64 float16) | news-sentiment (NC-1) |
| `social_posts` | item_id (unique), ticker, ts, source, author (salted hash), score, tag, length, text_hash, model | news-sentiment (NC-11) |
| `fundamentals_daily` | date, ticker, kind (earnings, recommendation, earnings_calendar, insider, short_volume, apewisdom), json | news-sentiment (NC-16) |
| `news_stories` | story_id (unique), ticker, first_seen, last_seen, ctype, direction, type_source, prompt_version, novelty, items, sources, sentiment, strength, headline, item_ids | trader-core A17 (NC-1..NC-10, NC-46) |
| `catalyst_outlooks` | outlook_id (unique), ts, as_of, ticker, story_id, strength, label, direction, ctype, factors, confluence, social, horizons, stance, vehicle, plan, calibrated, text | trader-core A17 (NC-17..NC-26) |
| `catalyst_grades` | outlook_id, as_of, ticker, horizon, p_up, p10, p90, median, stance, realized, realized_excess, in_range, up, graded_at | trader-core A17 (NC-35) |

`news_items` also carries `kind` (news, 8-K, rating, post) and `publisher` (the outlet behind the feed, NC-3). `trader migrate` adds new nullable columns to existing tables; it never drops, renames or retypes one.

**Retention:** journal kept for 7 years (tax records). Raw news text may be pruned after 90 days; headlines and scores are kept.

---

## 4. Message types (event bus)

All are Pydantic models in `src/trader/models/`, serialized to the journal before delivery.

`BarClosed` · `LevelsReady` · `EventFlagsUpdated` · `GamePlanPublished` · `SetupTriggered` · `TradeProposal` · `Review` · `ComplianceResult` · `Decision` · `ApprovalRequest` / `ApprovalResponse` · `OrderSubmitted` · `OrderFilled` · `OrderCancelled` · `PositionOpened` · `StopMoved` · `PositionClosed` · `LockCreated` · `LimitHit` · `KillSwitch` · `HealthStatus` · `ReportPublished`

The `TradeProposal` schema is defined in [AGENT_PROMPTS.md](AGENT_PROMPTS.md#5-tradeproposal-schema).
