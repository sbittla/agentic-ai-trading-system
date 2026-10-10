# Owner Console: UI Requirements

**Version:** 0.1 draft (October 10, 2026) · Status: **for owner review, nothing built yet**

The owner already has three ways to see and control the system:

- the Telegram bot (commands and approvals)
- six Grafana dashboards (read-only analytics)
- the `trader` / `./run.sh` command line

None of them lets the owner **ask a question in plain language**, **look up any stock on demand**, or see one trade's whole story in one place.

This document defines an **Owner Console**: a local web app that adds those abilities and keeps every existing safety rule. Requirement IDs are `UI-n`, so they can be traced like the rest ([TRACEABILITY.md](TRACEABILITY.md)).

---

## 1. What exists today and what is missing

| Need | Today | Gap |
| --- | --- | --- |
| Is the system OK right now? | Telegram `STATUS` (JSON), Grafana "1. Today", `./run.sh status` | Three places; the JSON is hard to read; nothing shows GPU/VRAM or model load |
| Why didn't it trade? | Grafana "2. Pipeline" no-trade reasons; journal `decisions` | No per-ticker story; reason codes are not explained in words |
| What does the system think of stock X? | Only the 10 universe tickers, spread across dashboards | **No lookup for an arbitrary symbol**; no on-demand chart, indicators, news or checklist |
| Ask a question ("how did ORB do this month?") | Not possible | **No natural-language Q&A** |
| Approve a trade | Telegram `YES <code>` within 90 s | Only on the phone; no chart or context next to the approval |
| Next-day ideas | 4:15 PM Telegram message, Grafana "6. Next-day ideas" | No drill-down from an idea to its chart, screen scores and history |
| Kill, pause, flatten | Telegram (with a confirmation code), CLI | Fine on the phone; no desktop button with the same safety |
| Price alerts | Alerts bot commands in Telegram | No list or editor on the desktop |
| Reports | Files in `reports/`, Telegram text | No browsable history |
| Wash-sale locks, tax lots | Grafana table, journal | No calendar of unlock dates; no per-ticker explanation |
| Model and hardware | `./run.sh model`, `check-connections` | Not visible while running (VRAM in use, latency, queue) |

Grafana stays: it is the right tool for time-series analytics. The console links to the relevant dashboard instead of copying it.

## 2. Constraints from the current design (must hold)

| ID | Constraint | Source |
| --- | --- | --- |
| UI-C1 | **Single owner, local only.** The console listens on `127.0.0.1` only (like grafana-proxy). No internet exposure, no tunnel, no inbound port on the router | SEC-9, ADR-013 |
| UI-C2 | **Reads use the read-only database role.** Every write goes through the outbox as an `inbound_cmd` message with `source = console`, so trader-core's `CommandAuthority` validates it exactly as it does Telegram commands. The console never writes trading tables and never talks to Alpaca's order API | SEC-17, SEC-2, SEC-3 |
| UI-C3 | **Approvals keep their one-time code. Destructive commands keep their second confirmation** (FLATTEN, RESUME, UNPAUSE). A click alone can never approve a trade or flatten | SEC-2, SEC-3 |
| UI-C4 | **Only code sizes and places orders.** The question-answering feature is read-only: it can explain and *suggest* a command, never issue one | ADR, A7/A8 |
| UI-C5 | **One GPU model, one request at a time.** Owner questions share Qwen with the trading agents (`OLLAMA_NUM_PARALLEL=1`, 8 GB VRAM). Trading calls always go first; no second model is loaded | QW-1, QW-3, HARDWARE.md |
| UI-C6 | **Every figure comes from the journal or the broker, never from the model.** The model may phrase answers, not invent numbers | A11-3 |
| UI-C7 | Times shown in US Eastern; money as Decimal rounded to cents; paper/live always visible | IF-1, IF-2, SEC-7 |
| UI-C8 | Resource budget: console container ≤ 200 MB RAM and ~0% CPU when idle (the reference laptop has ~13 GB free) | HARDWARE.md |

