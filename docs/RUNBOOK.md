# Operations Runbook

**Version:** 1.0 (September 30, 2026) · Companion to [REQUIREMENTS.md](REQUIREMENTS.md)

How to set up, run, watch and recover the system. Times are US Eastern (6:30 AM ET = 3:30 AM Pacific).

---

## 1. One-time setup

1. Install Docker Desktop (WSL2 backend) and the NVIDIA driver with WSL GPU support; confirm `docker run --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` works.
2. Create an Alpaca account; generate **paper** API keys. Keep live keys ungenerated until stage 5 of the test plan.
3. Create a Finnhub key, a Reddit "script" app, and a Twilio WhatsApp sender.
4. Copy `config/config.example.yaml` to `config/config.yaml`; review every value in `risk` and `wash_sale`.
5. Create `.env` with the secrets listed in [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md#environment-variables-secrets); confirm `.env` is in `.gitignore`.
6. `docker compose up -d ollama` then `docker compose exec ollama ollama pull qwen3:14b` (pin the exact tag in config).
7. `docker compose up -d trader`; check the 7:00 AM pre-flight message arrives on WhatsApp the next trading day.
8. Windows: set active hours 6 AM–5 PM ET, disable sleep on AC power, enable automatic login recovery after power loss, and set the PC to sync time automatically.

## 2. Daily routine for the owner

| Time (ET) | What to do |
| --- | --- |
| 7:00 | Read the pre-flight message: green means the system will trade; red names the problem |
| 9:00 | Read the game plan: tickers in play, setups, off-limits list (also avoid those tickers in other accounts) |
| 9:35–10:30 | In approve mode, answer approvals within 90 seconds; no answer means no trade |
| 10:30 | Read the morning summary |
| 3:00–4:00 | Approvals as above |
| 4:30 | Read the daily review; note any rule breach or incident |
| Friday | Read the weekly review; decide on any setup returned to paper |

## 3. Commands

| Action | WhatsApp | Local |
| --- | --- | --- |
| Status | `STATUS` | `docker compose exec trader trader status` |
| Kill switch (cancel entries, block new ones, keep stops) | `KILL` | `docker compose exec trader trader kill` |
| Resume after kill | `RESUME` (only after the cause is understood) | `trader resume` |
| Flatten everything now | `FLATTEN` (asks for confirmation) | `trader flatten --confirm` |
| Approve / reject a trade | `YES <code>` / `NO <code>` | — |
| Pause one style | `PAUSE DAY` / `PAUSE SWING` | `trader pause day` |

## 4. Incident playbooks

### Data stream stale
1. System blocks entries automatically and alerts.
2. Check internet; check Alpaca status page.
3. If not back in 5 minutes during a window, send `KILL`; open positions remain protected by broker-side stops.

### Qwen container down or slow
1. Entries are blocked automatically (no trade without review).
2. `docker compose restart ollama`; check GPU with `nvidia-smi`.
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

### PC restarted or lost power
1. Containers restart automatically; the trader rebuilds state and reconciles before trading.
2. Check the WhatsApp alert for the reconcile result.

### Margin call or account restriction (should not happen at 1x leverage)
1. Send `KILL`.
2. Read the notice in the Alpaca dashboard; resolve within two business days.

## 5. Maintenance

| Task | When |
| --- | --- |
| Back up `data/` (journal and bars) to the backup path | Nightly, automatic |
| Restore test from backup | Monthly |
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
