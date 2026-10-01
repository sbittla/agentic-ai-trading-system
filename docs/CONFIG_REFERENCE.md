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
| `storage` | Journal URL, bar path, backup path | Paths writable |

## Environment variables (secrets)

| Variable | Used by |
| --- | --- |
| `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET` | Broker adapter in paper mode |
| `ALPACA_LIVE_KEY`, `ALPACA_LIVE_SECRET` | Broker adapter in live mode only |
| `FINNHUB_TOKEN` | A1, A3 |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | A3 |
| `TWILIO_SID`, `TWILIO_TOKEN`, `WHATSAPP_FROM`, `WHATSAPP_TO` | Notifications and approvals |

Store them in a `.env` file excluded by `.gitignore`, or in Windows Credential Manager, and pass them to the container through `docker-compose.yml` `env_file`. Live keys are loaded only when `mode.environment` is `live`.
