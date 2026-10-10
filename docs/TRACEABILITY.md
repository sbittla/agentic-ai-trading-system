# Traceability Matrix

**Version:** 1.0 (September 30, 2026) · Generated from [REQUIREMENTS.md](REQUIREMENTS.md) v1.3 and [SECURITY.md](SECURITY.md)

Every rule ID maps to the agent that enforces it, the test that proves it, and the reason code the journal records when it blocks or changes a trade. CI runs `tools/check_traceability.py`, which fails if any ID below without a "Manual" test has no test marked `@pytest.mark.rule("<ID>")`.

**Totals:** 188 requirement IDs (44 added in REQUIREMENTS 1.3, most of them planned).

| ID | Requirement | Enforced by | Test | Journal reason code |
| --- | --- | --- | --- | --- |
| `UR-1` | Trade only tickers on the config list. Any proposal for another ticker is rejected and… | A1 | unit/test_universe.py | NT_UNIVERSE |
| `UR-2` | One share class per company (GOOGL, never GOOG; they are treated as the same stock for… | A1 | unit/test_universe.py | NT_UNIVERSE |
| `UR-3` | Each ticker must be tradable on Alpaca and have at least 60 trading days of history. SPCX… | A1 | unit/test_universe.py | NT_UNIVERSE |
| `UR-4` | Every Sunday, A1 reports the current top 10 by market cap next to the owner's list and… | A1 | Manual: weekly report review | NT_UNIVERSE |
| `UR-5` | Store the list with its effective date so backtests use the list that was active at the… | A1 | unit/test_universe.py | NT_UNIVERSE |
| `TW-1` | Window times are config values, but no entry can be configured inside 9:30–9:35 or after… | A7, A10 | unit/test_windows.py | NT_WINDOW |
| `TW-2` | A morning trade that hits neither stop nor target is closed at 10:30 with a marketable… | A9, A10 | unit/test_windows.py | EXIT_TIME_STOP_1030 |
| `TW-3` | Swing trades enter only in the afternoon window, near the close, so the daily candle is… | A7, A10 | unit/test_windows.py | NT_WINDOW |
| `TW-4` | Half days (early 1:00 PM close): only the morning window runs. | A7, A10 | unit/test_windows.py | NT_WINDOW |
| `TW-5` | Scheduled high-impact events: on CPI and jobs-report days (8:30 AM releases) the morning… | A7, A10 | unit/test_windows.py | NT_WINDOW |
| `MF-1` | For long trades, QQQ is above its VWAP and not down more than 1% on the day. If QQQ is… | A4, A7 | unit/test_filters.py | NT_FILTER_MF1 |
| `MF-2` | For NVDA, AVGO and TSM, SMH must also be above VWAP. | A4, A7 | unit/test_filters.py | NT_FILTER_MF2 |
| `MF-3` | Relative volume (RVOL) at least 1.5 for the time of day, measured against the same IEX… | A4, A7 | unit/test_filters.py | NT_FILTER_MF3 |
| `MF-4` | Bid-ask spread under 0.05% of price at the moment of entry. | A4, A7 | unit/test_filters.py | NT_FILTER_MF4 |
| `MF-5` | No trading halt, no stale data, no limit-up/limit-down band in the last 10 minutes. | A4, A7 | unit/test_filters.py | NT_FILTER_MF5 |
| `CK-1` | Ticker is on the list and not locked (earnings, wash-sale, loss-streak, kill switch). | A7 | unit/test_checklist.py | NT_CK1 |
| `CK-2` | Inside an entry window. | A7 | unit/test_checklist.py | NT_CK2 |
| `CK-3` | Market, sector and stock filters pass (MF-1 to MF-5). | A7 | unit/test_checklist.py | NT_CK3 |
| `CK-4` | The setup is one of the named playbook setups and was on the game plan, or is flagged as… | A7 | unit/test_checklist.py | NT_CK4 |
| `CK-5` | Trigger has actually fired on a closed candle; no anticipation. | A7 | unit/test_checklist.py | NT_CK5 |
| `CK-6` | Entry price is no more than 0.25 ATR(5-min) above the trigger: never chase. | A7 | unit/test_checklist.py | NT_CK6 |
| `CK-7` | Stop is at a structural level and no wider than 1 ATR(5-min) for day trades. | A7 | unit/test_checklist.py | NT_CK7 |
| `CK-8` | The nearest resistance is at least 1.5R above entry, and the final target is at least 2R… | A7 | unit/test_checklist.py | NT_CK8 |
| `CK-9` | Review agent (A6) agrees. | A7 | unit/test_checklist.py | NT_CK9 |
| `CK-10` | Risk manager (A7) approves the size and every limit. | A7 | unit/test_checklist.py | NT_CK10 |
| `NT-1` | If no checklist passes in a window, the system places no order and logs "no qualifying… | A5, A7 | replay/test_no_trade_days.py | NT_NO_SETUP |
| `NT-2` | There is no minimum trade count. Zero-trade days are normal and are not a failure. | A5, A7 | replay/test_no_trade_days.py | NT_NO_SETUP |
| `EX-1` | Entries use limit orders at the trigger price plus a small offset (marketable limit),… | A8 | unit/test_execution.py | EXEC_* |
| `EX-2` | Every entry is a bracket order so the stop sits at Alpaca from the moment of fill. | A8 | unit/test_execution.py | EXEC_* |
| `EX-3` | Unfilled day-trade entries are cancelled after 3 minutes or when price moves 0.5… | A8 | unit/test_execution.py | EXEC_* |
| `EX-4` | To scale out, the position is split into two bracket orders: half with a target at 1R,… | A8 | unit/test_execution.py | EXEC_* |
| `XR-1` | Hard stop | A9 | unit/test_exits.py | EXIT_* |
| `XR-2` | First target | A9 | unit/test_exits.py | EXIT_* |
| `XR-3` | Trail | A9 | unit/test_exits.py | EXIT_* |
| `XR-4` | Failed-trade exit | A9 | unit/test_exits.py | EXIT_* |
| `XR-5` | Time stop | A9 | unit/test_exits.py | EXIT_* |
| `XR-6` | Event exit | A9 | unit/test_exits.py | EXIT_* |
| `LL-1` | Risk per trade | A7 | unit/test_loss_limits.py | HALT_LL1 |
| `LL-2` | Max trades per day | A7 | unit/test_loss_limits.py | HALT_LL2 |
| `LL-3` | Daily loss limit | A7 | unit/test_loss_limits.py | HALT_LL3 |
| `LL-4` | Two losses in a row | A7 | unit/test_loss_limits.py | HALT_LL4 |
| `LL-5` | Weekly loss limit | A7 | unit/test_loss_limits.py | HALT_LL5 |
| `LL-6` | Monthly drawdown limit | A7 | unit/test_loss_limits.py | HALT_LL6 |
| `LL-7` | Profit give-back | A7 | unit/test_loss_limits.py | HALT_LL7 |
| `LL-8` | Max open positions | A7 | unit/test_loss_limits.py | HALT_LL8 |
| `LL-9` | Max one position | A7 | unit/test_loss_limits.py | HALT_LL9 |
| `LL-10` | Correlated names (NVDA, AVGO, TSM) | A7 | unit/test_loss_limits.py | HALT_LL10 |
| `LL-11` | Margin | A7 | unit/test_loss_limits.py | HALT_LL11 |
| `DR-1` | No adding to a losing position, ever. | A7, A9 | unit/test_behaviour.py | NT_DR1 |
| `DR-2` | A stop can only move toward profit. | A7, A9 | unit/test_behaviour.py | NT_DR2 |
| `DR-3` | After a loss, the same ticker is locked for the rest of the window; after a stop-out, the… | A7, A9 | unit/test_behaviour.py | NT_DR3 |
| `DR-4` | Re-entering the same ticker in the same window is not allowed. | A7, A9 | unit/test_behaviour.py | NT_DR4 |
| `DR-5` | , and only after the first target is hit, with the stop on the whole position at… | A7, A9 | unit/test_behaviour.py | NT_DR5 |
| `DR-6` | A day trade can never be converted into a swing trade to avoid taking a loss. | A7, A9 | unit/test_behaviour.py | NT_DR6 |
| `DR-7` | Setups not in the morning game plan trade at half risk. | A7, A9 | unit/test_behaviour.py | NT_DR7 |
| `DR-8` | Any trade the owner forces through the kill switch or approval flow is tagged and… | A7, A9 | unit/test_behaviour.py | NT_DR8 |
| `RC-1` | When equity is 3% or more below its high-water mark, risk per trade halves to 0.25%. | A7 | unit/test_recovery.py | SIZE_REDUCED / SETUP_DISABLED |
| `RC-2` | Full size returns only after a new equity high, or 10 trades with positive expectancy at… | A7 | unit/test_recovery.py | SIZE_REDUCED / SETUP_DISABLED |
| `RC-3` | Any setup with negative expectancy over its last 20 trades is disabled for live trading… | A7 | unit/test_recovery.py | SIZE_REDUCED / SETUP_DISABLED |
| `RC-4` | A ticker with 3 losing trades in a row is locked for 5 trading days. | A7 | unit/test_recovery.py | SIZE_REDUCED / SETUP_DISABLED |
| `WS-1` | After a position in a ticker is closed at a loss, that ticker is locked for new entries… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-2` | Before any entry, A12 checks the last 30 days for realized losses in that ticker; if… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-3` | Before closing a position at a loss, A12 checks whether shares of that ticker were bought… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-4` | GOOGL and GOOG are treated as one ticker. Options on a locked ticker are also locked… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-5` | From December 1, no entry in any ticker with a realized loss in the prior 30 days, and by… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-6` | The system cannot see the owner's other accounts. The daily report lists locked tickers… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-7` | Every lot (date, size, cost, adjusted basis, disallowed loss) is stored and reconciled… | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-8` | In every policy, WS-5 (year-end) and WS-6 (IRA warning) always apply. | A12 | unit/test_wash_sale.py | NT_WASH_LOCK |
| `WS-9` | The owner should confirm the policy with a tax advisor. A trader who qualifies for trader… | A12 | Manual: tax advisor confirmation | NT_WASH_LOCK |
| `A1-1` | Load the owner's 10-ticker list from config; reject any ticker not on it (UR-1). | A1 | unit/test_a1_*.py | — |
| `A1-2` | Every Sunday at 6 PM, compare the list with the current top 10 by market cap (Finnhub,… | A1 | unit/test_a1_*.py | — |
| `A2-1` | Stream real-time 1-minute bars and quotes for the 10 tickers plus SPY, QQQ and SMH from… | A2 | unit/test_a2_*.py | — |
| `A2-2` | Build the 5- and 15-minute opening ranges and VWAP from 9:30. | A2 | unit/test_a2_*.py | — |
| `A2-3` | Keep at least 2 years of daily bars and 60 days of 1-minute bars locally. | A2 | unit/test_a2_*.py | — |
| `A2-4` | Mark data stale if no update for 60 seconds in a window; stale data blocks entries. | A2 | unit/test_a2_*.py | — |
| `A2-5` | Note that the free feed is IEX only; RVOL is always compared with the same feed's history. | A2 | unit/test_a2_*.py | — |
| `A3-1` | Pull company news (Finnhub), 8-K filings (SEC EDGAR), finance subreddit posts (Reddit),… | A3 | unit/test_a3_*.py | — |
| `A3-2` | Score each item with FinBERT; use Qwen only to summarize the top items per ticker in 3… | A3 | unit/test_a3_*.py | — |
| `A3-3` | Flag: earnings within 3 trading days, earnings today, new 8-K, analyst rating change,… | A3 | unit/test_a3_*.py | — |
| `A3-4` | Treat fetched text as untrusted data; strip links and instructions; never pass raw text… | A3 | unit/test_a3_*.py | — |
| `A4-1` | At 8:00 AM, compute key levels per ticker: prior day high, low and close; pre-market high… | A4 | unit/test_a4_*.py | — |
| `A4-2` | In the windows, compute VWAP, 9/20 EMAs, ATR(5-min), RVOL and the existing alert-bot… | A4 | unit/test_a4_*.py | — |
| `A4-3` | Detect only the playbook setups (sections 5.2 and 5.3), each with trigger, structural… | A4 | unit/test_a4_*.py | — |
| `A4-4` | Evaluate the market and stock filters (MF-1 to MF-5) and attach pass/fail to each setup. | A4 | unit/test_a4_*.py | — |
| `A5-1` | At 9:00 AM and 2:45 PM, write the game plan: tickers in play, the setup to watch,… | A5 | replay + eval set (a5) | — |
| `A5-2` | When A4 fires a trigger, return a TradeProposal in the JSON schema (section 10.1), using… | A5 | replay + eval set (a5) | — |
| `A5-3` | Recommend "no trade" whenever the setup is weak, and say which checklist item is weakest. | A5 | replay + eval set (a5) | — |
| `A5-4` | Proposal expires after 2 minutes for day trades and at 3:50 PM for swing trades. | A5 | replay + eval set (a5) | — |
| `A6-1` | Argue against each proposal with a separate prompt at temperature 0, looking for the… | A6 | replay + eval set (a6) | — |
| `A6-2` | Return agree or disagree, the main risk in one sentence, and any rule the proposal breaks. | A6 | replay + eval set (a6) | — |
| `A6-3` | A disagreement drops the trade in auto mode, or sends it to the owner in approve mode. | A6 | replay + eval set (a6) | — |
| `A7-1` | Run the full entry checklist (section 5.4) on live data; any failure rejects the trade… | A7 | unit/test_a7_*.py | — |
| `A7-2` | Size from the stop distance and the current risk-per-trade (0.5%, or 0.25% in drawdown);… | A7 | unit/test_a7_*.py | — |
| `A7-3` | Enforce every limit and behaviour rule in section 6; limits are read-only to all agents. | A7 | unit/test_a7_*.py | — |
| `A7-4` | Block entries outside the windows, on stale data, after the daily stop, or with the kill… | A7 | unit/test_a7_*.py | — |
| `A8-1` | Place entries as marketable limit bracket orders; split into two brackets for scaling out… | A8 | unit/test_a8_*.py | — |
| `A8-2` | Use a unique client order ID per proposal; never send the same proposal twice. | A8 | unit/test_a8_*.py | — |
| `A8-3` | Cancel unfilled entries after 3 minutes or when price runs 0.5 ATR(5-min) past the… | A8 | unit/test_a8_*.py | — |
| `A8-4` | Reconcile orders and positions with Alpaca every minute and after any reconnect. | A8 | unit/test_a8_*.py | — |
| `A9-1` | Apply the exit rules (section 5.6): first target, break-even stop, trail, failed-trade… | A9 | unit/test_a9_*.py | — |
| `A9-2` | Flatten morning day trades at 10:30 and all day trades by 3:55. | A9 | unit/test_a9_*.py | — |
| `A9-3` | Swing trades: trail under the 10-day EMA; close before earnings; exit after 10 trading… | A9 | unit/test_a9_*.py | — |
| `A9-4` | Never widen a stop or add to a loser (DR-1, DR-2). | A9 | unit/test_a9_*.py | — |
| `A10-1` | Open and close the trading windows on the NYSE calendar, including half days, holidays… | A10 | unit/test_a10_*.py | — |
| `A10-2` | Health-check Alpaca, the data stream and the Qwen container every 30 seconds; pause… | A10 | unit/test_a10_*.py | — |
| `A10-3` | Kill switch via WhatsApp or a local command: cancel open entries, block new ones, keep… | A10 | unit/test_a10_*.py | — |
| `A10-4` | Run modes per style: paper, approve-each-trade, auto. | A10 | unit/test_a10_*.py | — |
| `A11-1` | 10:30 AM morning summary; 4:30 PM daily review with each trade graded A/B/C against the… | A11 | replay + eval set (a11) | — |
| `A11-2` | Friday weekly review and month-end review with the performance standards (section 6.4). | A11 | replay + eval set (a11) | — |
| `A11-3` | Figures come from journal queries, never from the model. | A11 | replay + eval set (a11) | — |
| `A12-1` | Before every entry, check the ticker against wash-sale locks (WS-1, WS-2, WS-4, WS-5),… | A12 | unit/test_a12_*.py | — |
| `A12-2` | Before every loss exit, run the look-forward check (WS-3) and record the expected… | A12 | unit/test_a12_*.py | — |
| `A12-3` | Maintain tax lots and adjusted basis (WS-7); produce the monthly wash-sale report and the… | A12 | unit/test_a12_*.py | — |
| `A12-4` | Publish the list of locked tickers and unlock dates every morning in the game plan. | A12 | unit/test_a12_*.py | — |
| `QW-1` | Run Qwen with the official Ollama Docker image, model version pinned in config (Qwen3 8B… | llm client | Manual: compose file review | — |
| `QW-2` | Request JSON output by passing the Pydantic schema in Ollama's `format` field, repeat the… | llm client | contract/test_llm_client.py | — |
| `QW-3` | 60-second timeout per call; one retry; then skip the trade (never trade without a review). | llm client | contract/test_llm_client.py | — |
| `QW-4` | Load the model by 9:00 AM and keep it loaded through 4:30 PM; expose a health check. | llm client | contract/test_llm_client.py | — |
| `QW-5` | Log every prompt, response, latency and token count to the journal. | llm client | contract/test_llm_client.py | — |
| `QW-6` | The owner chooses the model from the machine's capacity: `trader select-model` detects… | hardware, cli | unit/test_hardware.py | — |
| `SR-1` | Alpaca retired the pattern day trader rule on June 4, 2026; margin accounts with $2,000+… | A7, A8, A10 | unit/test_safety.py | — |
| `SR-2` | Never use margin beyond 1x equity. | A7, A8, A10 | unit/test_safety.py | — |
| `SR-3` | Separate API keys for paper and live, stored outside the code and never sent to Qwen. | A7, A8, A10 | unit/test_safety.py | — |
| `SR-4` | Every plan, signal, proposal, review, decision, order, fill, exit, lock and override is… | A7, A8, A10 | unit/test_safety.py | — |
| `SR-5` | All settings (ticker list, windows, limits, wash-sale policy, modes, model version) live… | A7, A8, A10 | unit/test_safety.py | — |
| `SR-6` | Everything runs in containers; only trader-core holds broker keys and can place orders; no inbound internet connections. | A10, compose | chaos/test_containers.py | — |
| `SEC-1` | allow-list of owner phone numbers; messages from other numbers are ignored and logged | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-2` | every approval request carries a 4-character one-time code (`YES K7Q2`); codes expire… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-3` | two-step confirmation: the system replies with a code that must be sent back within 60… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-4` | text is cleaned and fenced as DATA; Qwen output is validated by code; Qwen has no path to… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-5` | size and limits are computed only by A7 from config; proposal fields that are not in the… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-6` | keys only in `.env` / Windows Credential Manager; `.env` git-ignored; secret scanning in… | A10, notify | CI: gitleaks + unit/test_security.py | CMD_REJECTED |
| `SEC-7` | live keys loaded only when `environment: live`; separate `.env.live` file readable only… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-8` | config is in git; A10 logs the config hash daily; loosening needs the CF-3 paper period;… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-9` | Ollama is on the internal Docker network only, no host port published | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-10` | pinned versions with hashes (`pip-compile --generate-hashes`); Dependabot alerts; no… | A10, notify | CI: pinned hashes + Dependabot | CMD_REJECTED |
| `SEC-11` | BitLocker on the data drive; Windows account with a strong password; backups encrypted | A10, notify | Manual: monthly security check | CMD_REJECTED |
| `SEC-12` | order-rate guard in A8: at most 10 order submissions per minute and 30 per day, then… | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-13` | Alpaca account 2FA; daily reconcile flags any order not placed by the system | A10, notify | Manual: Alpaca 2FA enabled | CMD_REJECTED |
| `SEC-14` | Every command received (sender, text, time, accepted or rejected, reason) is written to the journal. | A10, notify | unit/test_security.py | CMD_REJECTED |
| `SEC-15` | The daily report lists all commands received and any rejected senders. | A11 | unit/test_a11_*.py | — |
| `SEC-17` | One Postgres role per container; trigger limits outbox channels per role; news and notifier hold no Alpaca keys. | postgres, A10 | chaos/test_containers.py | CMD_REJECTED |
| `SEC-18` | Each container reads only its own env file; live keys in a separate file. | compose | Manual: env file review at go-live | — |
| `SEC-19` | No inbound ports or tunnels; notifier polls Twilio; containers read-only with no-new-privileges. | compose, notifier | chaos/test_containers.py | — |
| `SEC-16` | Monthly security check: dependency alerts, key ages, config hash history, rejected commands. | Owner | Manual: monthly security check | — |


### Added in REQUIREMENTS 1.3 (day-trader review)

Rows marked **Planned** are specified but not built yet; CI counts them but does not enforce a test until the row names one.

| ID | Requirement | Enforced by | Test | Journal reason code |
| --- | --- | --- | --- | --- |
| `UN-1` | In-play scan (watch only): from 8:30 to 9:25 AM, scan the 50 largest S&P 500 names for ga… | A4 | Planned: unit/test_universe.py | — |
| `UN-2` | Governed rotation: A1's weekly report may propose swapping a list ticker for a name that… | A1 | Manual: weekly report review | NT_UNIVERSE |
| `TW-6` | 10:00 AM releases and Fed speakers: The macro calendar also lists the 10:00 AM releas… | A7, A10 | Planned: unit/test_windows.py | NT_MACRO_RELEASE |
| `MF-6` | Reliable quote: the spread check (MF-4) uses only a two-sided quote less than 2 seconds o… | A4, A7 | Planned: unit/test_checklist.py | NT_NO_RELIABLE_QUOTE |
| `RG-1` | Daily regime: at 8:30 AM, classify the day as `normal` or `high-vol` from SPY's ATR(14-da… | A4 | Planned: unit/test_regime.py | — |
| `RG-2` | High-vol rules: risk per trade halves (like RC-1, not stacked below 0.25%); no gap-and-go… | A7 | Planned: unit/test_regime.py | NT_REGIME |
| `RG-3` | Trend day, defined: by 3:00 PM, at least 80% of 5-minute closes since 9:30 are above VWAP… | A4 | Planned: unit/test_regime.py | NT_NOT_TREND_DAY |
| `EX-5` | Stop order type: Broker-side stops are stop-market orders, so a fast move or a halt c… | A8, A9 | Planned: unit/test_execution.py | EXIT_GAP_BELOW_STOP |
| `LAT-1` | Latency budget: from the trigger candle's close to the order sent, p95 ≤ 10 s in auto… | A10, engine | Planned: unit/test_latency.py | — |
| `LAT-2` | Pre-arming: When price is within 0.3 ATR(5-min) of a game-plan trigger, A5 writes the… | A4, A5, A6 | Planned: unit/test_latency.py | — |
| `LAT-3` | No-chase at send time: CK-6 is checked again the moment the order is sent (after approval… | A7, A8 | Planned: unit/test_latency.py | NT_CHASE_AT_SEND |
| `LAT-4` | Aligned expiries: A day-trade proposal expires 2 minutes after the trigger, or 30 s a… | A5, A7 | Planned: unit/test_latency.py | NT_EXPIRED |
| `SH-1` | Shadow trades: Every setup whose trigger fires is followed with a simulated bracket o… | engine | Planned: unit/test_shadow.py | — |
| `SH-2` | Shadow report: the weekly review compares the expectancy of taken and not-taken setups, b… | A11 | Planned: unit/test_shadow.py | — |
| `AC-1` | Cash account: Size every entry from settled cash only. Track unsettled proceeds per t… | A7 | Planned: unit/test_loss_limits.py | NT_UNSETTLED_FUNDS |
| `AC-2` | Margin account: buying power is used only up to 1× equity (SR-2, LL-11). The account type… | A7, A10 | Planned: unit/test_safety.py | NT_ACCOUNT_MISMATCH |
| `EXQ-1` | Slippage feedback: Expectancy per setup is always computed after actual slippage (ent… | A7, A11 | Planned: unit/test_execution.py | — |
| `WS-9` | Other-account import: The owner can import trades from other brokers (CSV exports fro… | A12 | Planned: unit/test_wash_sale.py | NT_WASH_LOCK |
| `DQ-6` | IEX vs consolidated: after each session, compare the day's IEX 1-minute bars with consoli… | A2, A11 | Planned: unit/test_a2_market_data.py | — |
| `DQ-7` | Go-live data gate: live trading needs either 20 paper days in which the opening-range and… | A10 | Manual: go-live data gate review | — |
| `DQ-8` | Unreliable quote: when the latest quote is one-sided, crossed or more than 2 seconds old,… | A2, A7 | Planned: unit/test_checklist.py | NT_NO_RELIABLE_QUOTE |
| `NW-1` | News in positions: during the windows, poll news and new 8-Ks every 60 seconds for ticker… | A3 | Planned: unit/test_a3_news.py | — |
| `NW-2` | Shock rule: A move of more than 1.5 ATR(5-min) against an open position within one mi… | A3, A9 | Planned: unit/test_exits.py | NT_NEWS_SHOCK |
| `A5-5` | Pre-armed proposals (LAT-2) use the same schema and prompt; the journal records whether a… | A5 | Planned: unit/test_strategy_review.py | — |
| `A9-5` | In co-pilot mode (CP-2), apply the same exit rules as alerts instead of orders. | A9 | Planned: unit/test_exits.py | — |
| `NF-1` | Notifier down: if the notifier channel is unreachable for 2 minutes during a window, appr… | A10, notifier | Planned: unit/test_a10_supervisor.py | NT_NOTIFIER_DOWN |
| `TX-1` | Tax set-aside: the monthly report shows realized short-term gains year to date and an est… | A11 | Planned: unit/test_a11_reporter.py | — |
| `AL-1` | One source while trading: When trader-core runs, it produces the price and signal ale… | engine, notifier | Planned: unit/test_alerts.py | — |
| `AL-2` | Labels: every signal alert is labelled raw signal or qualified setup, with the checklist… | A4, A7 | Planned: unit/test_alerts.py | — |
| `AL-3` | Actionable: every directional alert carries the plan, computed by the same A4/A7 code as… | A4, A7 | Planned: unit/test_alerts.py | — |
| `AL-4` | Schedule: `windows` (default: pre-market, 9:30–10:30, 3:00–4:00) or `all_day`. | notifier | Planned: unit/test_alerts.py | — |
| `AL-5` | Caps: at most 3 alerts per symbol per window, and at most 12 per window overall. | notifier | Planned: unit/test_alerts.py | — |
| `AL-6` | Ranking: alerts are ranked by a quality score (RVOL, alignment with QQQ, distance to resi… | notifier | Planned: unit/test_alerts.py | — |
| `AL-7` | Grading: every alert type is graded: the share that reached +1R before −1R within 30 minu… | A11 | Planned: unit/test_alerts.py | — |
| `AP-1` | Chart in the approval: the approval message includes a small chart image rendered by code… | notifier | Planned: unit/test_telegram.py | — |
| `AP-2` | Buttons: Inline `YES <code>` / `NO <code>` buttons carry the one-time code. Callback… | notifier, commands | Planned: unit/test_telegram.py | CMD_REJECTED |
| `AP-3` | Live status: the message is edited every 5 seconds with | notifier | Planned: unit/test_telegram.py | — |
| `CP-1` | Plan only: in `copilot` mode the system sends the full plan (AL-3) at the trigger and pla… | A10 | Planned: unit/test_copilot.py | — |
| `CP-2` | Owner fills tracked: The owner's fills are taken from the Alpaca account (positions a… | A9, A12 | Planned: unit/test_copilot.py | — |
| `CP-3` | Options: the same applies to options advisories (OPTIONS_REQUIREMENTS.md, A16). | A16 | Planned: unit/test_copilot.py | — |
| `OPS-1` | Restore test: monthly, restore the latest backup into a throwaway database and check the… | Owner, backup | Manual: monthly restore test | — |
| `OPS-2` | Emergency drill: monthly, on the paper account, with one position open | Owner | Manual: monthly emergency drill | — |
| `BT-6` | Real-history backtest of the playbook on 2 years of minute bars before paper trading (TEST_PLAN.md). | backtest | Planned: replay/test_backtest_history.py | — |
| `PT-4` | Paper fills re-checked against the replay fill model; paper statistics use the stricter result (TEST_PLAN.md). | backtest, A11 | Planned: replay/test_paper_fills.py | — |

## How to use

- Mark each test: `@pytest.mark.rule("CK-6")`. One test can cover several IDs; every ID needs at least one test.
- When a requirement is added or changed, regenerate this table and add or update the test in the same commit.
- Reason codes are the values written to `decisions.reason_code` and `positions.exit_reason`; reports group by them.
