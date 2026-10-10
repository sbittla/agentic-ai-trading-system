# Telegram Price Alerts (`trader alerts`)

A standalone, lightweight day-trading alert bot. It watches up to 30 symbols on Alpaca's free
IEX feed and sends alerts to your Telegram chat. By default it watches the 20 largest S&P 500
companies by market cap (NVDA, AAPL, GOOGL, MSFT, AMZN, META, AVGO, TSLA, MU, LLY, BRK.B, AMD,
JPM, WMT, V, XOM, JNJ, INTC, MA, ABBV; October 2026 snapshot) plus SPY and QQQ, which leaves
8 slots for your own picks (`/watch`). Rankings change: review the list each quarter. It does **not** trade, and it does not need
Postgres, Ollama/Qwen, a GPU, FinBERT/PyTorch or Grafana.

| | Full stack (`docker compose up`) | `alerts` only |
| --- | --- | --- |
| Containers | core, news, notifier, ollama, postgres, backup (+ grafana) | 1 |
| RAM | several GB (Qwen 8B in VRAM, PyTorch, Postgres) | ~40–60 MB |
| GPU | required | none |
| Network off-hours | WebSocket + polling all day | one idle Telegram long-poll every 50 s |

## How it stays light

- One Python process, imports no pandas/numpy/alpaca-py at runtime: it talks to Alpaca with
  `websockets` + `httpx` and passes raw JSON frames to a pure in-memory engine.
- The Alpaca WebSocket is open only from 5 minutes before the open to 1 minute after the close
  (Alpaca's clock decides, so holidays and half days need no calendar). Between sessions the
  process just waits on Telegram.
- 1-minute **bars** for every watched symbol, but **trades** (the busy channel) only for
  symbols that have a price rule, and only while the rule exists.
- Alerts from one WebSocket frame go out as one Telegram message; one pooled HTTPS connection
  is reused for every Telegram call.
- State (rules, watchlist, settings, 30-day alert log) is a small SQLite file.

## Setup

1. Telegram: message **@BotFather**, `/newbot`, copy the token. Send any message to your bot.
2. Alpaca: an account's paper API key/secret is enough (the IEX data feed is free).
3. Create the env file and fill it in:

   ```bash
   cp .env.alerts.example .env.alerts            # or: trader init-env
   trader telegram-chats --env-file .env.alerts  # shows your chat id -> TELEGRAM_CHAT_ID
   ```

4. Optional: `cp config/alerts.example.yaml config/alerts.yaml` and edit the watchlist and
   signal settings. Without it the built-in defaults are used.
5. Run it:

   ```bash
   # Docker (recommended): one small container, restarts automatically
   docker compose -f docker/docker-compose.yml --profile alerts up -d alerts
   docker compose -f docker/docker-compose.yml logs -f alerts

   # or directly (Python 3.12)
   pip install -e . websockets
   set -a; . ./.env.alerts; set +a
   trader alerts
   ```

> Alpaca allows **one** market-data WebSocket per account. Do not run `alerts` and
> `trader-core` with the same Alpaca keys at the same time (use a second paper account, or
> run only one of them).

## Alerts you get

**Price rules** (you set them; checked on every trade, so they fire within about a second):

| Command | Fires when |
| --- | --- |
| `/add NVDA above 140` · `NVDA > 140` | price ≥ 140 |
| `/add NVDA below 120.5` · `NVDA < 120.5` | price ≤ 120.5 |
| `/add TSLA 250` | above or below, picked from the current price |
| `/add AAPL up 3%` · `AAPL +3%` | up 3% from the prior close |
| `/add AAPL down 2%` · `AAPL -2%` | down 2% from the prior close |

Rules are one-shot by default (removed after firing). Add `repeat` to keep a rule: it re-arms
once price moves back 0.15% past the level, so it does not spam while price chops around it.
Anything after the number is a note shown in the alert: `/add SPY below 560 repeat stop zone`.
A lone print more than 5% from the last price is ignored until a second print confirms it
(bad ticks).

**Auto signals** (every watched symbol, on 1-minute bar closes):

| Signal | Rule |
| --- | --- |
| Gap | open ≥ 2% from the prior close (once a day) |
| Opening-range breakout / breakdown | close above/below the first 15 minutes' high/low (once a day each) |
| VWAP reclaim / loss | close crosses session VWAP (from 09:35, 15-min cooldown) |
| New high / low of day | after the opening range (15-min cooldown) |
| Volume spike | 1-min volume ≥ 4× the last 20 minutes' average |
| Day move | every ±2% from the prior close, each level once a day |

All thresholds are in `config/alerts.yaml`. After the close you get a summary: alert count and
the top and bottom movers.

## Commands

| Command | |
| --- | --- |
| `/list [SYM]` · `/del 3 [4 5]` · `/clear [SYM]` | manage price rules |
| `/watch AMD PLTR` · `/unwatch AMD` · `/watchlist` | edit the watchlist (saved; max 30) |
| `/price [SYM ...]` | live price, change, VWAP, range, opening range |
| `/movers` | watchlist sorted by % change today |
| `/signals on\|off` | turn auto signals off (price rules still fire) |
| `/mute [minutes]` · `/unmute` | silence everything for a while (default 60 min) |
| `/status` · `/help` | feed state, counts, help |

Only chat ids in `TELEGRAM_ALLOWED_CHAT_IDS` (default: `TELEGRAM_CHAT_ID`) are answered;
messages sent while the bot was offline are skipped at start-up.

## Planned changes (REQUIREMENTS 1.3, §10.5)

Not built yet. These change how alerts work, for this bot and for alerts sent by trader-core:

| ID | Change |
| --- | --- |
| AL-1 | While trader-core runs, it sends the alerts from its own stream, because Alpaca allows one WebSocket per account. This bot is for when the core is off, or for a second account |
| AL-2 | Each alert is labelled **raw signal** or **qualified setup (checklist n/10, first failed item)** |
| AL-3 | Each directional alert carries a plan: trigger, invalidation (stop), T1/T2, share size for the risk budget, R:R, RVOL, position against VWAP and QQQ |
| AL-4 | Schedule `windows` (default: pre-market, 9:30–10:30, 3:00–4:00) or `all_day` |
| AL-5 | At most 3 alerts per symbol per window, and 12 per window in total |
| AL-6 | Ranked by quality; the top 5 per 10 minutes go out at once, the rest in a 10-minute digest |
| AL-7 | Every alert type is graded (+1R before −1R within 30 minutes); weak types are flagged |

## Notes

- IEX is ~2–3% of US volume: prices track the market closely, volume numbers are IEX-only
  (spikes are measured against IEX's own average, so they are still meaningful).
  Set `feed: sip` if you pay for Alpaca's SIP data.
- While muted, a one-shot rule that fires is still used up (it is logged, not sent).
- Not financial advice; alerts are information, not trade signals.
