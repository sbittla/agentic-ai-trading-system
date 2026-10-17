# Company Event Requirements (EV)

**Version:** 1.0 (October 17, 2026) · Status: **built, advisory** · Companion to [MOMENTUM_REQUIREMENTS.md](MOMENTUM_REQUIREMENTS.md) and [NEWS_CATALYST_REQUIREMENTS.md](NEWS_CATALYST_REQUIREMENTS.md)

The events module (agent A20) covers recurring company events, such as product launches, developer conferences, keynotes and Prime Day. It measures how each stock moved around the same event in past years. When the pattern was consistent, it turns the next occurrence into a dated idea, for example "buy at the close of November 13, sell at the close of November 30".

```bash
./run.sh events            # upcoming events, their past pattern and the ideas (add --telegram)
./run.sh events NVDA       # one ticker
```

trader-core refreshes `reports/events.json` at 17:05 ET, after the momentum job has topped up the bars. With `events.telegram: true` it sends an idea the evening before its buy window opens. The console shows the report on the **Momentum** page (*Company events ahead*).

**What this is not.** A company has one WWDC a year, so ten years give ten observations. That is enough to see a strong tendency, but not enough to prove an edge. With 13 groups and 3 windows each, about 3 of 33 tests would pass a p ≤ 0.10 bar by chance. The report prints that count beside every result. Ideas are for small positions with a stop, never for conviction sizing.

## 1. Calendar

| ID | Requirement |
| --- | --- |
| `EV-1` | **Calendar file** `events.file` (`config/corporate_events.yaml`), kept by hand. Each group is one kind of event for one ticker: `id`, `ticker`, `name`, an optional `family`, the past `dates` and the `upcoming` ones. A date is the keynote or start day, written `YYYY-MM-DD` when the event starts during or before market hours, and `YYYY-MM-DD amc` when it starts after the 16:00 ET close. An upcoming date may be marked `estimated: true` when it is guessed from the usual pattern. A duplicate group id, or a date listed twice, fails the load. The dates were collected from public announcements and verified where possible, so check one before trading on it, and add each new year's date when the company announces it. |
| `EV-2` | **Reaction session.** The first session that can price the event: the event day when it starts during or before market hours, else the next session (after-close events, weekends, holidays). Upcoming dates use the NYSE calendar. |

## 2. Event study

