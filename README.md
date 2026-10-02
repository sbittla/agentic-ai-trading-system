# Agentic AI Trading System

A disciplined, agent-based day and swing trading system for a fixed list of 10 large-cap US stocks.

- **Broker:** Alpaca (paper first, then live)
- **Models:** Qwen running locally in Docker (Ollama); only code can size and place orders
- **Runtime:** fully containerized with Docker Compose: `trader-core`, `news-sentiment`, `notifier`, `ollama`, `postgres`, `backup` (Python 3.12)
- **Data:** free sources (Alpaca IEX feed, Finnhub, SEC EDGAR, Reddit)
- **Trading hours:** first hour and last hour of the session only; no qualifying setup means no trade
- **Discipline:** hard loss limits, no averaging down, wash-sale prevention, full journal of every decision

> Personal research project. Not financial or tax advice.

## Documentation

Start with [docs/README.md](docs/README.md). The source of truth is [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Key documents

- [Requirements](docs/REQUIREMENTS.md) · [Strategy spec](docs/STRATEGY_SPEC.md) · [Architecture](docs/ARCHITECTURE.md) · [Implementation plan](docs/IMPLEMENTATION_PLAN.md) · [Runbook](docs/RUNBOOK.md) · [Account setup](docs/ACCOUNT_SETUP.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Build status](docs/BUILD_STATUS.md)

## Status

Milestones M0–M7 are built and tested offline: 263 tests plus Postgres integration tests, with all 137 automated requirement IDs covered. Paper trading (M8) needs Alpaca and Finnhub keys, a Telegram bot (Reddit optional), and Qwen on the local NVIDIA GPU: follow [docs/ACCOUNT_SETUP.md](docs/ACCOUNT_SETUP.md). See [docs/BUILD_STATUS.md](docs/BUILD_STATUS.md).

## Quick start (development)

```powershell
py -3.12 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[dev,notifier]"
trader init-env                                   # local config + env files from the templates
pytest -q
trader replay --synthetic 10 --start 2026-10-05   # whole system on synthetic days, SimBroker
```