## 3. Functional requirements

### 3.1 Home: today's cockpit

- **UI-1** One screen answers "is everything OK and what is happening":
  - mode banner (PAPER or LIVE, red for live)
  - kill-switch, halted and paused states
  - equity, day P&L against the 1.5% loss limit, trades used (of 3)
  - health of every component as green/amber/red with its detail text
  - the current trading window and a countdown to the next one
- **UI-2** Open positions: ticker, style, quantity, entry, stop, T1, unrealized P&L in $ and R, time in trade. Each position links to its trade story (UI-9).
- **UI-3** Today's game plan and the next-day ideas, each linking to its stock page (UI-4).
- **UI-4a** Service and model panel:
  - each container's heartbeat age
  - Ollama: model tag, whether it is loaded, VRAM used of total, p95 latency today, current queue length
  - links to the matching Grafana dashboards
- **UI-4b** Open incidents and unacknowledged critical alerts, with an **ACK** button (sends `ACK` through UI-C2).

### 3.2 Stock lookup: "check any stock"

- **UI-4** A search box takes **any US symbol**, not only the 10 universe tickers. The page shows:
  1. quote: last, day change from the prior close, bid/ask spread, volume vs the average at this time of day (RVOL)
  2. an intraday chart (1- and 5-minute bars) with VWAP, the opening range, prior-day high/low/close, the day's levels (A4) and markers for this system's entries, exits and stops; a daily chart with EMAs and ATR
  3. indicators from the existing A4 code (`indicators.py`), computed on demand: RSI, MACD, ATR(5m), RVOL, gap %, position in the day's range, Bollinger/Keltner
  4. news and sentiment: latest headlines with source, time and FinBERT score; earnings date; event flags (A3)
  5. status in this system:
     - in the universe or not
     - correlated group
     - wash-sale lock and unlock date
     - in today's or tomorrow's ideas
     - past trades and their R results
- **UI-5 Checklist dry-run.** For universe tickers, show the entry checklist (REQUIREMENTS §5.4) evaluated *now*: each item pass/fail with the measured value and the threshold. The answer to "why isn't the system buying NVDA?" is one click. For other symbols, mark it "informational: not tradable (UR-1)".
- **UI-6** Buttons:
  - add a price alert for this symbol (UI-14)
  - ask a question about this stock (UI-7, prefilled)
  - open in Grafana
- **UI-6a** Data limits are stated on the page: the free Alpaca IEX feed covers about 2–3% of volume; Finnhub's free tier allows 60 calls a minute (lookups are cached for 60 s); symbols outside the universe get no FinBERT run unless the owner asks for it.

### 3.3 Ask: plain-language questions

- **UI-7** A chat box answers questions about **the system and the market data it holds**, for example:
  - "Why were there no trades this morning?"
  - "How did the ORB setup do in the last 30 days, by ticker?"
  - "What is my expectancy on Fridays?"
  - "Show NVDA's ideas this week and whether they triggered."
  - "Which tickers are wash-sale locked and until when?"
  - "Compare AAPL and MSFT today: trend, RVOL, VWAP."
  - "What happened at 10:02 when the AVGO proposal was rejected?"
- **UI-8** How answers are built (UI-C4, UI-C6):
  - Qwen does not see the database. It picks from a fixed set of **read-only tools** (typed Python functions over the journal and market data), such as:
    - `decisions(date, ticker)`, `trades(range, setup)`, `metrics(range, group_by)`
    - `ideas(date)`, `locks()`, `quote(symbol)`, `bars(symbol, tf, range)`
    - `checklist(symbol)`, `news(symbol)`, `incidents(range)`
  - Code runs the tools and returns rows. Qwen writes the answer in words.
  - Every number in the answer is shown with a **source link** to the rows it came from (table + id, or the chart).
  - If no tool can answer, it says so; it never guesses.
  - Questions about the future ("will NVDA go up?") get the system's current state and setups, with a standard "not advice" note, never a prediction.
