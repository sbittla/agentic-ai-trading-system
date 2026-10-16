# Decision Log

**Version:** 1.2 (October 11, 2026)

Short records of the main design decisions, so later changes can see why things are the way they are. New decisions are appended; superseded ones are marked, not deleted.

| ID | Date | Decision | Status |
| --- | --- | --- | --- |
| ADR-001 | 2026-09-30 | Broker: Alpaca | Accepted |
| ADR-002 | 2026-09-30 | Model: Qwen, local, in Docker via Ollama | Accepted |
| ADR-003 | 2026-09-30 | Data: free sources only (Alpaca IEX, Finnhub, EDGAR, Reddit) | Accepted |
| ADR-004 | 2026-09-30 | Universe: fixed owner list of 10 large caps | Accepted |
| ADR-005 | 2026-09-30 | Trade only the first and last hour | Accepted |
| ADR-006 | 2026-09-30 | Only code can size and place orders | Accepted |
| ADR-007 | 2026-09-30 | Strict wash-sale policy by default | Accepted |
| ADR-008 | 2026-09-30 | Long only in v1 | Accepted |
| ADR-009 | 2026-09-30 | One service with an event bus, not one container per agent | Superseded by ADR-010 |
| ADR-010 | 2026-09-30 | Containerize everything: six services split by isolation need | Accepted |
| ADR-011 | 2026-09-30 | Python 3.12 for all application code | Accepted |
| ADR-012 | 2026-09-30 | Postgres as journal and cross-container bus (outbox + LISTEN/NOTIFY) | Accepted |
| ADR-013 | 2026-09-30 | Outbound-only notifications: poll Twilio for replies, no inbound endpoint | Accepted |
| ADR-014 | 2026-10-01 | Telegram bot as the default notifier channel; WhatsApp kept as an option | Accepted |
| ADR-015 | 2026-10-10 | Options: advisory first, defined risk only, bearish views via puts (no stock shorting) | Proposed |
| ADR-016 | 2026-10-11 | Analyst ratings from public RSS headlines parsed by rules, not by the model | Accepted |
| ADR-017 | 2026-10-16 | News catalyst outlooks from a pooled event study, not a forecasting model | Accepted |
| ADR-018 | 2026-10-16 | Portfolio risk limits and an independent pre-trade order check | Accepted |

---

### ADR-001 Broker: Alpaca

**Context:** Webull's API needs a paid market data subscription; Robinhood's agent access is MCP-based and less controllable from custom code.
**Decision:** Alpaca for paper and live trading.
**Consequences:** Free $100K paper account, commission-free stocks, official Python SDK, bracket orders. Bracket orders require whole shares and regular hours only.

### ADR-002 Model: Qwen in Docker

**Context:** Cost and privacy; the owner already runs models locally.
**Decision:** Qwen3 (8B default for the 8 GB reference GPU; 14B/32B when they fit fully in GPU memory, chosen with `trader select-model`) served by the Ollama Docker image, JSON-constrained output at temperature 0.
**Consequences:** No per-call cost. Reasoning is weaker than large commercial models, so the rules, checklist and code validation carry the decisions; Qwen filters and explains. GPU memory and uptime become operational concerns.

### ADR-003 Free data only

**Decision:** Alpaca free feed (IEX real-time), Finnhub, SEC EDGAR, Reddit.
**Consequences:** IEX is one exchange, so volume is partial; RVOL is always measured against the same feed. Upgrade path: Alpaca Algo Trader Plus ($99/month) if replays show IEX data hurts entries.

### ADR-004 Fixed list of 10 stocks

**Decision:** Trade only the owner's list (top 10 by market cap at the start). A1 reports changes weekly but never edits the list.
**Consequences:** Deep liquidity and tight spreads; the agents learn a small set well. Wash-sale locks bite harder on a small list (see ADR-007).

### ADR-005 First and last hour only

**Decision:** Entries 9:35–10:15 and 3:00–3:40 (swing 3:30–3:50); morning trades closed by 10:30, day trades by 3:55.
**Consequences:** Trades the highest-volume periods; avoids midday chop; fewer trades. Morning time stop may cut some winners early (open question: allow to 11:00?).

### ADR-006 Only code can trade

