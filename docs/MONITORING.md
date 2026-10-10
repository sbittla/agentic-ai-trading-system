# Monitoring and Alerting

**Version:** 1.0 (September 30, 2026) · Companion to [RUNBOOK.md](RUNBOOK.md)

What the system measures about itself, what the owner sees, and when it alerts. All metrics are computed from the journal, so they are identical in replay, paper and live.

---

## 1. Health score (7:00 AM pre-flight and every 30 s)

| Check | Green | Amber (alert, keep trading) | Red (block entries) |
| --- | --- | --- | --- |
| Alpaca Trading API | Responds < 1 s | 1–3 s | Errors or > 3 s, 3 times |
| Market data stream | Message < 10 s old | 10–60 s | > 60 s in a window |
| Qwen (`ollama`) | Health OK, p95 latency < 20 s | p95 20–45 s | Down or p95 > 45 s |
| `postgres` | Healthy, query < 50 ms | 50–500 ms | Down |
| `news-sentiment` heartbeat | < 2 min old | 2–5 min | > 5 min (news setups off) |
| `notifier` heartbeat and Twilio poll | < 1 min old | 1–2 min | > 2 min (approve mode blocked) |
| All containers | `healthy` in Docker | One restarted in the last hour | Restart loop (3 in 10 min) |
| Clock drift vs Alpaca | < 0.5 s | 0.5–2 s | > 2 s |
| Reconcile | Journal = Alpaca | — | Any mismatch |
| Disk free (data volume) | > 20 GB | 5–20 GB | < 5 GB |
| GPU memory free | > 2 GB | 0.5–2 GB | < 0.5 GB |
| Macro calendar coverage | Next CPI date listed | < 30 days of dates left | No future dates |
| Key age | < 75 days | 75–90 days | > 90 days (no block, daily alert) |

Pre-flight is **green** only if every check is green or amber; any red sends "NO TRADING TODAY: <reason>".

## 2. Trading metrics

| Metric | Definition | Alert when |
| --- | --- | --- |
| Slippage | Fill price − limit price on entries; stop fill − stop price on exits, in cents and % | Average > 0.03% over 10 trades |
| Fill rate | Filled entries ÷ submitted entries | < 60% over 20 entries (limits too tight or chasing) |
| Rejection mix | Count of `NT_*` reason codes per day | One code > 70% of rejections for 5 days (a filter may be miscalibrated) |
| LLM skip rate | A5 "skip" + A6 "disagree" ÷ setups | < 10% (rubber-stamping) or > 90% (blocking everything) over 20 setups |
| LLM validity | Valid JSON on first try ÷ calls | < 95% |
| Expectancy (R) | Average R per closed trade, rolling 20 and 30 | < 0 over 20 (RC-3 per setup) |
| Profit factor | Gross wins ÷ gross losses, rolling 30 | < 1.3 |
| Drawdown | Equity vs high-water mark | ≥ 3% (RC-1 size cut), ≥ 6% month (LL-6) |
| Rule breaches | Any order without a full decision record, any day trade open past its flatten time | Any (immediate alert, kill switch) |
| Time in market | Minutes with an open day trade outside windows | > 0 |

## 3. Dashboards (Grafana, `monitoring` profile)

Start or stop the browser view (it is optional; trading does not depend on it):

```powershell
docker compose -f docker/docker-compose.yml --profile monitoring up -d grafana grafana-proxy
docker compose -f docker/docker-compose.yml --profile monitoring stop grafana grafana-proxy
```

Open http://localhost:3000, user `admin`, password in `secrets/grafana_admin_password.txt`. The home page is **Today**; the others are under Dashboards → Trader.

How it is wired (SEC-9, SEC-17):

- `grafana` (`grafana/grafana:13.2.3`) is on the `internal` network only, so it has no internet and no host port. Plugin auto-install is off (`GF_PLUGINS_PREINSTALL_DISABLED`), so it runs the plugins bundled in the image.
- `grafana-proxy` (`nginx:1.30.5-alpine`) is the only door: it publishes `127.0.0.1:3000` and forwards to Grafana. An `internal` network cannot publish ports, and Docker Desktop ignores "no NAT" bridge options, hence the proxy.
- The `Journal` data source logs in as the `<prefix>readonly` role (SELECT only), with the password read from the `pg_readonly_password` Docker secret. No separate metrics store is needed because every metric is a journal query (ADR-012).
- Data source and dashboards are provisioned from `docker/grafana/` and cannot be edited in the UI. To change a panel, edit `tools/build_grafana_dashboards.py`, run `python tools/build_grafana_dashboards.py`, and Grafana picks it up within 30 s. A unit test fails if the JSON is stale or a query writes.

| Dashboard | Shows |
| --- | --- |
| **1. Today** (refresh 10 s) | System alive, kill switch, equity, day P&L vs the 1.5% limit, trades left, health checks, open positions, locks, today's decisions, game plan, open incidents |
| **2. Pipeline** | Funnel setups → proposals → LLM agreed → risk approved → owner yes → entry orders → fills; no-trade reasons; decisions over time; approvals with response time; LLM reviews |
| **3. Performance** | Win rate, expectancy (R), profit factor, P&L, drawdown; equity curve; expectancy by setup, ticker, window and weekday; daily metrics; closed trades |
| **4. LLM (Qwen)** | Calls, JSON validity, p50/p95 latency by agent, disagree rate, tokens, errors, per prompt version, recent calls with output |
| **5. Data quality** | Last bar age per ticker, stream gaps, non-green health history, bars and news per hour, service heartbeats, incidents |
| **6. Next-day ideas** | Latest after-close run: every ticker's readiness score, day change, close-in-range, volume vs 50-day, ATR distance to key levels, picks per window, news tone, Qwen's reason; hit rates and a track record graded on the planned session's daily bar |

Thresholds on the panels match the tables in §1 and §2. Panels stay empty until the system has traded or replayed a day. The 4:30 PM static HTML report is still produced.

## 4. Alert routing

| Severity | Examples | Channel |
| --- | --- | --- |
| Critical | Rule breach, reconcile mismatch, red health during a window, monthly limit hit | WhatsApp immediately, repeated every 5 min until acknowledged (`ACK`) |
| Warning | Amber health, slippage drift, fill-rate drop, daily limit hit | WhatsApp once |
| Info | Daily and weekly reports | WhatsApp summary + file |

Alerts are rate-limited to 10 per hour (except critical) to avoid alert fatigue; suppressed alerts are listed in the daily report.
