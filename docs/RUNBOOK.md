# Operations Runbook

**Version:** 1.0 (September 30, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md)

How to set up, run, watch and recover the system. Times are US Eastern (6:30 AM ET = 3:30 AM Pacific).

---

## 1. One-time setup

1. Install Docker Desktop (WSL2 backend) and the NVIDIA driver with WSL GPU support; confirm `docker run --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` works ([ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) §0).
2. Create an Alpaca account; generate **paper** API keys. Keep live keys ungenerated until stage 5 of the test plan.
3. Create a Finnhub key, a Reddit "script" app (optional), and a Twilio WhatsApp sandbox sender. Step-by-step instructions for every account, key and password are in [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md).
4. Copy `config/config.example.yaml` to `config/config.yaml`; review every value in `risk` and `wash_sale`.
5. Run `trader init-env --prefix <your prefix>` to create the env files (`.env.core`, `.env.news`, `.env.notifier`, `.env.backup`, `.env.replay`) from their `.example` templates and the five password files in `secrets/` (`pg_admin_password.txt`, `pg_core_password.txt`, `pg_news_password.txt`, `pg_notifier_password.txt`, `pg_readonly_password.txt`); optionally set the database and role names in `docker/.env`. Run `trader check-connections` to verify the keys ([ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) §6, §8) ([CONFIG_REFERENCE.md](CONFIG_REFERENCE.md#environment-variables-secrets)). Confirm `git status` shows none of them.
6. `docker compose -f docker/docker-compose.yml up -d postgres ollama`, then `docker compose -f docker/docker-compose.yml --profile setup run --rm model-pull` to download the model set in `OLLAMA_MODEL` (default `qwen3:8b`; set `OLLAMA_MODEL=qwen3:14b` with a GPU of 12 GB or more, and pin the same tag in `llm.model`). The runtime `ollama` service has no internet access (SEC-9), so it cannot pull models itself. If Docker Desktop is unstable during large downloads, use `tools/fetch_ollama_model.py` and `tools/import_ollama_model.ps1` instead ([ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) §0).
7. `docker compose -f docker/docker-compose.yml run --rm trader-core trader migrate` to create the journal tables.
8. `docker compose -f docker/docker-compose.yml up -d` to start all six services; `docker compose -f docker/docker-compose.yml ps` must show every service `healthy`.
9. Check the 7:00 AM pre-flight message arrives on WhatsApp the next trading day, and reply `STATUS` to confirm inbound polling works. With the Twilio sandbox, WhatsApp delivers free-form messages only within 24 hours of your last message, so reply `STATUS` once a day ([ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) §5).
10. Windows: set active hours 6 AM–5 PM ET, disable sleep on AC power, enable automatic login recovery after power loss, and set the PC to sync time automatically.

## 2. Daily routine for the owner

| Time (ET) | What to do |
| --- | --- |
| 7:00 | Read the pre-flight message: green means the system will trade; red names the problem |
| 9:00 | Read the game plan: tickers in play, setups, off-limits list (also avoid those tickers in other accounts) |
| 9:35–10:30 | In approve mode, answer approvals within 90 seconds; no answer means no trade |
| 10:30 | Read the morning summary |
| 3:00–4:00 | Approvals as above |
| 4:15 | Read the **next-day ideas** alert: up to 4 picks per window with trigger, stop and T1, built from today's close (code screen + Qwen review), plus how the previous ideas played out. Grafana **6. Next-day ideas** shows every ticker's screen. Ideas are refreshed at 9:00 and 2:45 with gap and pre-market data; they mark trades as planned (full size), they never place orders |
| 4:30 | Read the daily review; note any rule breach or incident |
| Friday | Read the weekly review; decide on any setup returned to paper |

## 3. Commands

Commands are accepted only from allow-listed senders (`TELEGRAM_ALLOWED_CHAT_IDS` / `TELEGRAM_CHAT_ID` with Telegram, the default channel per ADR-014; `WHATSAPP_ALLOWED_SENDERS` with WhatsApp); approvals need the one-time code, and destructive commands need a second confirmation code ([SECURITY.md](SECURITY.md) SEC-1..3).

| Action | Telegram / WhatsApp | Local |
| --- | --- | --- |
| Status | `STATUS` | `docker compose -f docker/docker-compose.yml exec trader-core trader status` |
| Rebuild next-day ideas (outside market hours) | `PLAN` | `docker compose -f docker/docker-compose.yml exec trader-core trader plan` |
| Kill switch (cancel entries, block new ones, keep stops) | `KILL` | `docker compose -f docker/docker-compose.yml exec trader-core trader kill` (instant) |
| Resume after kill | `RESUME`, then the confirmation code (only after the cause is understood) | `trader resume` |
| Flatten everything now | `FLATTEN`, then the confirmation code | `trader flatten --confirm` |
| Approve / reject a trade | `YES <code>` / `NO <code>` | — |
| Acknowledge a critical alert | `ACK` | — |
| Pause one style | `PAUSE DAY` / `PAUSE SWING` | `trader pause day` |

## 4. Incident playbooks

Setup and machine problems (Docker Desktop crashes, model loading, GPU, Telegram/Twilio, database names and passwords) are in [TROUBLESHOOTING.md](TROUBLESHOOTING.md), with a step-by-step recovery sequence.

### Data stream stale
1. System blocks entries automatically and alerts.
2. Check internet; check Alpaca status page.
3. If not back in 5 minutes during a window, send `KILL`; open positions remain protected by broker-side stops.

### Qwen container down or slow
1. Entries are blocked automatically (no trade without review).
2. `docker compose restart ollama`; check GPU with `nvidia-smi` and `trader check-connections --service llm` (run inside `trader-core`): `llm gpu` must be 100% on GPU.
3. If GPU memory is exhausted, switch `llm.model` to the 8B model for the next day.

### Alpaca API errors or outage
1. Entries blocked after 3 failures; brackets at Alpaca still protect positions.
2. If positions must be closed and the API is down, use the Alpaca web dashboard.
3. After recovery, the reconcile must pass before entries resume; check the reconcile report.

### Position and journal disagree (reconcile mismatch)
1. Entries blocked.
2. Alpaca is the source of truth for positions and orders; the journal is corrected from Alpaca and the incident logged.
3. Cancel any order the system does not recognise.

### Daily, weekly or monthly limit hit
1. No action needed; the system stops by itself.
2. Do not raise the limit to keep trading. Review the trades in the daily report.
3. Monthly limit: live trading stays off until you review and set `mode.environment: live` again.

### A container is unhealthy
1. `docker compose -f docker/docker-compose.yml ps` shows which one; `docker compose -f docker/docker-compose.yml logs --tail 200 <service>` shows why.
2. `news-sentiment` down: trading continues without news-dependent setups (GAP_AND_GO off); restart it.
3. `notifier` down: approvals cannot arrive, so approve-mode entries stop; use local commands; restart it.
4. `postgres` down: entries stop (no journal, no trade); exits continue; restart Postgres, then check the reconcile alert.
5. `trader-core` restarting repeatedly: send `KILL` from the notifier or close positions in the Alpaca dashboard; check logs before restarting.

### PC restarted or lost power
1. Containers restart automatically; `trader-core` rebuilds state from Postgres and reconciles with Alpaca before trading.
2. Check the WhatsApp alert for the reconcile result.

### Margin call or account restriction (should not happen at 1x leverage)
1. Send `KILL`.
2. Read the notice in the Alpaca dashboard; resolve within two business days.

## 5. Maintenance

| Task | When |
| --- | --- |
| Postgres dump and bar files copied to `BACKUP_PATH` by the `backup` container | Nightly, automatic |
| Restore test: load last night's dump into a scratch Postgres container and run `trader validate-journal` | Monthly |
| Update pinned image versions and dependency locks; rebuild; replay library diff | Monthly |
| Rotate Alpaca and Finnhub keys | Every 90 days |
| Update macro calendar (CPI, jobs, FOMC dates) | December for the next year; check monthly |
| Pin new Ollama / Qwen versions | Only after the prompt evaluation set passes (AGENT_PROMPTS.md section 8) |
| Review the ticker list against market caps | Sunday universe report |
| Export tax lots and wash-sale report | Monthly; full year in January |

## 6. Go-live checklist

- [ ] All stages in [TEST_PLAN.md](TEST_PLAN.md) passed and recorded
- [ ] Live account funded with money you can afford to lose; margin account if day trading with intraday buying power
- [ ] Live keys created, stored in `.env`, never committed
- [ ] `mode.environment: live`, `day_trades: approve`, `swing_trades: approve`
- [ ] `risk_per_trade_pct: 0.25` for the first 20 live trades
- [ ] Kill switch tested on the live account with no position open
- [ ] Wash-sale policy confirmed with a tax advisor
