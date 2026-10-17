# Build Status

**As of:** October 16, 2026 · Companion to [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and [TEST_PLAN.md](TEST_PLAN.md)

Milestones M0–M7 are built and tested offline. M8 (paper trading) and M9 (small live) need the owner's accounts, keys and the local model, and real trading days.

## What is built

| Milestone | Status | Where |
| --- | --- | --- |
| M0 Repo, CI, config, Docker | Done | `pyproject.toml`, `src/trader/config.py` (every CF-4 rule, `trader validate-config`), `.github/workflows/ci.yml`, `.devcontainer/`, `requirements/*.lock` (hashed, Linux), `.dockerignore` |
| M1 Journal, models, bus | Done | `journal/` (all DATA_SPEC §3 tables, Postgres + SQLite), `models/`, `bus.py` (journal-before-deliver bus; Postgres outbox with LISTEN/NOTIFY and the role/channel guard) |
| M2 Market data, clock, calendar | Done | `agents/a02_market_data.py`, `indicators.py`, `calendar.py` (NYSE sessions, half days, macro days), `data/alpaca_data.py`, `data/store.py` (Parquet store, day recorder) |
| M3 Safety core | Done | `agents/a07_risk.py`, `a12_compliance.py`, `a08_execution.py`, `a09_positions.py`, `brokers/sim.py` |
| M4 Replay engine | Done | `backtest/engine.py` (same agents, ReplayClock, SimBroker, CachedLLM; run folders; decision diff), `backtest/synthetic.py` |
| M5 Technicals + setups | Done | `agents/a04_technicals.py`: all 8 setups, MF-1..MF-5, levels, score |
| M6 Qwen agents + news | Done (offline) | `llm/ollama_client.py`, `agents/a03_news.py`, `a05_strategy.py`, `a06_review.py`, `prompts/*/v1.md`, `backtest/evaluation.py`, `news/sources.py` |
| M7 Supervisor, notifications, reports | Done (offline) | `agents/a10_supervisor.py`, `a11_reporter.py`, `notify/`, `services/*_main.py`, `cli.py` |
| REQUIREMENTS 1.3 (day-trader review) | Done (offline) | Latency and pre-arming (`agents/speed.py`), shadow trades, regime, slippage feedback, settled cash/account type, news shock, core alerts (`agents/alerts_core.py`), approval charts and buttons (`notify/chart.py`), co-pilot mode, notifier-down fallback (`notify/fallback.py`), in-play scan, tax set-aside, other-broker import (`agents/external_trades.py`), IEX-vs-SIP gate, strict paper fills, restore test and drill (`agents/ops.py`) |
| Options layer (OPTIONS_REQUIREMENTS.md) | Done, advisory | `agents/a04_bearish.py` (BR-1..4), `options/` (A13 data, A14 strategist, A15 risk, A16 monitor, desk), OP-14 line in the alerts bot, Grafana **7. Options** dashboard. Option order placement (§9 paper/live) is not built |
| Owner Console (UI_REQUIREMENTS.md) | Done | `console/` (Starlette app; `render.py` components, `pages.py` page layouts, Ask tools, SVG charts), `services/core_api.py` (trader-core internal read-only API, incl. options board and preview), `console` container on 127.0.0.1:8080 |
| Analyst ratings, recap, backfill (UI-33..36) | Done (offline) | `news/ratings.py` (Finnhub upgrade-downgrade + Yahoo/Google News RSS headline rules, `analyst_actions`), `agents/recap.py` (`trader recap`, `RECAP N`, `market_recap`), `NewsAgent.backfill` (`trader backfill-news`), `./run.sh backfill [days]`, console Market recap / Analyst ratings / News pages, options board and preview |
| Trading-decision checks and Technical scan page (TS-17..TS-19, UI-37) | Done | `scanner/checks.py` (hard/soft checks, RiskManager sizing, verdicts, bearish ideas options-only), `SCAN` command, `console/technical.py` (page + Today card); coverage map in [TRADING_DECISIONS.md](TRADING_DECISIONS.md) |
| Technical scanner (SCANNER_REQUIREMENTS.md) | Done, advisory | `scanner/` (TA-Lib-identical indicators and candlestick patterns, 9 swing techniques long and short, day watchlist, score with measured edge, walk-forward grading), `trader scan` / `scan-eval`, `./run.sh scan`, trader-core 16:20 ET Telegram scan |
| Gap review against professional practice (GAP_REVIEW.md, REQUIREMENTS 1.4) | Done | `portfolio.py` + `agents/a07_risk.py` (PF-1..PF-4: open risk, measured correlation, beta exposure, swing gap stress), `agents/a08_execution.py` `order_sanity` (OC-1..OC-4), `metrics.py` + `a11_reporter.py` (PA-1..PA-3: Sharpe, Sortino, Calmar, SQN, SPY benchmark), `metrics.py` + `backtest/walkforward.py` (SV-1..SV-3: bootstrap interval, Monte Carlo drawdown, statistical pass bar) |
| News catalysts (NEWS_CATALYST_REQUIREMENTS.md, A17) | Done, advisory; uncalibrated until backfilled and graded | `catalyst/` (stories, rules + Qwen typing, strength, social, event-study outlook, confluence, position view, walk-forward grading and gates), A3 enrichment and fundamentals (`catalyst/collect.py`), trader-core jobs and `CATALYST` command, `trader catalyst` / `catalyst-eval` / `backfill-catalysts`, console Catalysts page |
| Macro, forecasts, technicals (MACRO_FORECAST_REQUIREMENTS.md, A18) | Done, advisory | `macro/` (FRED CSV, Fed RSS, Finnhub general news, themes, regime, release surprises, grading), `forecast/` (volatility estimators graded by QLIKE, gaps, macro-day multipliers feeding the catalyst outlook, sector rotation), scanner TS-20..TS-23, `trader macro` / `macro-collect` / `forecast`, `MACRO` command, console Macro page |
| Momentum (MOMENTUM_REQUIREMENTS.md, A19) | Done, advisory; 4 of 7 setups and the rotation model pass their gates | `momentum/` (factors, RS rating, trend template, stage, regime with crash guard, 7 setups, one exit state machine, walk-forward grading with ablations, alert-stream portfolio, rotation model chosen in-sample), `momentum_signals` tracker with close and 5-minute intraday alerts, `trader momentum` / `momentum-eval` / `momentum-history`, `MOMENTUM` command, console Momentum page |
| M8 Backtest + paper | Ready to start | Needs keys, history download, and 20+ paper days |
| M9 Small live | Not started | After the M8 gates |

## Verification (all run on this machine)

| Check | Result |
| --- | --- |
| `pytest` | 451 passed (October 11), including the Postgres integration tests against a real Postgres 16 initialised with `init.sql` + `upgrade.sql` (skipped without `TEST_PG_*` env vars); 1 real-Ollama test skipped (model not downloaded in CI) |
| `python tools/check_traceability.py docs/TRACEABILITY.md tests` | 268 of 268 automated requirement IDs covered (16 technical-scanner IDs added) |
| Technical scanner tests (October 15, local venv) | 34 new tests pass (`tests/unit/test_scanner.py`, `tests/replay/test_technical_scan_job.py`); full suite 460 passed with no new failures (the console tests need `starlette`, absent from that venv) |
| Scanner vs TA-Lib (TS-2, TS-3) | 18 indicator outputs and 8 candlestick patterns identical to TA-Lib on the stored history; walk-forward results in SCANNER_REQUIREMENTS.md §6 |
| `ruff check src tests` (incl. bandit `S` rules) | Clean |
| `mypy` (strict) | Clean, 105 files |
| Console role on real Postgres | Every page renders as `console_rw`; it may publish only `inbound_cmd` (source forced to `console`), insert notes/chats/LLM logs, and is refused on every other write |
| Restore test (OPS-1) on real Postgres | `pg_dump` as the read-only role, restore into a scratch cluster, counts compared: PASS |
| Replay determinism (RP-2) | Same days twice give byte-identical decision logs |
| 60-day synthetic backtest (`trader backtest --synthetic 60`) | Runs end to end in about 20 s |

Synthetic results only prove the plumbing; they say nothing about whether the strategy has an edge. Only recorded IEX days, walk-forward on real history and paper trading can answer that (TEST_PLAN stages 2–4).

## Corrections made to the design during the build

| Finding | Resolution |
| --- | --- |
| STRATEGY_SPEC §7 worked example: R $0.88 > ATR5 $0.85 fails checklist item 7 | The code enforces R ≤ 1.0 × ATR5; the spec now notes the correction |
| "HOD" as resistance would block almost every breakout (the breakout bar's own high sits just above the entry) | HOD counts as resistance only for setups that trigger below it (VWAP_RECLAIM, FIRST_PULLBACK, VWAP_TREND); STRATEGY_SPEC §1 updated |
| RUNBOOK step 6 pulled the model from the `ollama` container, which has no internet access (SEC-9) | New one-off `model-pull` service (profile `setup`) shares the model volume; RUNBOOK updated |
| `trader-core` runs with a read-only filesystem but writes reports and recordings | Mounted `../reports` and `../recordings` in compose |
| `CREATE SCHEMA IF NOT EXISTS` fails for the least-privilege `core_rw` role | The migration checks for the schema first (found against a real Postgres) |
| After a restart, reconcile flagged the system's own bracket legs as foreign orders | Reconcile also recognises orders from the journal and the restored legs |
| QW-1: this machine has an 8 GB GPU (RTX 4060 Laptop) | Local `config/config.yaml` uses `qwen3:8b` |
| The restore test (OPS-1) found the nightly `pg_dump` failing: the read-only backup role could not read sequences, and `trader backup` did not notice | Sequence grants for `readonly` (in `trader migrate` and `upgrade.sql`); `trader backup` checks the exit code, prints BACKUP FAILED, retries and leaves the health file stale so Grafana and the supervisor flag it |
| Rule-parsed rating headlines attributed "Bank of America raises Amazon target" to BAC | A headline counts only when its subject company (symbol or name in `news/names.py`) is the ticker; firm names are matched case-sensitively |
| Console sections were blank on a fresh install (no ratings, news or recap yet) | Every section has an empty state that says why and what to do (UI-35); `./run.sh backfill` loads the past week or month |
| A console needs live values that the journal does not hold (bars, quotes, checklist) | trader-core serves a GET-only JSON API on the internal network; the console holds no broker keys |

## What the owner needs to provide (M8)

Step-by-step instructions for every item: [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md).

1. **Alpaca paper keys** in `.env.core` (`ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`).
2. **Finnhub token** and **SEC User-Agent with your email** in `.env.news` (Reddit script app optional); set `data.sec_user_agent` in `config/config.yaml`.
3. **Telegram bot** token and your chat id in `.env.notifier` (default channel, ADR-014), or Twilio WhatsApp.
4. Postgres passwords in `secrets/pg_<role>_password.txt` and each `.env.*` file; database/role names in `docker/.env`.
5. The model: `docker compose -f docker/docker-compose.yml --profile setup run --rm model-pull`.
6. Then: `./run.sh connections`, `./run.sh` (starts everything), `./run.sh backfill 30` (last month's news, ratings, recap and ideas), `./run.sh backtest`, and 20+ days of paper trading in approve mode (RUNBOOK §1, TEST_PLAN stage 4).

## Quick commands

All from WSL at the repo root; everything runs in containers.

```bash
./run.sh test                  # ruff, mypy, pytest, traceability (dev container)
./run.sh replay 10             # full system on synthetic days
./run.sh backtest 500          # playbook on real minute bars (BT-6)
./run.sh restore-test          # OPS-1: newest backup into a scratch database
./run.sh backfill 30           # past month: news, analyst ratings, recap + watchlist, next-day ideas
docker compose -f docker/docker-compose.yml exec trader-core trader eval-prompts --llm ollama
```
