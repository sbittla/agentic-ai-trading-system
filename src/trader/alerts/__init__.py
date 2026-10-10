"""Lightweight day-trading price alerts to Telegram (``trader alerts``).

A single small process: one Alpaca IEX WebSocket for trades and 1-minute bars, a pure
in-memory alert engine, SQLite for the owner's rules and watchlist, and the Telegram Bot API
for alerts and commands. No Postgres, Ollama or PyTorch, so it runs in ~60 MB of RAM and
sleeps between sessions (the WebSocket is only open while the market is).
"""
