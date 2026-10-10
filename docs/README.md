# Documentation

Start with **REQUIREMENTS.md**, then read in the order below. Requirement IDs (for example `A7-2`, `WS-1`, `CK-6`) are used across all documents, in tests and in journal reason codes, so every behaviour can be traced from requirement to test to trade ([TRACEABILITY.md](TRACEABILITY.md)).

## What to build

| Document | What it covers |
| --- | --- |
| [REQUIREMENTS.md](REQUIREMENTS.md) | Source of truth: universe, trading windows, daily process, playbook, discipline rules, wash-sale process, every agent's requirements, acceptance tests |
| [STRATEGY_SPEC.md](STRATEGY_SPEC.md) | Exact, codeable rules for each setup, sizing algorithm, worked example, edge cases |
| [DECISIONS.md](DECISIONS.md) | Why the main design choices were made |

## How to build it

| Document | What it covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Containers, repo layout, event flow, trade states, scheduling, failure handling |
| [INTERFACES.md](INTERFACES.md) | Python interfaces for broker, data, LLM, notifier, clock; build files |
| [DATA_SPEC.md](DATA_SPEC.md) | External sources, indicator definitions, journal tables, message types |
| [AGENT_PROMPTS.md](AGENT_PROMPTS.md) | Qwen prompts, JSON schemas, validation, evaluation set |
| [SECURITY.md](SECURITY.md) | Threat model, WhatsApp command authentication, key management, audit |
| [ALERT_BOT_REUSE.md](ALERT_BOT_REUSE.md) | What moves over from the existing alert bot, what changes, what is dropped |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Milestones M0–M9, tasks, done criteria, risks |
| [BUILD_STATUS.md](BUILD_STATUS.md) | What is built and verified, design corrections found during the build, what the owner must provide |
| [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) | Local setup, coding rules, safe change procedures |

## How to prove it works

| Document | What it covers |
| --- | --- |
| [TEST_PLAN.md](TEST_PLAN.md) | Unit, replay, backtest, paper, live and chaos tests; go-live gates |
| [BACKTEST_ENGINE.md](BACKTEST_ENGINE.md) | Replay and backtest modes, fill model, LLM caching, walk-forward |
| [TRACEABILITY.md](TRACEABILITY.md) | Every requirement ID → agent → test → reason code |

## How to run it

| Document | What it covers |
| --- | --- |
| [HARDWARE.md](HARDWARE.md) | System requirements, the reference machine it is set up and tested on, and choosing the Qwen model for your GPU |
| [ALERTS.md](ALERTS.md) | Standalone lightweight Telegram price alerts (`trader alerts`): setup, alerts, commands |
| [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md) | Step by step: Docker + NVIDIA GPU, Alpaca, Finnhub, SEC EDGAR, Reddit, Telegram/WhatsApp, Postgres names and passwords, env files, first start, connection checks |
| [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) | Every config field and its validation; secrets |
| [RUNBOOK.md](RUNBOOK.md) | Setup, daily routine, commands, incident playbooks, go-live checklist |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Recovery sequence for Docker/WSL crashes and model loading; known problems with symptom, cause and fix |
| [MONITORING.md](MONITORING.md) | Health score, trading metrics, dashboards, alert routing |
| [REPORT_TEMPLATES.md](REPORT_TEMPLATES.md) | Pre-flight, game plan, approvals, trade events, daily/weekly/monthly reports |

## Other files

| File | Purpose |
| --- | --- |
| [`config/config.example.yaml`](../config/config.example.yaml) | All settings with v1 defaults |
| [`config/macro_calendar_2026_2027.yaml`](../config/macro_calendar_2026_2027.yaml) | CPI, jobs report and FOMC dates (2027 CPI and jobs dates still to add) |
| [`docker/`](../docker/) | `docker-compose.yml` (six services + profiles), multi-stage `Dockerfile`, `postgres/init.sql` |
| [`tools/devcontainer.json`](../tools/devcontainer.json) | VS Code dev container; copy to `.devcontainer/devcontainer.json` |
| [`pyproject.toml`](../pyproject.toml) | Package and tool configuration |
| [`tools/ci.yml`](../tools/ci.yml) | CI workflow: lint, types, tests, traceability, secret scan. Move it to `.github/workflows/ci.yml` to enable it |
| [`tools/check_traceability.py`](../tools/check_traceability.py) | Fails CI when a rule ID has no test |
| `.env.<name>.example`, [`docker/.env.example`](../docker/.env.example), [`secrets/README.md`](../secrets/README.md) | Templates for every local setup file; `trader init-env` creates the real ones ([ACCOUNT_SETUP.md](ACCOUNT_SETUP.md)) |
