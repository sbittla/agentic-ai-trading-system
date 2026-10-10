# Options Suggestions and Alerts: Requirements

**Version:** 0.1 draft (October 10, 2026) · Status: **for owner review, nothing built yet** · Proposed decision: ADR-015 ([DECISIONS.md](DECISIONS.md))

The system already produces directional views on its stocks:

- the A4 setups (entries with a trigger, stop and T1)
- the next-day ideas (picks per window)
- the alerts engine's signals, both bullish and bearish:
  - bullish: ORB breakout, VWAP reclaim, new high of day, up moves
  - bearish: ORB breakdown, VWAP loss, new low of day, down moves

This document adds an **options layer** on top of those views. When the system has a long (bullish) or short (bearish) view on a stock, it suggests a defined-risk options trade sized to the same risk rules, built from the live option chain at the stock's current price. The suggestion goes out on Telegram (and later the console) and is graded afterwards.

**Scope rule:** options start **advisory only** (suggest and track). Placing option orders is a later, gated phase (§9), like the go-live gates for stocks. A bearish view is expressed by **buying puts or put spreads, or selling call spreads, never by shorting stock**, so ADR-008 (no short selling) still holds.

Requirement IDs are `OP-n` (options), `BR-n` (bearish signals) and `A13-n` to `A16-n` (new agents). They go into [TRACEABILITY.md](TRACEABILITY.md) when they are built, each with its tests.

---

## 1. Where the views come from (inputs)

| Source | Today | Direction | Used for options |
| --- | --- | --- | --- |
| A4 setups (morning and afternoon) | Built; long only | bullish | yes: trigger, stop and T1 give the strike and expiry targets |
| Next-day ideas (A5 preview, 4:15 PM) | Built; long picks | bullish | yes: next-session options ideas |
| Alerts engine signals (`trader alerts`) | Built; bull and bear | both | yes: ORB, VWAP, HOD/LOD and moves with a level to anchor on |
| **Bearish setups in A4** | **Not built** | bearish | **new (§2)**: needed for put ideas from the main system |
| Owner's own view (Telegram `OPT NVDA bear`) | — | either | yes, on demand |

## 2. Bearish ("short") signals (new)

The stock side stays long-only. These signals exist **only to feed bearish options suggestions**, and code enforces it: A7 and A8 reject any stock order that comes from a bearish signal.

- **BR-1 Mirrored setups** in A4, with the same definitions as the long setups ([STRATEGY_SPEC.md](STRATEGY_SPEC.md)) turned upside down. Each has a trigger, an invalidation level (the "stop") and T1/T2 targets, measured in ATR(5m), as for longs:
  - **ORB breakdown:** a 5-minute close below the opening-range low.
  - **VWAP rejection:** a failed reclaim, then a lower high under VWAP.
  - **Gap-down-and-go:** a gap down of 2% or more on news, holding below the pre-market low.
  - **Failed breakout:** a new high of day reversed below the opening-range high within 15 minutes.
  - **Close near low (swing):** the stock closes in the bottom 10% of its range, below VWAP and the 10-day EMA.
- **BR-2 Mirrored market filter:**
  - bearish ideas need QQQ below VWAP, or the stock to be weaker than QQQ and SMH (relative weakness)
  - the same RVOL threshold as longs (1.5)
  - no bearish ideas in the first 5 minutes or during halts
- **BR-3 Next-day ideas** also rank bearish candidates. The 4:15 PM message gets a "Bearish (options only)" section with trigger, invalidation and T1.
- **BR-4** Each bearish signal is journaled like a long setup, with `direction = bear` and `route = options_only`.

## 3. Option data (new agent A13)

**A13 Options Data agent (code).** It runs inside trader-core, with no new container (ADR-010).

- **A13-1 Chains:**
  - For each universe ticker plus SPY and QQQ, fetch listed expirations and contracts from Alpaca's options API, once at 8:30 AM and on demand.
  - Keep expirations from 0 to 60 days to expiry (DTE) and strikes within ±20% of the stock price.
