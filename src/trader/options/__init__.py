"""Options suggestions and alerts (OPTIONS_REQUIREMENTS.md).

- ``pricing``: Black-Scholes prices, greeks and implied volatility (pure functions).
- ``data`` (A13): Alpaca option chain and snapshots, IV rank, expected move, events.
- ``strategist`` (A14): defined-risk structures, strikes, sizing, exits, computed numbers.
- ``risk`` (A15): wash sales, shared limits, event rules, account level, correlated groups.
- ``monitor`` (A16): paper tracking, follow-up alerts, grading, promotion gate.
- ``desk``: wires the four into trader-core (advisory: no option orders are placed).
"""