**Decision:** Qwen agents produce proposals, reviews and summaries. The risk manager (code) runs the checklist, sizes and approves; only the execution agent (code) calls the broker.
**Consequences:** A model error or prompt injection cannot place an order. Some good trades will be skipped by strict rules; that is accepted.

### ADR-007 Strict wash-sale policy

**Decision:** 31-day lock on a ticker after any loss; strict year-end rule; balanced and year-end-only policies available by config.
**Consequences:** Zero wash sales in this account, at the cost of fewer tradable tickers after losses. Policy to be confirmed with a tax advisor.

### ADR-008 Long only

**Decision:** No short selling in v1.
**Consequences:** Simpler margin and borrow handling; no trades on down days unless a long setup with relative strength appears.

### ADR-009 One service with an event bus (superseded by ADR-010)

**Decision:** All agents run in one Python process communicating through typed, journaled events.
**Consequences:** Shared state is simple and fast; replay testing is straightforward. Agents can be split out later because messages are already typed.

### ADR-010 Containerize everything, split by isolation need

**Context:** The owner wants the system containerized as much as possible. Splitting has costs (network hops, more failure points) and benefits (isolation, smaller blast radius).
**Decision:** Six always-on containers: `ollama`, `trader-core` (A1, A2, A4–A12), `news-sentiment` (A3), `notifier`, `postgres`, `backup`; plus on-demand `replay`, `dev` and (later) `grafana`. Split where it buys isolation: GPU model, heavy PyTorch dependencies, untrusted text, internet-facing messaging, data. Keep the decision-and-order path in one container.
**Consequences:** The risk manager keeps exact in-memory state; Alpaca's one-connection market-data limit is respected by keeping the stream in `trader-core`; each container gets only its own secrets and Postgres role. More health checks to watch (MONITORING.md).

### ADR-011 Python for application code

**Decision:** Python 3.12 for every service; SQL for the journal; YAML for config; PowerShell for host scripts.
**Consequences:** Official Alpaca SDK, pandas, Pydantic, FinBERT and the existing alert-bot code all work directly. The system acts in seconds, so Python's speed is sufficient. No second language toolchain in v1.

### ADR-012 Postgres as journal and bus

**Decision:** Postgres 16 holds the journal and an `outbox` table; cross-container messages are outbox rows announced with `LISTEN/NOTIFY`. Inside `trader-core`, an in-process bus handles high-rate events.
**Consequences:** "Written before delivered" comes for free; one fewer service than Redis or NATS. Cross-container traffic is low-rate (flags, commands, reports), well within Postgres limits. If Postgres is down, entries stop.

### ADR-013 Outbound-only notifications

**Context:** Twilio normally delivers WhatsApp replies to a public webhook, which would need a tunnel into the home PC.
**Decision:** The notifier polls Twilio's Messages API for inbound replies (every 3 s during approvals, every 10 s otherwise in market hours, every 60 s outside). No inbound ports or tunnels.
**Consequences:** No internet-reachable endpoint on the host. Command latency up to 10 s (3 s for approvals), acceptable within the 90-second approval window; the local `trader kill` command is instant. Alternative if WhatsApp polling proves unreliable: a Telegram bot, which also works outbound-only.

### ADR-014 Telegram bot as the default notifier channel

**Context:** During setup (October 1, 2026) the owner's Twilio trial WhatsApp number rejected every free-form message with "ContentSid Required", even inside the 24-hour window, and the only available template was Twilio's fixed sample. A permanent WhatsApp sender needs a paid account, Meta business verification and approved templates.
**Decision:** The notifier supports two channels behind the same interface. Telegram (Bot API) is the default when `TELEGRAM_BOT_TOKEN` is set (`NOTIFIER_CHANNEL` overrides). It stays outbound-only, as ADR-013 requires: `sendMessage` to send, `getUpdates` long polling to receive, no webhook. Only allow-listed chat IDs can issue commands (SEC-1); approval codes and two-step confirmations are unchanged (SEC-2, SEC-3). At start-up the notifier skips messages that arrived while it was down, so an old command is never executed.
**Consequences:** Free, no templates and no 24-hour window; replies arrive within about a second. New network destination `api.telegram.org` for the `notifier` container only (SECURITY.md). Requirement text that says "WhatsApp" applies to whichever channel is active.

