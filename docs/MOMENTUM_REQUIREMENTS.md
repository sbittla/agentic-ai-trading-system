# Momentum Trading Requirements (MO)

**Version:** 1.0 (October 17, 2026) · Status: **built, advisory** · Companion to [SCANNER_REQUIREMENTS.md](SCANNER_REQUIREMENTS.md), [STRATEGY_SPEC.md](STRATEGY_SPEC.md) and [GAP_REVIEW.md](GAP_REVIEW.md)

The momentum module (agent A19) finds the market's leading stocks and runs two things. The first is a **rotation model**, a portfolio of the 10 strongest stocks rebalanced every second week, with buy and sell alerts. The second is **swing setups**: buy-stop alerts with a trigger, a stop and a size, followed to the exit with sell alerts for a stop hit, a partial profit, a raised stop, a broken trend, a climax and a time stop. Every rule is a published technique. Each one is graded walk-forward on ten years of consolidated (SIP) daily bars before its alerts are sent.

```bash
./run.sh momentum-history     # once: 10 years of SIP daily bars for momentum.universe
./run.sh momentum-eval        # grade every setup x exit policy, the alert stream and the ranking model
./run.sh momentum             # leaderboard, armed buy-stops and open signals (add --telegram)
```

trader-core runs the close job at 16:50 ET (retrying until the day's bar is final), checks triggers, stops and +2R targets every 5 minutes from 9:36 to 15:50 ET, and re-grades everything on Sundays at 19:00 ET. In Telegram or the Owner Console, `MOMENTUM` shows the same view. The console page is **Momentum**.

**What this is not.** No rule set guarantees profits, and "maximum profit" is not something a backtest can promise. Momentum is the best-documented anomaly in equities: two centuries of data across countries and asset classes. It also has the worst crashes. These are sudden reversals after bear markets: −73% for US winners-minus-losers in 1932, −45% in 2009. The design therefore puts the measured edge first, the crash guard second and the size third. It reports what the history actually shows, including where a technique does not work (§9).

## 1. Research basis

| Technique | Source | What is implemented |
| --- | --- | --- |
| Cross-sectional momentum (12-1 month) | Jegadeesh & Titman (1993); Asness, Moskowitz & Pedersen, *Value and Momentum Everywhere* (2013) | `r12_1` factor and the ranking model (MO-13) |
| Momentum crashes | Daniel & Moskowitz, *Momentum Crashes* (2016) | Crash guard: half size when the market rebounds from a bear market (MO-6) |
| Risk-managed momentum | Barroso & Santa-Clara (2015) | Inverse-volatility weights; ATR risk sizing (MO-13, MO-17) |
| 52-week-high momentum | George & Hwang (2004) | `high52` factor (close ÷ 52-week high); the `HIGH_52W` setup |
| Residual momentum | Blitz, Huij & Martens (2011) | `resid` factor: 12-1 return net of SPY beta, scaled by residual volatility |
| Frog in the pan | Da, Gurun & Warachka (2014) | `fip` information discreteness (smooth trends beat jumpy ones), shown in the context |
| Exponential-regression momentum | Clenow, *Stocks on the Move* (2015) | `clenow` factor (annualised 90-day log-slope × R²), index filter, gap filter, biweekly rebalance |
| Absolute momentum and trend filter | Antonacci, *Dual Momentum* (2014); Faber (2007) | Regime filter: no new entries while SPY is under its 200-day average (MO-6) |
| Relative-strength rating | O'Neil, *How to Make Money in Stocks*; IBD | RS rating 1–99 (MO-3) |
| Trend template, stage 2 and VCP | Minervini, *Trade Like a Stock Market Wizard* (2013); Weinstein, *Secrets for Profiting in Bull and Bear Markets* (1988) | Trend template (8 criteria), Weinstein stage, the `VCP` setup, max loss 8% |
| Momentum breakout and episodic pivot | Kristjan Kullamägi (Qullamaggie), public trade notes | `MOMENTUM_BREAKOUT`, `EPISODIC_PIVOT`; partial after 3–5 days; trail on the 10- or 20-day average |
| Pocket pivot | Kacher & Morales, *Trade Like an O'Neil Disciple* (2010) | `POCKET_PIVOT` |
| High tight flag | O'Neil; Bulkowski, *Encyclopedia of Chart Patterns* | `HIGH_TIGHT_FLAG` |
| Turtle breakout (System 2) | Faith, *Way of the Turtle* (2007) | `DONCHIAN_55` with a 2N stop and a 20-day-low exit |
| Chandelier exit | LeBeau (via Elder, *Come into My Trading Room*) | `chandelier` policy: highest high − 3 × ATR22 |
| Post-earnings drift | Bernard & Thomas (1989) | The gap-on-volume event behind `EPISODIC_PIVOT` (graded like every setup) |

## 2. Gaps against the current system

| Area | Before this module | Gap | Closed by |
| --- | --- | --- | --- |
| Universe | 10 tradable mega caps + 30 large caps for context, 2 years of IEX daily bars | Momentum is cross-sectional: it needs a broad universe and long history | MO-1, MO-22 |
| Ranking | Scanner score from technique edge and small context points | No cross-sectional momentum, RS rating, trend template or stage | MO-2..MO-5 |
| Setups | 10 swing techniques with fixed T1/T2 targets | No momentum breakout, episodic pivot, pocket pivot, high tight flag or Turtle 55-day breakout | MO-7 |
| Exits | Fixed 1.5R and 3R targets, a 10-session cap | Momentum profits come from letting winners run: no trailing exit, partial-then-trail, climax sale or time stop | MO-9, MO-10 |
| Alerts | One daily scan message; no follow-up | No intraday "trigger hit" or "stop hit", no exit alert for an alerted trade | MO-15, MO-16 |
| Grading | Per-idea R with fixed exits | No grading of exit policies, of the whole alert stream as a portfolio, or of a ranking model against an equal-weight benchmark | MO-11..MO-13 |
| Portfolio | Sector rotation of ETFs (FC-5), not traded | No stock-level momentum portfolio with rebalance alerts | MO-13, MO-25 |
| Risk | Per-trade risk, portfolio heat and correlation for the system's own trades | Market-level crash guard for momentum; heat and correlation across alerted trades; a per-name cap in the model portfolio; bad prints from corporate actions | MO-6, MO-13, MO-17, MO-22 |

## 3. Data and universe

| ID | Requirement |
| --- | --- |
| `MO-1` | **Universe.** `momentum.universe`: about 110 liquid US stocks across every sector (the default list in `config.py`), plus `universe.tickers`. The list is chosen from today's large caps, so it carries **survivorship bias**: stocks that were delisted or shrank are missing. Every result is therefore also compared with the equal-weight buy-and-hold of the same universe (MO-12, MO-13). That isolates the skill of the rules from the luck of the list. Liquidity filter at every date: close ≥ `min_price` ($10) and 20-session average dollar volume ≥ `min_dollar_volume` ($50M). |
| `MO-22` | **Data.** Daily bars from the consolidated SIP feed (free for history older than 15 minutes), split-adjusted, stored as kind `daily_sip` in the bar store by `momentum-history` (default 10 years). The live job reads the store and adds the newest sessions from Alpaca SIP, with the request ending 16 minutes before now. A symbol with fewer than `min_history` (260) sessions, or without the day's bar, is listed as skipped with the reason. **Jump guard:** a one-day gain above 50% is a probable corporate action or bad print (the 2020 RTX merger day and SMCI relisting showed +71% and +73%). The symbol is ineligible for 252 sessions after it. A run before 16:15 ET uses the previous session, so a partial bar is never treated as a closed one. |

## 4. Factors, rating and regime

| ID | Requirement |
| --- | --- |
| `MO-2` | **Factors** per symbol and session, pure functions of the bars up to that session: `r12_1` (return from 252 to 21 sessions ago), `r6_1`, `r3` (63 sessions); `clenow` (annualised slope of the 90-session log-price regression × its R²); `high52` (close ÷ 252-session high); `vadj` (`r12_1` ÷ 252-session volatility); `resid` (12-1 return net of a 252-session SPY beta, ÷ residual volatility); `fip` (information discreteness: sign(`r12_1`) × (% down days − % up days), lower is smoother); `adr` (20-session average daily range %); `max_gap` (largest absolute open gap in 90 sessions). |
| `MO-3` | **RS rating** 1–99: the percentile, across the universe on that session, of 0.4 × r3 + 0.2 × r6 + 0.2 × r9 + 0.2 × r12 (IBD-style weights). |
| `MO-4` | **Trend template** (Minervini): (1) close > SMA150 and SMA200, (2) SMA150 > SMA200, (3) SMA200 higher than 20 sessions ago, (4) SMA50 > SMA150 > SMA200, (5) close > SMA50, (6) close ≥ 1.30 × the 52-week low, (7) close ≥ 0.75 × the 52-week high, (8) RS rating ≥ 70. Score 0–8. **Weinstein stage** from the 150-session (30-week) average: 2 = close above a rising average, 4 = below a falling one, 1 and 3 in between, by the side the average came from. |
| `MO-5` | **Leaderboard.** Composite = the average percentile of `momentum.rank_factors` (default `r12_1`, `clenow`, `high52`, `resid`) across the eligible universe. Eligible means passing the liquidity filter with at least 260 sessions. The top `leaderboard_n` are listed with the composite, every factor, the RS rating, the template score, the stage and a gap flag when `max_gap` > 15% (Clenow). |
| `MO-6` | **Market regime** for new entries. `OFF` when SPY closes under its 200-day average (no new entry alerts). `CAUTION` (half size) when SPY is above it but either the crash guard holds or the macro regime (A18) is `RISK_OFF`. The crash guard holds when SPY's 252-session return is negative and its 21-session return is above +5%, a rebound from a bear market (Daniel & Moskowitz). `ON` otherwise. Exits ignore the regime: a stop is a stop. |

## 5. Setups and entries

| ID | Requirement |
| --- | --- |
| `MO-7` | **Setups**, long only (ADR-008). Each is armed at the close as a buy-stop for the next session, except `POCKET_PIVOT`, which buys at the next open. All except `EPISODIC_PIVOT` need a **leader**: RS rating ≥ `min_rs` (80) and a template score ≥ `min_template` (7). Rules in §6. |
| `MO-8` | **Entry.** A buy-stop fills on the next session only, at max(open, trigger). An open more than 0.5R above the trigger skips the alert, because gaps are not chased (EX-3). The initial stop is the setup's stop, raised to entry × (1 − `max_loss_pct`) (8%) if it is lower. R must be between 0.25 and 2.5 ATR, or there is no setup. New entries need the regime `ON` or `CAUTION` (MO-6). |

## 6. Setup rules

Notation: today's bar `o, h, l, c, v`; ATR = ATR14; b = 0.05 × ATR; vol50 = the prior 50-session average volume.

| Setup | Armed when (today) | Trigger | Stop | Default exit |
| --- | --- | --- | --- | --- |
| `MOMENTUM_BREAKOUT` | Leader. The highest high of the last 40 sessions (the advance's top) is at least 30% above the lowest low of the 60 sessions before it. Since the top: 5 or more sessions, at most 15% below it, the base low above SMA50; from 10 sessions on, the last 5 sessions tighter than the base's first half. Close within 3% of the top | top + b | max(base low, trigger − 1.5 ATR) − b | `ema10` |
| `VCP` | Leader. Base 20–60 sessions, at most 25% deep. Last 10 sessions span ≤ 50% of the base range, with volume ≤ 80% of vol50. Close within 5% of the base high | base high + b | 10-session low − b | `ema20` |
| `HIGH_TIGHT_FLAG` | Leader. The highest high of the last 60 sessions is at least 90% above the lowest low of the 40 sessions before it. Since that high, 10–25 sessions with a pullback of at most 25%. Close within 10% of the high | flag high + b | flag low − b | `ema20` |
| `EPISODIC_PIVOT` | Open ≥ 8% above the prior close, volume ≥ 3 × vol50, close in the top half of the day's range and above the open | h + b | l − b | `ema10` |
| `POCKET_PIVOT` | Leader. Up day whose volume beats every down day's volume of the prior 10 sessions; close above SMA50 and within 1 ATR of SMA10 | next open | lowest low of 3 sessions − b | `ema20` |
| `DONCHIAN_55` | Leader. Close within 1 ATR(20) below the prior 55-session high | 55-session high + b | trigger − 2 × ATR(20) | `donchian20` |
| `HIGH_52W` | Leader. The scanner's tight 10-session range under the 52-week high (SCANNER §3.1) | 52-week high + b | 10-session low − b | `chandelier` |
| `SMA200_PULLBACK` | No leader filter (a pullback fails the template); its own trend rule instead (MO-26). The low comes within 1% of the 200-day average and the close holds at most 2% under it | h + b | min(l, SMA200) − 0.5 ATR | `chandelier` |

## 7. Exits

| ID | Requirement |
| --- | --- |
| `MO-9` | **Exit state machine.** The same code runs live and in the backtest, one session at a time, in this order. (1) **Stop:** a low at or under the stop exits everything at the stop (a gap through the stop exits at the open). (2) **Partial:** sell `partial_frac` (1/3) at +`partial_r` (2R) intraday, or at the next open after the close of session `partial_days` (5) if that close is above the entry; the stop moves to break-even. (3) **Trail**, decided on the close and executed at the next open: close under EMA10 (`ema10`), EMA20 (`ema20`) or SMA50 (`sma50`); or an intraday trailing stop, the highest high since entry − 3 × ATR22 (`chandelier`) or the prior 20-session low (`donchian20`), ratcheted up only. (4) **Climax:** a close ≥ `climax_atr` (7) ATR above SMA50 sells `climax_frac` (1/2) at the next open and tightens the trail to EMA10. (5) **Time stop:** after `time_stop_days` (10) sessions without reaching +1R, exit at the next open. (6) **Max hold** `max_hold_days` (250). A bar that touches the stop and the partial target counts the stop first. |
| `MO-10` | **Policies.** `momentum.policies` names the trail rules. Each setup has a default (§6, overridable in `momentum.setup_policy`). The evaluation grades every setup with every policy. |

## 8. Evaluation, gate and alerts

| ID | Requirement |
| --- | --- |
| `MO-11` | **Walk-forward grading.** Every setup on every past session, from the same code as the live job, graded by MO-8/MO-9 on the sessions that follow. Costs: `cost_bps` (10 bps) per side, in R. Per setup × policy, per half (in-sample = the first half of the graded sessions, out-of-sample = the second): trades, win rate, average and median R, average win and loss, profit factor, average hold, the longest losing streak, and the bootstrap 95% interval of the mean R. The evaluation is repeated without the leader filter and without the regime filter (ablation), so the value of each filter is measured, not assumed. |
| `MO-12` | **Alert-stream portfolio.** The graded trades of the alerted setups (default policies), taken in date order with `max_positions` (8) slots and `risk_pct` risk each (default `risk.risk_per_trade_pct`, 0.5%), the heat cap and the correlation cap (MO-17). When more signals arrive than slots, the highest RS rating goes first. Outputs: the equity curve, CAGR, Sharpe, Sortino, max drawdown, exposure, compared over the same sessions with SPY and with the equal-weight universe. Also the statistical gate SV-3 on the trades. |
| `MO-13` | **Rotation model.** On the last session of every `rebalance_weeks`-th (2nd) ISO week, hold the top `top_n` (10) by composite, weighted by inverse 60-session volatility, each name capped at `rotation_max_weight` (20%; the excess goes to the other names). With the regime filter, no new buys in regime `OFF`. A holding is sold when it drops below rank `hold_rank` (20) or under its SMA100. Decided on the close, held from the next close; costs on turnover. **Selection:** every single factor, the configured composite and the composite of the three best single factors in-sample, each with and without the regime filter, are graded. The live model uses the one with the best **in-sample** CAGR (unless `rotation_factors` is set); its out-of-sample rank is reported, never used to choose. |
| `MO-14` | **Alert gate.** Telegram entry alerts are sent only for setups whose default policy, after costs, has at least `min_trades` (30) trades, a positive average R in both halves and a profit factor ≥ `min_pf` (1.2). The other setups are listed on the console as *unproven* and never sent, unless `momentum.alert_unproven` is on. Exit alerts for an open signal are always sent. |
| `MO-15` | **Live tracker.** Signals live in `momentum_signals` with the states `ARMED → OPEN → CLOSED`, or `ARMED → EXPIRED / SKIPPED`. The close job (16:50 ET) applies the day's bar to open signals (MO-9). It then expires untriggered armed signals and arms the new setups. The intraday job (every 5 minutes, 9:35–15:55 ET) reads Alpaca SIP snapshots for armed and open tickers. It marks triggers (or skips a gap open), stop hits and partial targets as they happen. Each event alerts once (keyed by signal and event), and the state survives restarts. |
| `MO-16` | **Alert content.** Entry: ticker, setup, buy-stop, stop, risk %, shares and dollars at risk, the partial target, the trail rule, the time-stop date, RS rating, template score, stage, earnings within 10 sessions when known, and the setup's measured record (trades, win rate, average R, profit factor). Exit: the action (SELL ALL, SELL 1/3, RAISE STOP), the price, the reason and the R so far. |
| `MO-17` | **Size and concentration.** Shares = equity × risk % ÷ R, using trader-core's risk rules (`RiskManager.size`) or `--equity` in the CLI. Regime `CAUTION` halves it. New entry alerts stop when the alerted signals' total risk to their stops (armed, or open before the partial) would pass `max_heat_pct` (6%) of equity, or when `max_positions` (8) are armed or open. They also stop when `max_correlated` (2) of them already move with the candidate, meaning a 60-session return correlation above 0.7. One signal per ticker. |
| `MO-18` | **Advisory.** No orders are placed (ADR-006). Long stock only (ADR-008). A setup with earnings inside the next 3 sessions is listed but not alerted (CK-1), except `EPISODIC_PIVOT`, whose gap usually *is* the earnings. **Fails closed:** when the Finnhub earnings calendar is missing or older than `earnings_max_age_days` (4), entry alerts are held back as `earnings_unknown` until it is fresh; without a journal (a host CLI run) the report states that earnings were not checked. |
| `MO-19` | **No look-ahead.** Factors, ratings, setups and exit decisions for session D depend only on bars up to D. Cutting the series after D never changes them (tested). |
| `MO-20` | **Outputs.** `trader momentum` (`--json`, `--telegram`, `--date`, `--equity`, `--all`, `--track`); `trader momentum-eval`; `trader momentum-history`; `./run.sh` and `run.cmd` commands of the same names. Also the `MOMENTUM` command, the console page, `momentum.latest_file` and `momentum.eval_file`. |
| `MO-21` | **Config.** `momentum:` with `enabled`, `telegram`, `universe`, the filters, factors, setup thresholds, policies, exit parameters, sizing and gate values. Defaults work without the section; invalid names fail validation. |
| `MO-23` | **Explainable.** Every alert and leaderboard row states its numbers: the factor values, the template items that failed, and the exact rule that fired. |
| `MO-25` | **Rotation alerts.** The close job replays the rotation model over the loaded sessions with the backtest's code. On a rebalance day it lists BUY (with weight and shares), SELL and HOLD against the previous rebalance, and on other days the current holdings and the next rebalance date. Rebalance alerts go to Telegram only when the selected model's CAGR beat SPY and the equal-weight universe in both halves (the MO-25 gate). |
| `MO-26` | **The 200-day pullback** (`SMA200_PULLBACK`, §6). A leader's first pullback to a rising 200-day average. All four must hold: (1) the SMA200 is higher than 20 and 60 sessions ago; (2) a close at least 15% above it within the last 63 sessions; (3) every close of the prior 40 sessions above it, so this is the first test, not a stock churning around the line; (4) today's low within 1% of it and the close at most 2% under it. Graded like every setup (MO-11). Setups in `momentum.watch_only` (default: this one) are armed, tracked and listed with their record, but never alerted and never counted in the alert-stream portfolio (§9.6). |
| `MO-27` | **Near the 200-day average.** The close job lists leaders, meaning the top half of the composite plus `universe.tickers`, that meet MO-26 rules (1)–(3) and close within `sma200_watch_pct` (6%) above the average. That is the price point where the setup would arm. Each row gives the close, the SMA200, the distance in % and in ATR, the distance from the 52-week high, the RS rating, and the stock's own history of first tests: their count, how many held (no close more than 3% under the average within 20 sessions), the share up after 20 sessions, and the median 20- and 60-session returns. Shown in `trader momentum`, the console and `momentum.json` (`sma200_watch`). |
| `MO-24` | **Honest results.** §9 records the measured results with their sample sizes, including setups and policies that lost. A rule is not tuned on the out-of-sample half. Default changes made after looking at both halves are labelled as such. |

## 9. Measured results

`momentum-eval` on October 17, 2026: SIP daily bars from August 2016 to October 16, 2026, 114 symbols (SPCX has too little history), 2,288 graded sessions from September 2017. In-sample (IS) is September 2017 to March 2022 and out-of-sample (OOS) is March 2022 to October 2026. Costs are 10 bps per side. Re-run with `./run.sh momentum-eval`; trader-core re-grades every Sunday.

### 9.1 Rotation model (MO-13)

| Model | IS CAGR | IS max DD | OOS CAGR | OOS Sharpe | OOS max DD |
| --- | --- | --- | --- | --- | --- |
| **Selected on IS: 6-1 + Clenow + 3-month, no regime filter** | **34.0%** | 35.2% | **23.3%** | 0.83 | 38.1% |
| 3-month return only, no regime filter | 32.1% | 32.4% | 22.9% | 0.86 | 34.5% |
| 12-1 return only, no regime filter (the academic classic) | 21.9% | 38.1% | 30.7% | 0.98 | 38.1% |
| The first default composite (12-1, Clenow, 52-week high, residual), with regime filter | 16.4% | 31.1% | 17.5% | 0.78 | 30.1% |
| Equal-weight universe (buy and hold, rebalanced daily) | 26.2% | 34.1% | 19.4% | 0.96 | 24.0% |
| SPY | 14.2% | 34.2% | 12.0% | 0.75 | 22.7% |

What this means:

- **Momentum adds return over the same stocks:** out of sample, the selected model made 23.3% a year against 19.4% for holding all 114 equally and 12.0% for SPY. The MO-25 gate passed.
- **It also adds risk:** the out-of-sample Sharpe (0.83) is lower than equal weight (0.96), and the worst drawdown is deeper (38% against 24%). Concentrating in 10 leaders buys return with volatility. Expect drawdowns of a third of the account.
- **The in-sample choice was not the out-of-sample best:** it ranked 7th of 18 models out of sample, and 12-1 momentum would have done better. This is the normal cost of choosing without hindsight; the numbers above are the honest ones.
- **Survivorship:** the list is today's large caps, so every model and the equal-weight line are inflated. The difference against equal weight is the fair measure, not the absolute CAGR.
- The regime filter (no buys under SPY's 200-day average) **lowered** in-sample returns for every factor. Momentum names often recover first. The filter stays for the swing setups, where it helps (§9.2).

### 9.2 Swing setups (MO-11, MO-14)

Default exit policy per setup, all graded sessions:

| Setup | Exit | Trades | Win | Avg R | PF | Avg R IS / OOS | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `DONCHIAN_55` | `donchian20` | 1,366 | 52% | +0.15 | 1.40 | +0.15 / +0.16 (95% CI above 0 in both) | alerts |
| `POCKET_PIVOT` | `ema20` | 691 | 42% | +0.15 | 1.25 | +0.09 / +0.20 | alerts |
| `EPISODIC_PIVOT` | `ema10` | 76 | 58% | +0.22 | 1.61 | +0.39 / +0.12 | alerts |
| `VCP` | `ema20` | 127 | 49% | +0.10 | 1.23 | +0.14 / +0.04 | alerts |
| `HIGH_52W` | `chandelier` | 224 | 46% | +0.02 | 1.06 | +0.08 / −0.05 | unproven |
| `MOMENTUM_BREAKOUT` | `ema10` | 361 | 46% | −0.01 | 0.99 | −0.01 / +0.00 | unproven |
| `HIGH_TIGHT_FLAG` | `ema20` | 2 | 0% | −0.61 | 0 | — | unproven (too rare in large caps) |

What this means:

- **The only robust swing edge is the Turtle 55-day breakout.** Its confidence interval excludes zero in both halves. It holds without the leader filter too (+0.15R over 3,846 trades), so the edge is the breakout, not the stock selection.
- **The leader filter (RS ≥ 80, template ≥ 7) is mixed.** It helps pocket pivots (+0.15R against +0.06R without it) and the 52-week-high setup (+0.02R against −0.01R). It hurts VCP (+0.10R against +0.15R without) and the momentum breakout (−0.01R against +0.05R). It makes no difference to the 55-day breakout. The defaults keep it, because changing them now would be choosing on the out-of-sample half (MO-24). The weekly re-grade keeps measuring both.
- **The regime filter (no new entries while SPY is under its 200-day average) helps five of the six setups that traded**, by +0.02 to +0.08R a trade. `HIGH_52W` is the exception (+0.04R without it).
- **Qullamaggie-style breakouts did not work on these large caps** (−0.01R). The method targets fast small and mid caps; this universe has none. It stays graded, and stays unproven.
- Results are not sensitive to the unknown order of the low and the trigger on the fill day: the pessimistic version moves each average by at most 0.01R.

| `SMA200_PULLBACK` | `chandelier` | 264 | 51% | +0.14 | 1.33 | +0.21 / +0.09 | passes; watch-only (§9.6) |

### 9.3 The swing alert stream as a portfolio (MO-12)

At 0.5% risk per trade, with 8 slots and the heat and correlation caps, the gated setups made **8.2% a year** (Sharpe 0.75, max drawdown 25%), with only 42% of the money invested on average. That is worse than SPY. The swing alerts are a way to time entries into leaders with a defined risk. **On their own they are not the high-return engine; the rotation model is.** The alert stream passed the SV-3 statistical gate (990 closed trades, the 95% interval of the expectancy +0.07R to +0.29R, the Monte Carlo drawdown inside the limit), so the edge per trade is real, just small next to buy-and-hold of strong stocks.

### 9.4 Fixes the real data forced

| Problem | Evidence | Fix |
| --- | --- | --- |
| Corporate-action prints counted as momentum | RTX +71% (2020 merger), SMCI +73% (2020 relisting) | MO-22 jump guard |
| Fills right at the stop made R-multiples explode | A pocket pivot that opened 0.5% above its stop booked +222R | MO-8: a fill less than 0.25 ATR above the stop is skipped |
| A takeover-pinned stock took 30% of the model | ROKU flat at about $152 with 14% volatility | MO-13: 20% cap per name |
| Four energy names alerted the same evening | COP, XOM, CVX, OXY on October 16 | MO-17 correlation cap |
| The leaderboard and the model ranked on different factors | — | One ranking: the rotation's factors |

### 9.5 What to expect live

These are backtests on a survivorship-biased list. Paper-track the alerts before you trust them with money (TEST_PLAN stage 4): `momentum_signals` keeps every alerted and unproven signal with its outcome. Momentum has crashed suddenly in the past: −73% for US winners-minus-losers in 1932 and −45% in 2009, both in rebounds after bear markets. The crash guard halves size in that pattern, but it cannot prevent a loss.

### 9.6 The 200-day pullback (MO-26, MO-27), added October 17, 2026

The rules were fixed before the first backtest (MO-24). On the same bars and sessions as §9.2:

| | Trades | Win | Avg R | PF | 95% CI of the mean |
| --- | --- | --- | --- | --- | --- |
| In-sample (Sep 2017 – Mar 2022) | 117 | 56% | +0.21 | 1.53 | −0.10 to +0.60 |
| Out-of-sample (Mar 2022 – Oct 2026) | 147 | 48% | +0.09 | 1.20 | −0.17 to +0.38 |
| Without the regime filter | 357 | 48% | +0.07 | 1.14 | −0.10 to +0.26 |

It passes the per-setup gate (MO-14), but only just: the out-of-sample profit factor is at the 1.2 bar, and the interval includes zero in both halves. Added to the alert stream it **lowers** the portfolio: Sharpe falls from 0.75 to 0.63 (out of sample from 0.55 to 0.39), and the stream fails SV-3. It takes slots and heat from the stronger breakouts. On its own the stream's Sharpe is 0.34. So it is **watch-only** (`momentum.watch_only`): armed, tracked in `momentum_signals` and shown with its record, never sent. This was decided after seeing both halves, and it removes alerts rather than adding them. The weekly re-grade keeps measuring it.

Per stock, first tests of the 200-day average are rare (0–3 in ten years for most large caps), so a single stock's history is a hint, not a statistic. The pooled record above is the number to trust.