- **UI-8a** Command suggestions: if the answer implies an action ("pause day trading"), it shows a **suggested command** button that opens the normal control flow (UI-12) with its confirmation. The model never sends it.
- **UI-8b** Priority and latency (UI-C5):
  - Questions go to a **low-priority queue** behind every trading call.
  - During entry windows (09:35–10:30, 15:00–15:55 ET) they wait until the window closes, unless the question needs no model (tables and charts render without Qwen).
  - The UI shows "queued, about N s" and lets the owner cancel.
  - Target: answer within 20 s outside windows on the reference machine.
- **UI-8c** Safety:
  - News text, Reddit text and filings are passed to the model as quoted data, never as instructions (same rule as A3, SEC prompt-injection controls).
  - Tool arguments are validated (symbol pattern, date range ≤ 1 year, row limits).
  - Every question, tool call and answer is logged to `llm_calls` with `agent = "console"`.
- **UI-8d** Conversation history is kept per day, can be searched, and can be cleared.

### 3.4 Approvals and trade story

- **UI-9 Trade story.** One page per proposal or trade, built from the journal:
  - setup and trigger
  - checklist results
  - A5 proposal text
  - A6 review (agree or disagree, with arguments)
  - A7 risk sizing (shares, $ risk, R)
  - A12 compliance check
  - approval (who, when, response time)
  - orders, fills, stop moves, exits, final R
  - reason codes explained in words
  - the chart with every event marked
- **UI-10 Approvals inbox.** A pending proposal appears on the console at the same moment as on Telegram, with the trade-story context and a 90-second countdown. To approve, the owner **types the one-time code** shown on the phone (UI-C3), so a stolen browser session cannot approve on its own. The first answer wins, from either channel; the other then shows it as answered.
- **UI-11** Expired, rejected and approved proposals stay listed for the day with their outcome.

### 3.5 Controls

- **UI-12** Buttons for STATUS, PLAN, KILL, PAUSE DAY / SWING, ACK, and FLATTEN, RESUME, UNPAUSE.
  - Destructive ones show what will happen ("close 2 positions: NVDA 40 sh, MSFT 15 sh, ~$X at market") and require the confirmation code that trader-core returns (UI-C3).
  - KILL needs no confirmation: it is the safe direction, as on Telegram.
- **UI-13** Every control action is written to the `commands` journal table with `source = console` and shown in an audit list (SEC-14).

### 3.6 Alerts, ideas, reports, compliance

- **UI-14** Price-alert manager for the alerts bot:
  - list, add, edit and delete rules
  - see the watchlist and change it
  - mute or unmute; turn signals on or off
  - see today's alert log
