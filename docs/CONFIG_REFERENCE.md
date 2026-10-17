# Configuration Reference

**Version:** 1.0 (September 30, 2026) · File: [`config/config.example.yaml`](../config/config.example.yaml)

One YAML file controls every tunable value. It is loaded once at start, snapshotted into the journal, and frozen; no agent can change it. Edits take effect the next trading day (SR-5). Secrets never go in this file.

## Change rules

- **CF-1:** Every change is a git commit with a one-line reason.
- **CF-2:** A change to anything under `risk`, `wash_sale` or `universe` is made outside market hours and noted in the next daily report.
- **CF-3:** Loosening a risk limit (larger risk, more trades, wider windows) requires 20 paper trades under the new value before it applies to live trading.
- **CF-4:** On start, A10 validates the file against the rules below and refuses to start if any check fails.

## Sections

| Section | Key fields | Validation at start |
| --- | --- | --- |
| `mode` | `environment` (paper/live), `day_trades` and `swing_trades` (paper/approve/auto) | `auto` not allowed until the go-live gates in TEST_PLAN.md are recorded as passed |
| `universe` | `tickers` (exactly the owner's list), `context_symbols`, `substantially_identical`, `correlated_groups`, `min_history_days` | 1–10 tickers; tickers + context ≤ 30 (free stream limit); each ticker tradable on Alpaca |
| `windows` | Morning and afternoon entry and flatten times; macro-day overrides | Morning entries ≥ 09:35; afternoon entries ≤ 15:50; flatten after last entry; times in ET |
| `filters` | QQQ/SMH checks, `min_rvol`, `max_spread_pct`, halt look-back | `min_rvol` ≥ 1.0 |
| `entries` | Chase limit, stop width, minimum R, confidence, unplanned size factor, cancel rules | `min_reward_risk` ≥ 1.5; `min_confidence` ≥ 0.5 |
| `exits` | Targets, partial size, trail method, time stops, swing hold limit | `t1_fraction` between 0 and 1 |
| `risk` | All loss limits and behaviour limits from REQUIREMENTS.md section 6, including the portfolio limits `max_open_risk_pct`, `max_pair_correlation`, `correlation_lookback`, `max_beta_exposure_pct`, `max_gap_loss_pct`, `gap_percentile` (PF-1..PF-4) | `risk_per_trade_pct` ≤ 1.0; `daily_loss_limit_pct` ≤ 3.0; `max_leverage` ≤ 1.0; `max_open_risk_pct` between `risk_per_trade_pct` and 5; correlation 0.3–1.0; beta exposure 25–300%; gap loss 0–5% |
| `execution` | Slippage feedback (EXQ-1); pre-trade order checks `max_price_deviation_pct`, `max_adv_pct`, `max_order_notional` (OC-1..OC-4) | price band 0.1–5%; volume share 0–10%; notional ≥ 0 (0 = off) |
| `macro` | FRED series list, regime thresholds `risk_on_at` / `risk_off_at`, Finnhub news categories, report file (MA-1..MA-8) | `risk_off_at` < 0 < `risk_on_at` ≤ 100; FRED ids upper-case |
| `forecast` | Sector ETFs and each ticker's sector, gap-size buckets in ATR, minimum events for a macro multiplier, report files (FC-1..FC-6) | — |
| `momentum` | Momentum A19 (MOMENTUM_REQUIREMENTS.md): `enabled`, `telegram`, `alert_unproven`, `universe` (about 110 liquid stocks), `history_days`, `feed` (`sip`), liquidity filters `min_price` / `min_dollar_volume`, `rank_factors`, `leaderboard_n`, the leader filter `min_rs` / `min_template`, `disabled` setups, `setup_policy`, exits (`partial_frac`, `partial_r`, `partial_days`, `climax_atr`, `climax_frac`, `time_stop_days`, `max_hold_days`, `max_loss_pct`, `cost_bps`), sizing (`risk_pct`, `max_positions`, `max_heat_pct`, `max_correlated`), the rotation model (`top_n`, `hold_rank`, `rebalance_weeks`, `rotation`, `rotation_factors`, `rotation_regime_filter`, `rotation_max_weight`), the gate (`min_trades`, `min_pf`), `earnings_block_sessions`, `intraday_every_min`, `watch_only` setups (MO-26), `sma200_watch_pct` (MO-27), `earnings_max_age_days` (MO-18, fail closed), report files | known factors, setups and policies (also in `watch_only`); `sma200_watch_pct` 0–25; `earnings_max_age_days` 1–10; fractions 0–1; `max_loss_pct` 0–25; `min_rs` 1–99; `min_template` 0–8; `risk_pct` ≤ `risk.risk_per_trade_pct`; `hold_rank` ≥ `top_n`; `intraday_every_min` 1–30 |
| `events` | Company events A20 (EVENT_REQUIREMENTS.md): `enabled`, `file` (the calendar, `config/corporate_events.yaml`), `pre_sessions` / `post_sessions` (10), the pattern bar `min_years` (6), `min_hit` (0.7), `max_p` (0.10), `min_move_pct` (1.0), `lookahead_days` (90), `telegram`, `latest_file` | pre/post 1–60; `min_years` ≥ 3; `min_hit` 0.5–1; `max_p` 0–1; `lookahead_days` 1–400 |
| `catalyst` | News catalysts (NEWS_CATALYST_REQUIREMENTS.md §10): horizons, story thresholds, strength and alert thresholds, half-life, IV weight, shrinkage, stance probability, social sources, `telegram`, history universe, gate, models, source weights, report files | thresholds 0–100 with min ≤ alert; IV weight 0–1; stance 0.5–1; known social sources and adjustments; horizons 1–63 sessions |
| `wash_sale` | `policy`, `lock_days`, balanced threshold, year-end date | `lock_days` ≥ 31 for strict |
| `data` | Feed, staleness, polling, subreddits, SEC User-Agent | SEC User-Agent contains an email |
| `llm` | Ollama URL, pinned model tag, temperature, timeout, prompt versions | `temperature` = 0; prompt files exist |
| `notifications` | WhatsApp on/off, approval timeout | Timeout ≤ 120 s |
| `storage` | Env var holding the Postgres URL, bar path, backup time | Env var present; Postgres reachable |
| `services` | Heartbeat interval, staleness limits for news and notifier, notifier polling intervals | Approval polling ≤ 5 s |

### Fields added by REQUIREMENTS 1.3, the options layer and the console

Every field has a default (`src/trader/config.py`); add a section to `config/config.yaml` only to change it.

| Section | Fields (default) | Validation at start |
| --- | --- | --- |
| `mode` | `day_trades` / `swing_trades` also accept `copilot` (CP-1); `account_type` `auto`/`cash`/`margin` (AC-1) | a set `account_type` that differs from Alpaca's blocks the day (AC-2) |
| `latency` | `budget_s` 10, `prearm` true, `prearm_atr` 0.3, `prearm_tolerance_atr` 0.1, `approval_grace_s` 30 (LAT-1..LAT-4) | `budget_s` ≤ 30 |
| `windows.macro_days` | `release_block_from` 09:57, `release_block_until` 10:05, `speech_block_before_min` 3, `speech_block_after_min` 5, `breakeven_at_r` 0.5 (TW-6) | block inside the morning window, from < until |
| `regime` | `high_vol_atr_ratio` 1.5, `high_vol_gap_pct` 1.5, `risk_factor_high_vol` 0.5, `gap_and_go_max_normal_x` 2.0, `trend_day_vwap_pct` 80, `trend_day_high_after` 13:00 (RG-1..RG-3) | ratio ≥ 1.0; factor 0–1 |
| `data` | `quote_max_age_s` 2 (DQ-8), `news_poll_open_s` 60 (NW-1), `sip_divergence_pct` 0.05, `sip_gate_days` 20 (DQ-6, DQ-7) | `news_poll_open_s` ≥ 30 (Finnhub limit); live on the IEX feed needs the `data_sip` gate |
| `alerts` | `enabled` true, `schedule` `windows`/`all_day`, `per_symbol_cap` 3, `per_window_cap` 12, `top_n` 5, `digest_min` 10, `grade_window_min` 30, `min_hit_rate` 0.4, `min_graded` 50, `disabled_kinds` (AL-1..AL-7) | caps ≥ 1 |
| `notifications` | `approval_chart` true, `approval_live_update_s` 5 (AP-1..AP-3), `notifier_down_after_s` 120 (NF-1) | — |
| `execution` | `slippage_switch_r` 0.1, `slippage_window` 10 (EXQ-1); stops are always stop-market (EX-5, not configurable) | `slippage_switch_r` > 0 |
| `shadow` | `enabled` true, `flag_after` 40 (SH-1, SH-2) | — |
| `in_play` | `enabled` true, `top_n` 5, `rotation_days` 3, `rotation_lookback` 5, `symbols` (50 largest S&P 500 names) (UN-1, UN-2) | — |
| `tax` | `set_aside_rate_pct` 30 (TX-1) | 0–60 |
| `options` | `mode` `advisory` (`off`/`advisory`/`paper`/`live`), `account_level` 3, `from_alerts` false, `structures`, `allow_0dte` false, `day_dte` [5, 14], `swing_dte` [21, 45], `min_open_interest` 500, `min_volume` 100 (`min_volume_early` 20 before 10:00), `max_spread_pct` 5, `iv_rank_cheap` 30, `iv_rank_rich` 60, `max_premium_pct` 1, `max_contracts` 10, `avoid_earnings` true, `max_alerts_per_window` 6, `min_rr_at_t1` 1.5, `max_target_expected_move_x` 1.5 (OPTIONS_REQUIREMENTS §10) | `paper`/`live` need their gates (`options_paper`, `options_live`); structures within `account_level`; DTE ranges ordered. Option order placement is not built: every mode is advisory |

`config/alerts.yaml` (standalone bot) gains `options:` with `enabled` false, `fixed_risk_usd` 200, `min_interval_s` 10, `day_dte` [5, 14] (OP-14).
| `scanner` | `symbols` [], `min_score` 50, `top_n` 5, `in_play_top_n` 5, `min_samples` 30, `max_hold_days` 10, `disabled` [REVERSAL_CANDLE:long, RSI2_REVERSION:short, TREND_PULLBACK:short, BEAR_FLAG], `eval_file` reports/scanner_eval.json, `latest_file` reports/technical_scan.json, `alert_after_close` true (SCANNER_REQUIREMENTS TS-15) | `min_score` 0-100; counts >= 1; `disabled` names must be scanner techniques, optionally `:long`/`:short` |

## Environment variables (secrets)

Each container reads only its own env file (SECURITY.md SEC-18). Templates are the `.env.<name>.example` files in the repo root (`./run.sh setup` creates the real files); how to obtain every value is in [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md). Reddit values may be left empty (the news service runs without them).

| File | Container | Variables |
| --- | --- | --- |
| `.env.core` | trader-core | `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `DATABASE_URL` (role `core_rw`) |
| `.env.live` | trader-core, go-live only | `ALPACA_LIVE_KEY`, `ALPACA_LIVE_SECRET` |
| `.env.news` | news-sentiment | `FINNHUB_TOKEN`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `SEC_USER_AGENT`, `DATABASE_URL` (role `news_rw`) |
| `.env.notifier` | notifier | Telegram (default, ADR-014): `NOTIFIER_CHANNEL=telegram`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_ALLOWED_CHAT_IDS`. WhatsApp: `TWILIO_SID`, `TWILIO_TOKEN`, `WHATSAPP_FROM`, `WHATSAPP_TO`, `WHATSAPP_ALLOWED_SENDERS`, optional `WHATSAPP_CONTENT_SID` / `WHATSAPP_CONTENT_VARIABLE`, `DATABASE_URL` (role `notifier_rw`) |
| `.env.backup` | backup | `DATABASE_URL` (role `readonly`) |
| `.env.replay` | replay, dev | None required (offline) |
| `.env.console` | console | `DATABASE_URL` (role `console_rw`), `CORE_API_URL` (`http://trader-core:8081`), `OLLAMA_URL`, `CONSOLE_IDLE_MIN` (30), optional `CONSOLE_ASK_MODEL=0` to answer Ask with tables only |
| `.env.alerts` | alerts (optional) | `ALPACA_KEY`, `ALPACA_SECRET` (its own Alpaca account), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `.env.core` (optional extras) | trader-core | NF-1 e-mail fallback: `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `FALLBACK_EMAIL_TO`; `ALPACA_OPTIONS_FEED` (`indicative` on the free plan, `opra` when paid); `CORE_API_PORT` (8081) |
| `secrets/console_password.txt` | console | Owner Console login (at least 12 characters) |
| `secrets/pg_<role>_password.txt` | postgres | Admin and the role passwords (core, news, notifier, readonly, console), read at initialisation; the console role is also created or updated by `upgrade.sql` on every start |
| `docker/.env` | Compose | `PG_DATABASE`, `PG_ADMIN_USER`, `DB_ROLE_PREFIX`, `OLLAMA_MODEL` (template: `docker/.env.example`) |

The analyst-ratings feed (UI-33) uses `FINNHUB_TOKEN` for `stock/upgrade-downgrade` when the plan includes it (it switches itself off on 401/403) and needs no key for the Yahoo Finance and Google News RSS headlines. Add a company name for a new universe ticker in `src/trader/news/names.py` so headlines that use the name rather than the symbol match.

All of these are git-ignored. Live keys are loaded only when `mode.environment` is `live`.
