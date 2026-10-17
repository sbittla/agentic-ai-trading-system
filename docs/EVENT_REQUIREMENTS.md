# Company Event Requirements (EV)

**Version:** 1.1 (October 17, 2026) · Status: **built, advisory** · Companion to [MOMENTUM_REQUIREMENTS.md](MOMENTUM_REQUIREMENTS.md) and [NEWS_CATALYST_REQUIREMENTS.md](NEWS_CATALYST_REQUIREMENTS.md)

The events module (agent A20) covers recurring company events, such as product launches, developer conferences, keynotes and Prime Day. It measures how each stock moved around the same event in past years. When the pattern was consistent, it turns the next occurrence into a dated idea, for example "buy at the close of November 13, sell at the close of November 30". Every idea is kept and graded when its window closes.

```bash
./run.sh events            # upcoming events, their past pattern and the ideas (add --telegram)
./run.sh events NVDA       # one ticker
```

trader-core refreshes `reports/events.json` at 17:05 ET, after the momentum job has topped up the bars, and records the ideas in `event_ideas`. With `events.telegram: true` it sends a buy idea the evening before its window opens, but only when no earnings report falls inside the window (EV-10). The console shows the report on the **Momentum** page (*Company events ahead*).

**What this is not.** A company has one WWDC a year, so ten years give about nine usable observations. That is enough to see a strong tendency, but not enough to prove an edge. With 13 groups and 3 windows each, about 3 of 33 tests would pass the bar by chance, and **3 did** (§4). As a set, the patterns are indistinguishable from luck. The report prints that count beside every result. Ideas are for small positions with a stop, never for conviction sizing.

## 1. Calendar

| ID | Requirement |
| --- | --- |
| `EV-1` | **Calendar file** `events.file` (`config/corporate_events.yaml`), kept by hand. Each group is one kind of event for one ticker: `id`, `ticker`, `name`, an optional `family`, the past `dates` and the `upcoming` ones. A date is the keynote or start day, written `YYYY-MM-DD` when the event starts during or before market hours, and `YYYY-MM-DD amc` when it starts after the 16:00 ET close. An upcoming date may be marked `estimated: true` when it is guessed from the usual pattern. A duplicate group id, or a date listed twice, fails the load. Check a date before trading on it, and add each new year's date when the company announces it. |
| `EV-2` | **Reaction session.** The first session that can price the event: the event day when it starts during or before market hours, else the next session (after-close events, weekends, holidays). Upcoming dates use the NYSE calendar. |

## 2. Event study

