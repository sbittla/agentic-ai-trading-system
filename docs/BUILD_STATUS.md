# Build Status

**As of:** October 1, 2026 · Companion to [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) and [TEST_PLAN.md](TEST_PLAN.md)

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
| M8 Backtest + paper | Ready to start | Needs keys, history download, and 20+ paper days |
| M9 Small live | Not started | After the M8 gates |

## Verification (all run on this machine)

| Check | Result |
| --- | --- |
| `pytest` | 263 passed, plus 9 Postgres integration tests passed against a real Postgres 16 initialised with `init.sql` (skipped without `TEST_PG_*` env vars); 1 real-Ollama test skipped (model not yet downloaded) |
| `python tools/check_traceability.py docs/TRACEABILITY.md tests` | 137 of 137 automated requirement IDs covered |
| `ruff check src tests` (incl. bandit `S` rules) | Clean |
| `mypy` (strict) | Clean, 62 files |
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

## What the owner needs to provide (M8)

Step-by-step instructions for every item: [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md).

1. **Alpaca paper keys** in `.env.core` (`ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`).
2. **Finnhub token** and **SEC User-Agent with your email** in `.env.news` (Reddit script app optional); set `data.sec_user_agent` in `config/config.yaml`.
3. **Telegram bot** token and your chat id in `.env.notifier` (default channel, ADR-014), or Twilio WhatsApp.
4. Postgres passwords in `secrets/pg_<role>_password.txt` and each `.env.*` file; database/role names in `docker/.env`.
5. The model: `docker compose -f docker/docker-compose.yml --profile setup run --rm model-pull`.
6. Then: `trader fetch-history`, `trader eval-prompts --llm ollama`, `docker compose ... up -d`, and 20+ days of paper trading in approve mode (RUNBOOK §1, TEST_PLAN stage 4).

## Quick commands

```powershell
.venv\Scripts\Activate.ps1
pytest -q                                              # all offline tests
trader validate-config --config config/config.yaml
trader replay --synthetic 10 --start 2026-10-05        # full system on synthetic days
trader backtest --synthetic 120 --walk-forward --in-sample 40 --out-sample 10
trader eval-prompts --llm ollama                       # AGENT_PROMPTS §8 against local Qwen
```