- **UI-15** Next-day ideas page:
  - picks per window with trigger, stop, T1 and readiness score
  - the screen for every ticker (why it was or wasn't picked)
  - the track record of past ideas (triggered, reached T1, stopped)
- **UI-16** Reports archive: daily, weekly and monthly reviews and wash-sale reports, from `reports/` and the journal, readable in the browser and downloadable.
- **UI-17** Compliance view:
  - a calendar of wash-sale locks and unlock dates
  - tax lots with adjusted basis
  - the monthly wash-sale report
  - for each lock, the loss sale that caused it

### 3.6a Options (draft)

- **UI-28** Options tab, payoff view, suggestions list and options Q&A tools: OP-16 to OP-19 in [OPTIONS_REQUIREMENTS.md](OPTIONS_REQUIREMENTS.md) §8.

### 3.6b Trader workflow (REQUIREMENTS 1.3)

- **UI-29 Owner journal:** for each trade (system, co-pilot or shadow), the owner can add notes, tags (FOMO, hesitated, early exit, followed the plan) and a screenshot. The weekly review shows tag statistics beside the A/B/C grades.
- **UI-30 Live scanner:** the watchlist and in-play names (UN-1), ranked by % change, RVOL, relative strength against QQQ and distance from VWAP, with each alert's qualified/raw label (AL-2). Click through to the stock page.
- **UI-31 Latency and missed trades:** a panel with today's trigger-to-order times (LAT-1), the missed triggers by reason (LAT-4), and the shadow result of each (SH-1).
- **UI-32 Co-pilot ticket:** in co-pilot mode (CP-1), the plan is shown as a ticket. "I filled" records the fill (CP-2), and exit alerts follow.

### 3.7 System and settings (read-mostly)

- **UI-18** Config viewer: the effective config for today (from `config_snapshots`) and any edits waiting for tomorrow (SR-5), as a diff. Config is still edited in `config/config.yaml`; the console does not write it (DV-3).
- **UI-19** Model and hardware page: the `select-model` table for this machine, the configured model, and the live VRAM use. Changing the model stays a `./run.sh model --apply` step.
- **UI-20** Replay and backtest results: list runs in `runs/`, show their summary and compare two runs. Launching runs from the UI is a later phase.

## 4. Non-functional requirements

- **UI-21 Security:**
  - localhost only
  - login with a password stored in `secrets/console_password.txt`, session cookie (HttpOnly, SameSite=Strict), CSRF token on every action, 30-minute idle timeout
  - Content-Security-Policy with no third-party scripts; no external CDNs (the internal network has no internet)
- **UI-22 Freshness:**
  - Home updates live via server-sent events, at most 5 s behind the journal.
  - Quotes on the stock page refresh every 5 s while it is open and stop when it is hidden.
- **UI-23 Resources:**
  - one small container (Python, FastAPI or similar, server-rendered pages with a small charting library bundled locally), ≤ 200 MB RAM
  - Postgres access only through the read-only role, plus outbox inserts through a new `console` role that may publish only to `inbound_cmd` (SEC-17 guard extended)
- **UI-24 Works on a phone browser on the same machine or LAN later**, but v1 is desktop only (localhost). Telegram remains the mobile channel.
- **UI-25 Accessibility and clarity:**
  - Colour is never the only signal (green/amber/red also have text).
  - Keyboard shortcuts: `/` focuses search, `g h` goes Home.
  - Light and dark themes.
- **UI-26 Testability:**
  - Every page renders against the replay journal (`trader replay`), so the UI is tested offline in CI with no broker or GPU.
  - Q&A tools have unit tests; answers are checked against tool output, so no number appears that a tool did not return.
- **UI-27 Failure behaviour:**
  - If Postgres, Ollama or market data is down, the console still loads and shows what is missing.
  - Q&A falls back to "model unavailable" with the table view.
  - Nothing in the console can block trading.

## 5. Suggested phases

| Phase | Scope | Why first |
| --- | --- | --- |
| P1 (read-only) | UI-1–UI-6, UI-9, UI-15, UI-21–UI-23 | Highest value, no write path, no new risk |
| P2 (ask) | UI-7–UI-8d | Needs the tool layer and the GPU queue |
| P3 (act) | UI-10–UI-13, UI-14 | Writes through the outbox with the existing safety codes |
| P4 | UI-16–UI-20 | Archives and settings |

## 6. Open questions for the owner

1. **Any stock, or the universe plus a watchlist?** Arbitrary symbols cost Finnhub calls and, if FinBERT should run on them, CPU time in news-sentiment. Proposal: any symbol for price, chart and indicators; news and sentiment only on request.
2. **Should questions run during entry windows?** Proposal: no model calls during 09:35–10:30 and 15:00–15:55 (UI-8b), so trading latency is never affected on the 8 GB GPU.
3. **Approve from the console at all,** or keep approvals on Telegram only and show them read-only? Typing the phone code keeps it safe but adds a step.
4. **Alerts bot integration:** its rules live in its own SQLite file. Either the console gets that volume (simple, single machine), or the alerts bot moves its rules into Postgres.
5. **Phone access over the LAN** (not just localhost) in a later phase? It needs TLS and is a deliberate change to SEC-9.
6. **Preferred look:** a dense trading-terminal style or a simpler card layout?