### ADR-015 Options: advisory first, defined risk only (proposed)

**Context:** The system has bullish and bearish views (A4 setups, next-day ideas, alerts-engine signals) but trades only long stock (ADR-008). The owner wants options suggestions and alerts for both directions.
**Decision:** Add an options layer (agents A13–A16, [OPTIONS_REQUIREMENTS.md](OPTIONS_REQUIREMENTS.md)) that starts in `advisory` mode: suggestions, follow-up alerts and paper grading, no orders. Only defined-risk structures (long calls/puts, debit and credit verticals). Bearish views are expressed with puts, put spreads or call credit spreads, never by shorting stock, so ADR-008 stands. Options share the stock risk budget and wash-sale locks in both directions. Order placement needs a per-structure promotion gate (A16-4), then paper, then live gates.
**Consequences:** Useful bearish ideas without borrow or margin rules; no undefined-loss positions; no extra GPU load in market hours (code templates, Qwen only after the close). Costs: an options data dependency (indicative free feed; OPRA paid), more config and tests, and tax-lot handling for exercise and assignment once orders are enabled.
**Implementation (October 10, 2026):** the advisory mode is built (`src/trader/options/`, `agents/a04_bearish.py`, Grafana **7. Options**, console Options page) and tested (`tests/unit/test_options.py`). The status stays *proposed* until the owner accepts it; `options.mode: off` disables the layer.

### ADR-016 Analyst ratings from RSS headlines, parsed by rules

**Context:** The owner wants upgrades and downgrades per stock. Finnhub's `stock/upgrade-downgrade` endpoint is not in every free plan, and paid ratings feeds conflict with ADR-003.
**Decision:** Use Finnhub when the key allows it (switch it off for the session on 401/403) and otherwise Yahoo Finance and Google News RSS headlines, parsed by regular expressions in `news/ratings.py`. A headline counts only when the ticker or company is its subject. Qwen is not used: headlines are untrusted text (A3-4) and the rules are testable.
**Consequences:** Free and deterministic, with links to the source. Coverage depends on headline wording, so some actions are missed and price targets appear only when the headline states them. Every action is stored once (`action_hash`) in `analyst_actions` and raises the A3-3 rating-change flag.

### ADR-017 News catalyst outlooks from a pooled event study

**Context:** The owner wants, for each stock with news, the strength of the news, what may happen over 1 day to 1 month, the likely up/down range and a position view.
**Decision:** Models read text, and statistics estimate moves. FinBERT (news), FinTwitBERT (social posts) and MiniLM (story grouping) run on CPU in news-sentiment; Qwen only types stories rules miss and never sets a number. Ranges come from implied and realized volatility. The tilt comes from a pooled event study of past stories, in volatility units, shrunk toward zero by sample size. Everything is graded walk-forward, and adjustments are kept only if they improve the result. Price-forecasting foundation models (Chronos, TimesFM) and a larger finance LLM were rejected: they ignore the news or do not fit the 8 GB GPU, and show no reliable edge on single-stock daily returns. A LightGBM quantile model may be added once there is graded history and it beats the event study.
**Consequences:** Explainable, deterministic, no GPU change (+~0.6 GB RAM). Outlooks are labelled UNCALIBRATED, and alerts stay off, until the NC-34 gate passes. With little history the outlook is the volatility range with no tilt, and the view is NO_TRADE, which is the honest answer.

### ADR-018 Portfolio risk limits and an independent pre-trade order check

**Context:** The per-trade limits (LL-1..LL-11) did not cap risk across positions. Correlation was a hand-written list, and nothing checked an order's price, size and shape independently before it left ([GAP_REVIEW.md](GAP_REVIEW.md) G1-G5).
**Decision:** A7 caps open risk to stops, beta-weighted exposure and swing gap stress, and refuses names that measurably move with an open position (PF-1..PF-4), using statistics computed each morning from bars before today. A8 checks the price band, volume share, order shape and an optional notional cap just before submitting (OC-1..OC-4).
**Consequences:** Some trades will be smaller, and a few correlated entries refused, by design. The checks use only data already loaded each morning.

