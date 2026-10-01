# AIAgentsTrading

A disciplined, agent-based day and swing trading system for a fixed list of 10 large-cap US stocks.

- **Broker:** Alpaca (paper first, then live)
- **Models:** Qwen running locally in Docker (Ollama); only code can size and place orders
- **Data:** free sources (Alpaca IEX feed, Finnhub, SEC EDGAR, Reddit)
- **Trading hours:** first hour and last hour of the session only; no qualifying setup means no trade
- **Discipline:** hard loss limits, no averaging down, wash-sale prevention, full journal of every decision

> Personal research project. Not financial or tax advice.

## Documentation

Start with [docs/README.md](docs/README.md). The source of truth is [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Status

Design phase. Next: build the journal, market data and risk manager (stage 1 of [docs/TEST_PLAN.md](docs/TEST_PLAN.md)).
