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
| `risk` | All loss limits and behaviour limits from REQUIREMENTS.md section 6 | `risk_per_trade_pct` ≤ 1.0; `daily_loss_limit_pct` ≤ 3.0; `max_leverage` ≤ 1.0 |
| `wash_sale` | `policy`, `lock_days`, balanced threshold, year-end date | `lock_days` ≥ 31 for strict |
| `data` | Feed, staleness, polling, subreddits, SEC User-Agent | SEC User-Agent contains an email |
| `llm` | Ollama URL, pinned model tag, temperature, timeout, prompt versions | `temperature` = 0; prompt files exist |
| `notifications` | WhatsApp on/off, approval timeout | Timeout ≤ 120 s |
| `storage` | Env var holding the Postgres URL, bar path, backup time | Env var present; Postgres reachable |
| `services` | Heartbeat interval, staleness limits for news and notifier, notifier polling intervals | Approval polling ≤ 5 s |

### Planned fields (REQUIREMENTS 1.3, not built yet)

| Section | New fields | Validation at start |
| --- | --- | --- |
| `mode` | `day_trades` / `swing_trades` also accept `copilot` (CP-1); `account_type` (`cash`/`margin`, AC-1) | `account_type` must match Alpaca's, or the day is blocked (AC-2) |
| `latency` | `budget_s` (10), `prearm_atr` (0.3), `prearm_tolerance_atr` (0.1) (LAT-1, LAT-2) | `budget_s` ≤ 30 |
| `windows` | `macro_release_block` (`09:57`–`10:05`), `speech_block_min` (3 before / 5 after) (TW-6) | inside the morning window |
| `regime` | `high_vol_atr_ratio` (1.5), `trend_day_vwap_pct` (80) (RG-1..RG-3) | ratio ≥ 1.0 |
| `data` | `quote_max_age_s` (2, DQ-8), `news_poll_open_s` (60, NW-1), `sip_divergence_pct` (0.05, DQ-7) | `news_poll_open_s` ≥ 30 (Finnhub limit) |
| `alerts` | `schedule` (`windows`/`all_day`), `per_symbol_cap` (3), `per_window_cap` (12), `digest_min` (10) (AL-4..AL-6) | caps ≥ 1 |
| `execution` | `slippage_switch_r` (0.1, EXQ-1); stops always stop-market (EX-5, not configurable) | `slippage_switch_r` > 0 |
| `tax` | `set_aside_rate_pct` (TX-1) | 0–60 |

## Environment variables (secrets)

Each container reads only its own env file (SECURITY.md SEC-18). Templates are the `.env.<name>.example` files in the repo root (`trader init-env` creates the real files); how to obtain every value is in [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md). Reddit values may be left empty (the news service runs without them).

| File | Container | Variables |
| --- | --- | --- |
| `.env.core` | trader-core | `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `DATABASE_URL` (role `core_rw`) |
| `.env.live` | trader-core, go-live only | `ALPACA_LIVE_KEY`, `ALPACA_LIVE_SECRET` |
| `.env.news` | news-sentiment | `FINNHUB_TOKEN`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `SEC_USER_AGENT`, `DATABASE_URL` (role `news_rw`) |
| `.env.notifier` | notifier | Telegram (default, ADR-014): `NOTIFIER_CHANNEL=telegram`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `TELEGRAM_ALLOWED_CHAT_IDS`. WhatsApp: `TWILIO_SID`, `TWILIO_TOKEN`, `WHATSAPP_FROM`, `WHATSAPP_TO`, `WHATSAPP_ALLOWED_SENDERS`, optional `WHATSAPP_CONTENT_SID` / `WHATSAPP_CONTENT_VARIABLE`, `DATABASE_URL` (role `notifier_rw`) |
| `.env.backup` | backup | `DATABASE_URL` (role `readonly`) |
| `.env.replay` | replay, dev | None required (offline) |
| `secrets/pg_<role>_password.txt` | postgres | Admin and the four role passwords, read at initialisation only |
| `docker/.env` | Compose | `PG_DATABASE`, `PG_ADMIN_USER`, `DB_ROLE_PREFIX`, `OLLAMA_MODEL` (template: `docker/.env.example`) |

All of these are git-ignored. Live keys are loaded only when `mode.environment` is `live`.