| ID | Requirement |
| --- | --- |
| `EV-3` | **Windows** around the reaction session R, close to close: `run_up` from R − 1 − `pre_sessions` (10) to R − 1, `reaction` from R − 1 to R, and `drift` from R to R + `post_sessions` (10). Each is reported raw, net of SPY over the same closes, and net of SPY and the baseline (EV-4). An event without bars for all three windows is left out. |
| `EV-4` | **Baseline.** The stock's mean return net of SPY over every window of the same length, between its first and last listed event. It is subtracted, because a stock that beat SPY all decade shows a "pattern" in any window. NVDA's baseline is +1.9% per 10 sessions; subtracting it brought the pooled run-up into NVIDIA keynotes from +5.5% to +3.6% (§4). |
| `EV-5` | **Pattern.** A window has a pattern, `up` or `down`, when all of these hold: at least `min_years` (6) events; a mean move of at least `min_move_pct` (1%) beyond the baseline; at least `min_hit` (70%) of the events moving that way; a two-sided sign-test p ≤ `max_p` (0.10); and the newer half of the events agreeing in sign. A group with too few events uses its `family` (all of one company's keynotes, pooled) when the family meets the same bar, and the idea says so. The report counts the tests and the patterns expected by chance (tests × `max_p`). |

## 3. Ideas and outputs

| ID | Requirement |
| --- | --- |
| `EV-6` | **Ideas** for each upcoming event within `lookahead_days` (90), one per window with a pattern, dated from its reaction session: `BUY_RUN_UP` (buy at the close of R − 1 − pre, sell at the close of R − 1), `HOLD_THROUGH` (close of R − 1 to close of R, so it carries the event's gap risk), `BUY_AFTER` (close of R to close of R + post), and, long only (ADR-008), the cautions `AVOID_BEFORE`, `TRIM_BEFORE` (sell the news) and `WAIT_AFTER`. Each idea carries its status (upcoming, open, passed), the basis (the group or its family), the event count, mean, median, same-sign rate, p-value and the newer half's mean. A buy idea also carries a stop hint: the last close − 2 × ATR14. Every upcoming event is listed with its trend context (close against the 200-day average) even when it has no idea. |
| `EV-7` | **Outputs.** `trader events [TICKER]` (`--json`, `--telegram`, `--no-write`), `./run.sh events` / `run.cmd events`, `events.latest_file`, trader-core's 17:05 ET job (Telegram only for buy ideas whose window opens next session, and only with `events.telegram`), and the console card. Advisory: no orders (ADR-006). |
| `EV-8` | **Config** `events:` with `enabled`, `file`, `pre_sessions`, `post_sessions`, `min_years`, `min_hit`, `max_p`, `min_move_pct`, `lookahead_days`, `telegram`, `latest_file`. Defaults work without the section, and out-of-range values fail validation. A missing calendar file fails the events job (logged), never the platform's start. |

## 4. Measured results

`trader events` on October 17, 2026, with SIP daily bars from August 2016 to October 16, 2026. Moves are in % net of SPY and of the stock's baseline. "Up" is the share of events with a positive move.

| Group | Events | Into (10 sessions) | Event session | After (10 sessions) |
| --- | --- | --- | --- | --- |
| NVDA GTC keynote | 10 | **+5.4%, 90% up, p 0.02: pattern** | +1.8%, 60% up | −1.0%, 40% up |
| NVDA keynotes pooled (GTC, CES, GTC DC) | 15 | **+3.6%, 80% up, p 0.04: pattern** | +1.2%, 53% up | +0.4%, 40% up |
| AAPL WWDC | 10 | +0.2%, 50% up | −0.6%, 30% up | +0.7%, 60% up |
| AAPL iPhone event | 10 | +0.4%, 70% up | −0.9%, 20% up, p 0.11 | +0.5%, 50% up |
| AAPL keynotes pooled | 20 | +0.3%, 60% up | −0.7%, 25% up, p 0.04 (under the 1% bar) | +0.6%, 55% up |
| GOOGL I/O | 9 | −1.3%, 44% up | +0.1%, 44% up | −0.7%, 56% up |
| GOOGL Pixel launch | 11 | −0.5%, 45% up | 0.0%, 45% up | +1.6%, 64% up |
| MSFT Build | 10 | +0.9%, 60% up | −0.2%, 70% up | −2.1%, 20% up, p 0.11 |
| MSFT Ignite | 10 | +0.4%, 60% up | 0.0%, 60% up | −1.1%, 50% up |
| META Connect | 11 | +0.7%, 45% up | +0.1%, 64% up | −1.1%, 55% up |
| AMZN re:Invent | 10 | −0.1%, 50% up | 0.0%, 50% up | −1.3%, 20% up, p 0.11 |
| AMZN Prime Day | 10 | +0.2%, 50% up | −0.2%, 50% up | −0.5%, 50% up |
| TSLA product and AI events | 8 | +0.5%, 25% up | −5.4%, 25% up, p 0.29 | −1.0%, 38% up |

What this means:

- **One real-looking pattern: NVIDIA runs up into GTC.** In 9 of 10 years NVDA beat SPY and its own trend in the 10 sessions before the keynote, by 5.4% on average. Pooled with its other keynotes it still holds (+3.6%, 12 of 15). The event itself and the days after show nothing. This is the run-up the report turns into `BUY_RUN_UP` ideas: for GTC DC on December 1, 2026, buy at the close of November 13 and sell at the close of November 30.
- **Apple sells the news, mildly.** The keynote session lagged in 15 of 20 Apple events (p 0.04), but by only 0.7% on average, under the 1% bar. It is not worth trading, but it argues against buying the day before a keynote.
- **After-event weakness at MSFT Build and AWS re:Invent** (8 of 10 years lower, −2.1% and −1.3%) is suggestive but misses the p bar. Watch it, don't trade it.
- **Tesla events are a coin flip with fat tails.** The average −5.4% on the event session comes from a few big drops; 6 of 8 fell, but the sample is tiny.
- **Chance:** 1 pattern in 33 tests, where about 3 would be expected by chance at p ≤ 0.10. The NVDA run-up may be one of them. It has a story behind it (attention and expectations building into a keynote) and it held in the newer half of the years, but size it as a small, stopped trade.

## 5. Gaps and next steps

- **Earnings** are not in this module: the next report date comes from Finnhub (NC-16), and the catalyst module studies earnings reactions.
- **More companies.** Only the tradable universe's events are listed. AMD Advancing AI, TSMC's technology symposium and investor days could be added the same way.
- **Re-check the dates** each year: an estimated date becomes real when the company announces it, and a wrong date moves every window.
