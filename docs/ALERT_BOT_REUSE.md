# Reuse Map: Existing Alert Bot → Trading Agents

**Version:** 1.0 (September 30, 2026)

The existing momentum/breakout alert bot (nine Python modules) already has data ingestion, indicators, sentiment scoring, pattern rules, a composite score and a Twilio WhatsApp dispatcher. This map says what moves into the new system as is, what changes, and what is dropped. File names below describe the bot's functions; replace them with the actual module names when the code is copied in.

| Alert-bot function | Decision | New home | Changes needed |
| --- | --- | --- | --- |
| Price data via yfinance | **Replace** for live; keep for research | `HistoricalData` fake / research notebooks only | Live and replay use Alpaca (IEX). yfinance is unofficial and can break (never on the order path) |
| Earnings calendar | **Adapt** | A3 `NewsSource` (Finnhub `calendar/earnings`) | Switch source to Finnhub; output `EARNINGS_TODAY` / `EARNINGS_3D` flags |
| News via NewsAPI | **Replace** | A3 `FinnhubNews` + `EdgarFilings` | NewsAPI's free tier is delayed and limited; Finnhub company news is ticker-tagged |
| Reddit sentiment via PRAW | **Reuse** | A3 `RedditPosts` | Restrict to the 10 tickers and 4 subreddits; add text cleaning (DQ-4); respect 100 queries/min |
| Indicators: RSI, MACD, EMA, ATR, Bollinger, Keltner, OBV, CMF, RVOL | **Reuse** | A4 `indicators/` | Make them work incrementally on 1/5-min bars; RVOL becomes time-of-day RVOL; ATR uses Wilder smoothing; add VWAP and opening range; unit tests against reference values |
| VADER sentiment | **Drop** | — | FinBERT is better for financial text; one sentiment model keeps results comparable |
| FinBERT sentiment | **Reuse** | A3 | Run on CPU in the trader container; batch per polling cycle |
| Pattern rule: gap-up fade | **Drop for v1** | — | It is a short/fade setup; v1 is long only (ADR-008). Keep code for a future short module |
| Pattern rule: dip accumulation | **Adapt** | Input to `FIRST_PULLBACK` and `SWING_CLOSE_NEAR_HIGH` | Re-express with STRATEGY_SPEC.md definitions |
| Pattern rule: volume spike + sentiment divergence | **Keep as context** | A5 input feature | Not a trigger; shown to the strategy and review agents |
| Pattern rule: earnings volatility compression | **Keep as context** | A3/A5 | Used to flag pre-earnings risk, not to trade earnings |
| Composite breakout score | **Adapt** | A4 `signal_score` (0–100) | Re-weight for intraday setups; used to rank simultaneous signals (STRATEGY_SPEC.md §8) |
| ML classifier (LightGBM / RandomForest) | **Defer** | Phase after M8 | Needs months of labelled outcomes; the journal now collects them automatically |
| Twilio WhatsApp dispatcher with retries | **Reuse** | `notify/whatsapp.py` | Add inbound commands, sender allow-list, approval codes, two-step confirmation (SECURITY.md) |
| Orchestration (`main.py`, single-run and loop modes) | **Replace** | A10 supervisor + scheduler | Event-driven, calendar-aware; the old loop mode is kept only as a research script |
| `requirements.txt` | **Merge** | `pyproject.toml` + `requirements.lock` | Pin versions with hashes |

## Migration steps

1. Copy the indicator and sentiment modules into `src/trader/agents/a04_technicals/indicators/` and `a03_news_sentiment/`, unchanged, with their current tests.
2. Write reference tests: the same input bars must give the same indicator values in old and new code.
3. Refactor to incremental (bar-by-bar) calculation; reference tests must still pass.
4. Move the WhatsApp dispatcher behind the `Notifier` interface; add inbound command handling.
5. Archive the old `main.py` loop under `research/` for ad-hoc scanning.