- **A13-2 Snapshots:**
  - Take bid, ask, last, volume, open interest, implied volatility (IV) and greeks (delta, gamma, theta, vega) for the contracts near a signal.
  - Fetch them only when a signal or command needs them, cached for 15 s. The chain is not streamed: it would exceed the free plan and waste resources.
- **A13-3 Volatility context**, per ticker:
  - ATM IV for 30 days to expiry
  - **IV rank and IV percentile** from a daily ATM-IV history stored in the journal. Until 252 sessions exist, it is labelled "short history: N days".
  - expected move to each expiration, from the ATM straddle price
  - IV term structure (front vs 30-day)
  - skew: 25-delta put IV minus 25-delta call IV
- **A13-4 Events:**
  - earnings date (Finnhub, as A3 already does)
  - ex-dividend date
  - FOMC and CPI days (macro calendar)
  - each event attached to every expiration it falls inside
- **A13-5 Data quality:**
  - Every quote shows its age. Quotes older than 60 s, or a bid of zero, make the contract unusable.
  - The free Alpaca plan gives an **indicative** options feed; real-time OPRA quotes need a paid plan.
  - Suggestions say "prices indicative" while on the free feed.
  - Mid prices are estimates; limit orders at or near the mid are assumed, never market orders.
- **A13-6 Storage:**
  - `options_iv_daily(ticker, date, atm_iv_30d, iv_rank, iv_pct, expected_move_30d)`
  - `option_quotes`: only the contracts that appear in a suggestion, at suggestion time and at grading time

## 4. Building the suggestion (new agent A14)

**A14 Options Strategist (code picks, Qwen only explains).** Inputs: a view (direction, trigger price, invalidation level, T1/T2, style day/swing, signal score) and the A13 context. Output: zero to two `OptionSuggestion`s. **No suggestion is a valid answer.**

- **OP-1 Defined risk only.** Allowed structures:

| View | IV rank < 30 (options cheap) | IV rank 30–60 | IV rank > 60 (options expensive) |
| --- | --- | --- | --- |
| Bullish | long call, or call debit spread | call debit spread | put credit spread (short put below the invalidation level) |
| Bearish | long put, or put debit spread | put debit spread | call credit spread (short call above the invalidation level) |

  - Never allowed: naked short calls or puts, short straddles or strangles, ratio spreads, anything with undefined loss.
  - Covered calls and cash-secured puts are a later, separate topic (§11).
- **OP-2 Expiration by style:**
  - **day setups:** 5–14 DTE, the nearest weekly expiry ≥ 5 DTE. 0–1 DTE is off by default and needs `options.allow_0dte: true`.
  - **swing setups and next-day ideas:** 21–45 DTE
  - Never an expiration that contains an earnings date, unless the owner asks (`OPT NVDA earnings`). Long premium through earnings is exposed to the post-earnings IV drop ("crush").
- **OP-3 Strikes are tied to the stock's levels:**
  - **Long call or put:** delta 0.55–0.70, slightly in the money, so it moves with the stock and loses less to time decay.
  - **Debit spread:**
    - long leg at delta about 0.60
    - **short leg at or just beyond T1** (or T2 for swing): the spread pays most when the stock reaches the system's own target
  - **Credit spread:** short strike **beyond the invalidation level** with delta ≤ 0.30; width set by the risk budget (OP-5).
  - Strikes are rounded to listed strikes. A leg that fails liquidity (OP-4) moves to the nearest strike that passes, or the suggestion is dropped.
- **OP-4 Liquidity filter**, per leg:
  - open interest ≥ 500
  - today's volume ≥ 100 (≥ 20 before 10:00 ET)
  - bid-ask spread ≤ 5% of mid, or ≤ $0.05 when mid < $1
  - both bid and ask present
  - Spread fills are estimated at mid ± 25% of the combined bid-ask width.
- **OP-5 Size from the same risk rules as stocks** (REQUIREMENTS §6, config `risk`):
  - max loss = premium paid (long options and debit spreads), or width − credit (credit spreads), × 100 per contract
  - contracts = floor(`risk_per_trade_pct` × equity ÷ max loss per contract); 0 means no suggestion
  - also capped at `options.max_premium_pct` of equity (default 1%) and `options.max_contracts` (default 10)
  - Drawdown mode (RC-1) halves it, as for stocks.
