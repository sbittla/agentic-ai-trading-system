# Replay and Backtest Engine

**Version:** 1.0 (September 30, 2026) · Companion to [TEST_PLAN.md](TEST_PLAN.md) and [INTERFACES.md](INTERFACES.md)

One engine serves both replay (full recorded days, all agents, Qwen in the loop) and backtest (months of bars, fast, Qwen optional). It runs the **same agent code** as live trading by swapping the interfaces: `ReplayClock`, `RecordedFeed`, `RecordedNews`, `SimBroker`, `CachedLLM`.

---

## 1. Modes

| Mode | Data | Agents | Qwen | Speed | Used for |
| --- | --- | --- | --- | --- | --- |
| Replay | Recorded days (bars, quotes, news) | All | Real model, responses cached | ~10–50x real time | Regression tests, prompt changes, incident review |
| Backtest-rules | Historical 1-min bars | A2, A4, A7, A8, A9, A12 | Off (A5/A6 replaced by "take every valid setup") | Thousands of days per hour | Setup edge, parameter tuning, walk-forward |
| Backtest-LLM | Historical bars + archived news | All | Real model | Slow | Measure whether Qwen filtering improves expectancy (BT-5) |

## 2. Event loop

```
for each bar (or quote) in time order across all symbols:
    clock.advance_to(bar.end_time)
    scheduler.fire_jobs_due(clock.now())        # game plans, flatten times, reports
    feed.publish(bar)                           # A2 -> A4 -> ... exactly as live
    sim_broker.match_open_orders(bar)           # fills, stops, targets
    bus.drain()                                 # all agents run until idle
```

The engine never lets an agent see a bar before its end time, and news only after its `published_at` (BT-2).

## 3. SimBroker fill model

| Order | Fill rule (1-min bars) |
| --- | --- |
| Limit buy (entry) | Fills if bar low ≤ limit − $0.01 (price must trade through), at the limit price; no fill if it only touches |
| Stop (sell) | Triggers if bar low ≤ stop; fills at min(stop, bar open) − slippage (gaps fill at the open) |
| Take-profit limit (sell) | Fills if bar high ≥ limit + $0.01, at the limit |
| Stop and target in the same bar | Stop first (conservative), unless the bar opened beyond the target |
| Marketable limit exit (time stop, flatten) | Next bar's open − slippage, capped at the limit |
| Partial fills | Volume cap: at most 10% of the bar's IEX volume fills per bar; remainder waits |
| Slippage | 0.02% per side (config), plus SEC and FINRA fees on sells |
| Halts | No fills while a halt flag is set; orders rest until resumption |

The fill model is versioned; any change is replayed on the recorded-day library and the paper-vs-sim slippage comparison (TEST_PLAN.md PT-2) is used to calibrate it.

## 4. LLM caching

- Key: SHA-256 of (model tag, prompt version, system prompt, DATA block, schema).
- First run calls Qwen and stores the response in `llm_cache/`; later runs read it, making replays deterministic (RP-2) and fast.
- A prompt or model change invalidates the cache for that agent only.

## 5. Walk-forward procedure (BT-1)

1. Split history into rolling windows: 12 months in-sample, 3 months out-of-sample, step 3 months.
2. In-sample: tune only parameters listed as tunable in CONFIG_REFERENCE.md (for example `min_rvol`, chase limit), on a coarse grid.
3. Out-of-sample: run with the chosen values; record results.
4. Report only the concatenated out-of-sample results; flag any parameter whose best value changes every window (unstable, likely overfit).

## 6. Outputs

Each run writes a self-contained folder:

```
runs/2026-10-14_replay_v1.3/
  run.yaml            # code commit, config hash, fill model version, prompt versions, data range
  journal.db          # full journal, same schema as live
  trades.csv
  metrics.json        # expectancy, profit factor, drawdown, by setup/ticker/window
  report.html         # same templates as the daily report
  diff_vs_previous.md # decisions that changed since the last run of the same days
```

## 7. Known limitations

- IEX-only bars understate volume and may miss prints that happened on other exchanges; fills in backtest can be optimistic or pessimistic versus live. Calibrate with paper trading.
- News history for Backtest-LLM mode is limited to what Finnhub and EDGAR return for past dates; Reddit history is incomplete.
- No queue-position modelling; limit fills require trade-through, which is conservative.
