# Operations Runbook

**Version:** 1.0 (September 30, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md)

How to set up, run, watch and recover the system. Times are US Eastern (6:30 AM ET = 3:30 AM Pacific).

---

## 1. One-time setup

**One script does all of this from WSL** (repo root), and nothing runs on the host: every service and every helper command runs in a container (helpers in the `toolbox` image, checks in the `dev` image). It refuses to start if git tracks any secret file.

1. Install Docker Desktop (WSL2 backend) and the NVIDIA driver with WSL GPU support; confirm `docker run --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` works ([ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) §0).
2. Create the accounts and keys: Alpaca **paper** keys (keep live keys ungenerated until stage 5 of the test plan), Finnhub, a Telegram bot (Reddit optional). Step by step: [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md).
3. `./run.sh setup` creates every missing local file from the `.example` templates (never overwrites): `config/config.yaml`, the env files (`.env.core`, `.env.news`, `.env.notifier`, `.env.backup`, `.env.replay`, `.env.console`), the password files in `secrets/` (Postgres roles including `pg_console_password.txt`, `console_password.txt` for the Owner Console, `grafana_admin_password.txt`) and `docker/.env`; then it picks the Qwen model that fits your GPU ([HARDWARE.md](HARDWARE.md)). Review every value in `risk` and `wash_sale` in `config/config.yaml`. Confirm `git status` shows none of these files.
4. Fill in the keys it lists, then `./run.sh connections` (read-only checks of Alpaca, Finnhub, Telegram, the database roles and Qwen; [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) §6, §8).
5. `./run.sh` starts everything in order: Postgres and Ollama, the database role/guard upgrade (`upgrade.sql`, idempotent), the model download if missing, `trader migrate` (tables and grants, idempotent), then trader-core, news-sentiment, notifier, backup, the **Owner Console** (http://localhost:8080) and **Grafana** (http://localhost:3000). It waits until every container is healthy and checks the model runs 100% on the GPU. `./run.sh status` shows them all; `./run.sh --no-console` or `--no-monitoring` skips those two.
6. Check the 7:00 AM pre-flight message arrives on Telegram the next trading day, and send `STATUS` to confirm inbound polling works.
7. Windows: set active hours 6 AM–5 PM ET, disable sleep on AC power, enable automatic login recovery after power loss, and set the PC to sync time automatically.

8. **Load the past week or month** so the console and the first plans have data: `./run.sh backfill 30` (or `7`). It rebuilds the images for the current code and runs `trader migrate` first, then pulls the last 30 days of Finnhub news, SEC filings and analyst rating actions (Finnhub plus Yahoo Finance and Google News RSS headlines) and scores them with FinBERT, builds the recap of the last ~21 sessions and sends the next-session watchlist to Telegram, then runs `trader plan` for the next-day ideas when trader-core is running. Services still running older code are restarted on the new images afterwards (trader-core only outside 9:25 AM–4:05 PM ET). It is safe to re-run: items already in the journal are skipped. It needs outbound access to finnhub.io, efts.sec.gov, feeds.finance.yahoo.com, news.google.com and the Alpaca data API.

Optional: `./run.sh alerts` starts the standalone price-alert bot when it has its own Alpaca account ([ALERTS.md](ALERTS.md)); otherwise trader-core sends the alerts itself (AL-1).

## 2. Daily routine for the owner

| Time (ET) | What to do |
| --- | --- |
| 7:00 | Read the pre-flight message: green means the system will trade; red names the problem |
| 9:00 | Read the game plan: tickers in play, setups, off-limits list (also avoid those tickers in other accounts) |
| 9:35–10:30 | In approve mode, answer approvals within 90 seconds (Telegram buttons, or the console's Approvals page with the code from your phone); no answer means no trade. In co-pilot mode the plan arrives as a ticket: place the order yourself, then press **I filled** (console) or send `FILLED NVDA 40 @ 190.05` |
| 10:30 | Read the morning summary |
| 3:00–4:00 | Approvals as above |
| 4:15 | Read the **next-day ideas** alert: up to 4 picks per window with trigger, stop and T1, built from today's close (code screen + Qwen review), plus how the previous ideas played out. Grafana **6. Next-day ideas** shows every ticker's screen. Ideas are refreshed at 9:00 and 2:45 with gap and pre-market data; they mark trades as planned (full size), they never place orders |
| Evening / weekend | `RECAP 5` (week) or `RECAP 21` (month) on Telegram, or the buttons on the console's Market recap page: leaders and laggards against QQQ, analyst actions, events, and a watchlist with levels for the next session. Levels to watch, not orders |
| 4:15 | Options ideas (advisory, OPTIONS_REQUIREMENTS.md) arrive with the next-day ideas; follow-ups for open ideas come during the windows |
| 4:30 | Read the daily review (now with latency, slippage, strict paper fills and the options section); note any rule breach or incident |
| Any time | Owner Console http://localhost:8080: cockpit, stock lookup with the live checklist ("why isn't it buying NVDA?"), Ask, trade stories, journal notes and tags |
| Friday | Read the weekly review; decide on any setup returned to paper |

## 3. Commands

Commands are accepted only from allow-listed senders (`TELEGRAM_ALLOWED_CHAT_IDS` / `TELEGRAM_CHAT_ID` with Telegram, the default channel per ADR-014; `WHATSAPP_ALLOWED_SENDERS` with WhatsApp); approvals need the one-time code, and destructive commands need a second confirmation code ([SECURITY.md](SECURITY.md) SEC-1..3).

The Owner Console sends the same commands through the same checks (Controls page; source `console` in the audit list).

| Action | Telegram / WhatsApp | Local |
| --- | --- | --- |
| Status | `STATUS` | `./run.sh status` |
| Recap of the last N sessions and next-session watchlist | `RECAP 5` / `RECAP 21` | `./run.sh backfill [days]` (news, ratings, recap and plan) or `docker compose -f docker/docker-compose.yml exec trader-core trader recap --days 5 [--no-send]` |
| Rebuild next-day ideas (outside market hours) | `PLAN` | `docker compose -f docker/docker-compose.yml exec trader-core trader plan` |
| Kill switch (cancel entries, block new ones, keep stops) | `KILL` | `docker compose -f docker/docker-compose.yml exec trader-core trader kill` (instant) |
| Resume after kill | `RESUME`, then the confirmation code (only after the cause is understood) | `docker compose -f docker/docker-compose.yml exec trader-core trader resume` |
| Flatten everything now | `FLATTEN`, then the confirmation code | `docker compose -f docker/docker-compose.yml exec trader-core trader flatten --confirm` |
| Approve / reject a trade | `YES <code>` / `NO <code>` | — |
| Acknowledge a critical alert | `ACK` | — |
| Pause one style | `PAUSE DAY` / `PAUSE SWING` | `docker compose -f docker/docker-compose.yml exec trader-core trader pause day` |
| Signal alerts on/off | `ALERTS ON` / `ALERTS OFF` | Console → Alerts |
| Co-pilot fills | `FILLED <T> <qty> @ <price>` / `SOLD <T> <qty> @ <price>` | Console → Co-pilot |
| Options | `OPT <T> [bull\|bear] [day\|swing]`, `CHAIN <T>`, `IV <T>`, `OPTS`, `OPTIONS ON\|OFF` (read-only, no orders) | Console → Stock → Options |

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
| Restore test (OPS-1): `./run.sh restore-test` restores the newest dump into a throwaway Postgres container (no network), compares row counts with production and records the result | Monthly; the monthly report lists it when overdue |
| Emergency drill (OPS-2): `./run.sh drill` walks through KILL, FLATTEN and a network pull on paper and records it | Monthly |
| Other-broker trades for wash sales (WS-9): `./run.sh import-trades FILE --broker robinhood\|webull\|fidelity\|schwab\|generic` | After any trade in another account |
| Check the backup really ran: Grafana / `./run.sh logs backup` shows no `BACKUP FAILED` (the restore test found a silent failure once; `trader backup` now fails loudly) | Weekly |
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