- **OP-6 Exits planned up front**, stated in the suggestion:
  - **Invalidation:** the stock trades through the invalidation level (the stock idea's stop) → exit.
  - **Profit:** the stock reaches T1 → take 50% (single options) or close (spreads at ≥ 70% of max value).
  - **Time:** day suggestions are closed by 15:45 ET on the same day unless re-qualified. Swing suggestions are closed at 21 DTE or after 10 sessions.
  - **Credit spreads:** close at 50% of max profit, or if the short strike is breached.
  - **Expiry day:** never held into the last hour of expiration day (pin and assignment risk).
- **OP-7 Numbers shown, all computed by code** (A11-3 rule: no number from the model):
  - net debit or credit (mid, and the estimated fill)
  - max loss and max gain in $ per contract and in total
  - breakeven at expiry
  - P/L at T1 and at the invalidation level (Black-Scholes repricing at the current IV, at the planned exit time)
  - probability of profit, approximated by delta and labelled an estimate
  - **expected move vs target:** T1 inside or beyond the 1× expected move to expiry
  - greeks of the position
  - IV rank
  - liquidity (OI, volume, spread %)
  - events inside the expiry
- **OP-8 Qwen's role:** after the close only (next-day ideas), Qwen may add one or two plain sentences on *why this structure fits this setup*, from the computed fields. During market hours the rationale is a code template, so suggestions add no GPU load (UI-C5, QW-3).
- **OP-9 Gate before sending.** A suggestion is dropped if any of these is true:
  - the view's own checklist failed
  - risk/reward at T1 < 1.5
  - T1 beyond 1.5× the expected move to expiry
  - liquidity failed
  - quotes are stale
  - A15 compliance blocked it

## 5. Options risk and compliance (new agent A15)

**A15 Options Risk & Compliance (code).** A12 and A7 extended to options.

- **A15-1 Wash sales cross both ways:**
  - Options on a locked ticker are locked (WS-4 already says so).
  - A loss on an option locks the stock, and a loss on the stock locks its options, for 31 days.
  - Puts are included: buying a put after a stock loss is not a wash sale on its own, but the strict policy treats every contract on the ticker as identical. Confirm with a tax advisor (open question 4).
- **A15-2 Shared limits:**
  - Options suggestions count toward the daily loss limit, weekly and monthly drawdown limits, and the max-trades-per-day budget.
  - Each counts as one trade, with its own window caps, exactly like stocks.
  - Within one window there is never a stock entry and an options suggestion on the same ticker in the same direction.
- **A15-3 Event rules:**
  - no long premium through earnings (OP-2)
  - no credit spread with a short call through an ex-dividend date: early assignment risk
  - no new suggestions in the last 15 minutes before 16:00
  - FOMC days: day suggestions only before 13:30 ET
- **A15-4 Account permission:**
  - Suggestions respect the options level the owner's Alpaca account is approved for: long options need level 2, spreads need level 3.
  - Set in config (`options.account_level`); a structure above the level is never suggested.
- **A15-5 Correlated groups:** the semis rule (NVDA, AVGO, TSM: max 1 open) covers options too.

## 6. Alerts and messages

- **OP-10 When a suggestion fires:**
  1. When an A4 setup (bull or bear) triggers in a trading window.
  2. When an alerts-engine signal fires on a universe ticker, if `options.from_alerts: true`. These signals have no full checklist, so they are marked "signal-only, lower conviction" and sized at 50% (like unplanned stock trades, DR-7).
  3. In the 4:15 PM next-day ideas: a swing structure per pick and per bearish candidate.
  4. On request: `OPT <TICKER> [bull|bear] [day|swing]`.
- **OP-11 Telegram format** (one message per suggestion, plain text). Example (illustrative numbers):

  ```
  📉 OPTIONS IDEA · NVDA bearish (ORB breakdown, score 74) · PAPER/ADVISORY
  Stock 132.40 · invalidation 134.10 · T1 128.90 · expected move to Oct 24: ±6.10
  Put debit spread  Oct 24 (14 DTE)  130P / 125P
    debit ~1.85 (bid/ask 1.70/2.00) · max loss $185 · max gain $315 · breakeven 128.15
    at T1: ~+$140 · at invalidation: ~-$95   (per spread)
    size 2 spreads = $370 risk (0.37% of equity)
  IV rank 42 · long Δ-0.58 / short Δ-0.31 · OI 12.4k / 9.1k · spread 3.1%
  Exit: stock > 134.10, or at T1, or by 15:45 today. No earnings before expiry.
  Prices indicative (free feed). Not advice.
  ```
- **OP-12 Follow-up alerts** on every open suggestion, tracked even in advisory mode:
  - underlying hit T1 or the invalidation level
  - the position reached 50% or 70% of max profit, or −50% of premium
  - the time exit is due
  - IV fell 20% or more (crush)
  - an event is now inside the expiry
  - 3 DTE left
  - Each alert says what the plan says to do.
- **OP-13 Telegram commands:**
  - `OPT <T> [bull|bear] [day|swing]`: suggest now
  - `CHAIN <T> [expiry]`: compact chain around the money with IV and delta
  - `IV <T>`: IV rank, expected move, term and skew
  - `OPTS`: open suggestions and their live P/L
  - `OPTIONS ON|OFF`
  - They follow the SEC-1 allow-list; none places an order.
- **OP-14 Lightweight alerts bot** (`trader alerts`). An optional `options:` setting (default off) adds one options line to ORB, VWAP and HOD/LOD alerts on watchlist symbols:
  - the suggested structure, debit and max loss, using one REST snapshot call per alert, rate-limited to 1 call every 10 s
  - no sizing beyond a configured fixed risk in $
  - The full logic stays in trader-core; the bot reuses A14's pure functions so both give the same answer.
- **OP-15 Rate limits and quiet rules:**
  - at most one suggestion per ticker per direction per window
  - at most 6 options alerts per window overall
  - follow-ups are grouped into one message per minute
  - `/mute` silences options too

## 7. Grading and track record (new agent A16)

**A16 Options Monitor & Grader (code).**

- **A16-1 Every suggestion is paper-tracked:**
  - entry at the estimated fill (OP-4) at suggestion time
  - marks every minute while the underlying is in a window, then at the close
  - exits applied by the plan's rules (OP-6) on the first condition met
- **A16-2 Result per suggestion:** P/L in $ and in R (R = max loss), exit reason, holding time, IV change, and whether the stock view was right compared with whether the option made money. That shows when the structure, not the view, lost.
- **A16-3 Reports:**
  - Daily and weekly reports (A11) get an options section: suggestions, win rate, expectancy in R, broken down by structure, by bull/bear, by IV regime and by DTE bucket.
  - A new Grafana dashboard, "7. Options", shows the same.
- **A16-4 Promotion gate:** a structure/direction pair can move from advisory to paper execution (§9) only after ≥ 40 graded suggestions with expectancy ≥ +0.15R and profit factor ≥ 1.3, over at least 20 sessions. This is the same idea as the stock go-live gates in [TEST_PLAN.md](TEST_PLAN.md).

## 8. Owner console (adds to [UI_REQUIREMENTS.md](UI_REQUIREMENTS.md))

- **OP-16 Options tab on the stock page (UI-4):**
  - IV rank and percentile, expected move per expiry, term structure and skew
  - a compact chain around the money with bid/ask, mid, IV, delta, OI and volume, colour-coded by liquidity
  - "Suggest bull / bear" buttons that call A14
- **OP-17 Payoff view:** for any suggestion, the profit/loss at expiry and at the planned exit time over a range of stock prices, with markers for the current price, T1, the invalidation level and the expected-move band.
- **OP-18 Suggestions list:** open and closed suggestions with live P/L, follow-up alerts, and their grade.
- **OP-19 Ask (UI-7)** gains read-only options tools: `iv(symbol)`, `chain(symbol, expiry)`, `suggestions(range)`, `options_metrics(group_by)`. Example: "How did put debit spreads do on high-IV days?"
- **OP-20 Prefetch (REQUIREMENTS LAT-2):** when a setup is pre-armed, A13 fetches the chain snapshot near the expected strikes, so the options suggestion goes out with the stock alert, not after it.

## 9. Phases and modes

`options.mode`:

| Mode | What happens | Gate to enter |
| --- | --- | --- |
| `off` | nothing | — |
| `advisory` (**first build**) | suggestions, follow-ups and grading; no orders | none |
| `paper` | A8 places the suggested option orders in the Alpaca **paper** account. Multi-leg limit orders at mid, after owner approval (`YES <code>`), with the same approval rules as stocks | A16-4 met for that structure/direction; owner sets it in config |
| `live` | same, in the live account | stock live gates passed + ≥ 2 months of options paper results meeting A16-4 + owner decision recorded (`trader gate --record options_live`) |

When orders are enabled:

- A8 places only limit orders.
- Every multi-leg order is sent as one order, never leg by leg.
- A9 manages exits by the OP-6 rules.
- Nothing is held into the last hour of expiration day.
- Exercise and assignment events are reconciled into the journal and tax lots.

## 10. Configuration (new `options:` section)

```yaml
options:
  mode: advisory              # off | advisory | paper | live (paper/live need their gates)
  account_level: 3            # Alpaca options level: 2 = long options, 3 = spreads
  from_alerts: false          # also suggest on alerts-engine signals (marked lower conviction)
  structures: [long_call, long_put, call_debit, put_debit, put_credit, call_credit]
  allow_0dte: false
  day_dte: [5, 14]
  swing_dte: [21, 45]
  min_open_interest: 500
  min_volume: 100
  max_spread_pct: 5.0
  iv_rank_cheap: 30
  iv_rank_rich: 60
  max_premium_pct: 1.0        # of equity, per suggestion
  max_contracts: 10
  avoid_earnings: true
  max_alerts_per_window: 6
```

Validation (as for every config section):

- a mode above `advisory` without its gate refuses to start
- structures above `account_level` are rejected
- DTE ranges must be ordered

## 11. Out of scope (for now)

- covered calls and cash-secured puts (income strategies on held shares)
- iron condors and butterflies
- calendars and diagonals
- rolling positions
- index options (SPX/XSP)
- any naked short option
- automatic trading of 0DTE

Any of these can be added later as new rows in the OP-1 table, with their own grading.

## 12. Agent roster after this change

| # | Agent | Type | Runs (ET) | Output |
| --- | --- | --- | --- | --- |
| A4 | Technical analysis (+ BR-1 bearish setups) | Code | as today | Long setups; bearish setups for options only |
| **A13** | Options data | Code | 8:30 AM chain load; on demand | Chain, quotes, greeks, IV rank, expected move, events |
| **A14** | Options strategist | Code (+ Qwen after close) | On each bull/bear trigger, 4:15 PM, on request | 0–2 defined-risk suggestions with sizing and exits |
| **A15** | Options risk & compliance | Code | Before every suggestion | Block or pass (wash sale, events, limits, account level) |
| **A16** | Options monitor & grader | Code | Every minute in windows; at the close | Follow-up alerts, paper P/L, track record, promotion gate |

All four live in trader-core: no new container, and the GPU is used only after the close (OP-8).

## 13. Open questions for the owner

1. **Options approval level** on your Alpaca account: 2 (long calls and puts) or 3 (spreads)? This decides which OP-1 structures exist.
2. **Paid OPRA data?** The free indicative feed is fine for advisory suggestions; real executions need real quotes.
3. **Day options at 5–14 DTE, or allow 0–1 DTE?** Short-dated options are cheaper but very sensitive to the stock's moves and to time decay. The proposal keeps them off.
4. **Wash sales:** treat every contract on a ticker as identical to the stock (strict, the proposal), or only calls (narrower)? Confirm with a tax advisor.
5. **Send options ideas from the lightweight alerts bot too** (OP-14), or only from trader-core?
6. **Risk budget:** should options share the stocks' 0.5% per trade and daily 1.5% limit (the proposal), or have their own smaller budget?
