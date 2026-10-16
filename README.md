# Agentic AI Trading System

A disciplined, agent-based day and swing trading system for a fixed list of 10 large-cap US stocks.

- **Broker:** Alpaca (paper first, then live)
- **Models:** Qwen running locally in Docker (Ollama); only code can size and place orders
- **Runtime:** fully containerized with Docker Compose: `trader-core`, `news-sentiment`, `notifier`, `ollama`, `postgres`, `backup`, the Owner Console (`console`, http://localhost:8080) and Grafana (Python 3.12). Nothing runs on the host: `./run.sh` starts everything, and helper commands run in a `toolbox` container
- **Options:** advisory defined-risk options ideas on the system's bullish and bearish views, paper-tracked and graded (no option orders)
- **Data:** free sources (Alpaca IEX feed, Finnhub, SEC EDGAR, Reddit, Yahoo Finance and Google News RSS for analyst upgrades/downgrades)
- **Trading hours:** first hour and last hour of the session only; no qualifying setup means no trade
- **Discipline:** hard loss limits, no averaging down, wash-sale prevention, full journal of every decision

> Personal research project. Not financial or tax advice.

**Technical scanner:** `./run.sh scan` ranks swing ideas from 9 classic techniques (pullbacks, breakouts, flags, squeezes, Connors RSI(2), MACD, reversal candles; long and short) and a next-session day-trading watchlist, with trigger, stop and targets. Indicator and candlestick math is identical to TA-Lib, and `./run.sh scan-eval` grades every technique on stored history. trader-core sends the scan to Telegram at 16:20 ET. See [docs/SCANNER_REQUIREMENTS.md](docs/SCANNER_REQUIREMENTS.md) for why this is a deterministic engine rather than an LLM, and for the measured results.

**News catalysts:** `./run.sh catalyst [TICKER]` scores how strong a stock's news is (0-100), gives P(up), the typical move and a 10-90% range for 1 day, 1 week, 2 weeks and 1 month, reads social sentiment next to the news, checks technicals, options, analysts, earnings, insiders and short volume, and suggests a view (stock, call or put spread, or no trade) sized by the risk rules. Run `./run.sh backfill-catalysts 365` once so the ranges are measured on a year of past events; outlooks say UNCALIBRATED until the walk-forward record earns the label. See [docs/NEWS_CATALYST_REQUIREMENTS.md](docs/NEWS_CATALYST_REQUIREMENTS.md).

**Just want day-trading price alerts on Telegram?** Run the standalone alerts bot: one ~50 MB
container, no GPU, LLM or database. See [docs/ALERTS.md](docs/ALERTS.md):
`./run.sh alerts`

## Documentation

Start with [docs/README.md](docs/README.md). The source of truth is [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Key documents

- [Requirements](docs/REQUIREMENTS.md) · [Strategy spec](docs/STRATEGY_SPEC.md) · [Technical scanner](docs/SCANNER_REQUIREMENTS.md) · [News catalysts](docs/NEWS_CATALYST_REQUIREMENTS.md) · [Gap review](docs/GAP_REVIEW.md) · [Architecture](docs/ARCHITECTURE.md) · [Implementation plan](docs/IMPLEMENTATION_PLAN.md) · [Runbook](docs/RUNBOOK.md) · [Account setup](docs/ACCOUNT_SETUP.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Build status](docs/BUILD_STATUS.md)

## Status

Milestones M0–M7 plus REQUIREMENTS 1.3, the options layer and the Owner Console are built and tested offline: 485 tests including the Postgres integration tests, with all 268 automated requirement IDs covered. Paper trading (M8) needs Alpaca and Finnhub keys, a Telegram bot (Reddit optional), and Qwen on the local NVIDIA GPU: follow [docs/ACCOUNT_SETUP.md](docs/ACCOUNT_SETUP.md). See [docs/BUILD_STATUS.md](docs/BUILD_STATUS.md).

## Run it (WSL, repo root)

```bash
./run.sh setup      # once: env files + passwords (git-ignored), model for your GPU
./run.sh            # pre-flight checks, then start every agent, Grafana and the console
./run.sh status     # every container and its health; ./run.sh help for the rest
./run.sh backfill 30  # once: last month's news, analyst ratings, recap and next-day ideas
```

From a Windows Command Prompt or PowerShell, `run.cmd` takes the same commands (`run.cmd setup`, `run.cmd`, `run.cmd status`, `run.cmd help`); it runs `tools/run.ps1` and needs only Docker Desktop, not WSL.

Full procedure: [docs/RUNBOOK.md](docs/RUNBOOK.md) §1. The Owner Console (http://localhost:8080) shows today's cockpit, a market recap, analyst ratings, news, next-day ideas, the options board and the controls.

## Quick start (development)

Everything runs in containers, including the checks:

```bash
./run.sh test        # ruff, mypy, pytest and the traceability check in the dev container
./run.sh replay 10   # whole system on 10 synthetic days, SimBroker, no keys or GPU
```

VS Code users can open the repo in the dev container (`.devcontainer/`, [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)).