| ID | Requirement |
| --- | --- |
| `EV-3` | **Windows** around the reaction session R, close to close: `run_up` from R − 1 − `pre_sessions` (10) to R − 1, `reaction` from R − 1 to R, and `drift` from R to R + `post_sessions` (10). Each is reported raw, net of SPY over the same closes, and net of SPY and the baseline (EV-4). An event without bars for all three windows is left out. |
| `EV-4` | **Trailing baseline.** The stock's mean return net of SPY over every window of the same length, in the 504 sessions before the event's run-up window, so no event is adjusted with later data. It is subtracted, because a stock that beat SPY all decade shows a "pattern" in any window. An event with fewer than 250 sessions before it is left out, which drops the 2016–early 2017 events (the SIP history starts in August 2016). Version 1.0 used one baseline over the whole period, a mild look-ahead. |
| `EV-5` | **Pattern.** A window has a pattern, `up` or `down`, when all of these hold: at least `min_years` (6) events; a mean move of at least `min_move_pct` (1%) beyond the baseline; at least `min_hit` (70%) of the events moving that way; a two-sided sign-test p ≤ `max_p` (0.10); and the newer half of the events agreeing in sign. A group with too few events uses its `family` (all of one company's keynotes, pooled) when the family meets the same bar, and the idea says so. The report counts the tests and the patterns expected by chance (tests × `max_p`). |

## 3. Ideas, checks and outputs

| ID | Requirement |
| --- | --- |
| `EV-6` | **Ideas** for each upcoming event within `lookahead_days` (90), one per window with a pattern, dated from its reaction session: `BUY_RUN_UP` (buy at the close of R − 1 − pre, sell at the close of R − 1), `HOLD_THROUGH` (close of R − 1 to close of R, so it carries the event's gap risk), `BUY_AFTER` (close of R to close of R + post), and, long only (ADR-008), the cautions `AVOID_BEFORE`, `TRIM_BEFORE` (sell the news) and `WAIT_AFTER`. Each idea carries its status (upcoming, open, passed), the basis (the group or its family), the event count, mean, median, same-sign rate, p-value and the newer half's mean. A buy idea also carries a stop hint: the last close − 2 × ATR14. Every upcoming event is listed with its trend context (close against the 200-day average) even when it has no idea. |
| `EV-7` | **Outputs.** `trader events [TICKER]` (`--json`, `--telegram`, `--no-write`), `./run.sh events` / `run.cmd events`, `events.latest_file`, trader-core's 17:05 ET job (Telegram only with `events.telegram`, and only for buy ideas whose window opens next session and whose verdict is `ok`), and the console card. Advisory: no orders (ADR-006). |
| `EV-8` | **Config** `events:` with `enabled`, `file`, `pre_sessions`, `post_sessions`, `min_years`, `min_hit`, `max_p`, `min_move_pct`, `lookahead_days`, `telegram`, `latest_file`. Defaults work without the section, and out-of-range values fail validation. A missing calendar file fails the events job (logged), never the platform's start. |
| `EV-9` | **Ledger.** Every idea is stored in `event_ideas`, keyed by group, event date and window, with the day it was first seen. Its fields are refreshed while the window is open, and it is never deleted. Once the window has closed it is graded once: the return from the close of `enter` to the close of `exit`, raw and net of SPY. After that the row is frozen. The report gives the graded buy ideas' count, the share that beat SPY and the mean. |
| `EV-10` | **Earnings inside the window.** None of the past windows held an earnings report, so the history says nothing about one that does. A buy idea is `earnings_in_window` (blocked) when an earnings date falls between `enter` and `exit`, inclusive. It is `earnings_unknown` (held) when no calendar covers the window, and `ok` otherwise. Neither blocked nor held ideas are sent. Dates come from the journal's Finnhub calendar (fresh within 4 days, covering 60 days ahead), else from the calendar file's hand-entered `earnings:` list. Cautions are never blocked, but they show the date. |

## 4. Measured results

`trader events` on October 17, 2026 (version 1.1, trailing baseline), with SIP daily bars from August 2016 to October 16, 2026. Moves are in % net of SPY and of the stock's trailing baseline. "Up" is the share of events with a positive move.

| Group | Events | Into (10 sessions) | Event session | After (10 sessions) |
| --- | --- | --- | --- | --- |
| NVDA GTC keynote | 9 | **+6.4%, 89% up, p 0.04: pattern** | 0.0%, 56% up | −2.5%, 33% up |
| NVDA keynotes pooled (GTC, CES, GTC DC) | 14 | **+4.0%, 79% up, p 0.06: pattern** | +0.1%, 50% up | −0.6%, 36% up |
| AAPL WWDC | 9 | +0.4%, 56% up | −0.5%, 33% up | +1.4%, 56% up |
| AAPL iPhone event | 9 | +0.7%, 78% up | −0.9%, 22% up, p 0.18 | +1.2%, 56% up |
| AAPL keynotes pooled | 18 | +0.6%, 67% up | −0.7%, 28% up, p 0.10 (under the 1% bar) | +1.3%, 56% up |
| GOOGL I/O | 8 | **−1.7%, 12% up, p 0.07: pattern (down)** | +0.2%, 50% up | −1.0%, 50% up |
| GOOGL Pixel launch | 10 | −0.3%, 50% up | −0.1%, 40% up | +1.6%, 60% up |
| MSFT Build | 9 | +0.9%, 44% up | −0.3%, 67% up | −2.3%, 22% up, p 0.18 |
| MSFT Ignite | 9 | +0.3%, 56% up | −0.1%, 67% up | −1.4%, 44% up |
| META Connect | 10 | +0.9%, 60% up | +0.2%, 60% up | −1.4%, 50% up |
| AMZN re:Invent | 9 | −0.6%, 44% up | +0.1%, 56% up | −1.5%, 22% up, p 0.18 |
| AMZN Prime Day | 9 | +0.3%, 56% up | −0.2%, 56% up | −0.8%, 44% up |
| TSLA product and AI events | 8 | +0.4%, 50% up | **−5.4%, 12% up, p 0.07: pattern (down)** | −1.1%, 25% up |

What this means:

- **3 patterns in 33 tests, where about 3.3 are expected by chance.** As a set, this is what luck alone produces. Each pattern on its own is weak evidence, and the patterns should not be stacked.
- **NVIDIA's run-up into GTC is the most consistent** (8 of 9 years, +6.4% beyond its trailing trend). It has a plausible story: attention and expectations build into the keynote. It is still the kind of pattern that dies once noticed.
- **The live NVIDIA idea is blocked.** The GTC DC run-up window (November 13–30, 2026) holds NVIDIA's earnings on November 17 (Finnhub), and no past GTC window held an earnings report. EV-10 blocks it: the history does not describe this window.
- **Google drifts lower into I/O (7 of 8 years) and Tesla falls on its event day (7 of 8).** Both are long-only cautions at most (`AVOID_BEFORE`, `TRIM_BEFORE`). Tesla's events are irregular, so there is nothing to schedule.
- **Apple sells the news, mildly.** The keynote session lagged in 13 of 18 Apple events, but by only 0.7% on average, under the 1% bar. It argues against buying the day before a keynote, not for a trade.
- **The event list was chosen with hindsight.** It includes GTC, a run-up that is well known. Selection bias of this kind is not in the chance count, which is a further reason to trust the forward record (EV-9) over the backtest.

## 5. Gaps and next steps

- **The forward record decides.** `event_ideas` grades every idea. Treat the module as unproven until a dozen graded buy ideas beat SPY on average.
- **Earnings history.** Past report dates are not stored, so the study cannot test windows that held earnings. That is why EV-10 blocks them instead.
- **More companies.** Only the tradable universe's events are listed. AMD Advancing AI, TSMC's technology symposium and investor days could be added. Each addition is another test, raising the chance count.
- **Re-check the dates** each year: an estimated date becomes real when the company announces it, and a wrong date moves every window.
