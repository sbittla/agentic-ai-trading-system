"""Macro page (MA-8, FC-6): regime, indicators, releases, Fed and economy headlines, and the
forecast card. Reads reports/macro.json and reports/forecast.json. Advisory."""

from __future__ import annotations

from typing import Any

from trader.console.render import (
    card,
    chip,
    cmd_button,
    code,
    e,
    empty,
    num,
    page_head,
    stock_link,
    table,
    tile,
    tiles,
    ts,
)

LABEL_TONE = {"RISK_ON": "ok", "RISK_OFF": "bad", "NEUTRAL": "neutral"}
SIGNAL = {1: ("risk-on", "ok"), -1: ("risk-off", "bad"), 0: ("neutral", "neutral")}
TONE = {"risk-on": "ok", "risk-off": "bad", "neutral": "neutral"}


def _forecast(fc: dict[str, Any] | None) -> str:
    if not fc or not fc.get("tickers"):
        return empty("No forecast yet", "trader-core writes it at 4:45 PM ET, or run "
                     f"{code('./run.sh forecast')}.", icon="◌")
    rows = []
    for t, x in fc["tickers"].items():
        v = x.get("volatility") or {}
        ev = {k: d for k, d in (x.get("events") or {}).items() if isinstance(d, dict)
              and d.get("used")}
        rows.append({"ticker": t, "last": x.get("last"),
                     "r1": v.get("range_1d_pct"), "r5": v.get("range_5d_pct"),
                     "band": f"{num((v.get('range_1d') or [None])[0])} – "
                             f"{num((v.get('range_1d') or [None, None])[1])}",
                     "method": v.get("method"),
                     "events": ", ".join(f"{k} ×{d['multiplier']:.1f}" for k, d in ev.items())
                     or "—",
                     "sector": f"{x.get('sector') or '—'} #{x.get('sector_rank') or '—'}"})
    body = table(rows, [("ticker", "Ticker"), ("last", "Last"), ("r1", "±1 day %"),
                        ("band", "80% next session"), ("r5", "±5 days %"),
                        ("method", "Estimator"), ("events", "Macro-day move"),
                        ("sector", "Sector rank")], "", {
        "ticker": lambda v, row: stock_link(v), "last": lambda v, row: num(v),
        "r1": lambda v, row: num(v, 1), "r5": lambda v, row: num(v, 1)}, compact=True)
    sec = fc.get("sectors") or []
    if sec:
        body += '<p class="small">Sector leadership: ' + e(", ".join(
            f"{r['rank']}. {r['etf']}" for r in sec)) + "</p>"
    if not fc.get("graded"):
        body += (f'<p class="muted small">Not graded yet: {code("./run.sh forecast-eval")} '
                 "picks the best volatility estimator per stock.</p>")
    return body


def page(macro: dict[str, Any] | None, fc: dict[str, Any] | None, csrf: str) -> str:
    run = cmd_button(csrf, "MACRO", "", "Refresh regime", primary=True)
    sub = "Economic data, Fed and economy news, and the regime every idea is checked against"
    if not macro:
        return (page_head("Macro", sub, run)
                + card("Regime", empty("No macro regime yet",
                                       "Run " + code("./run.sh macro-backfill") + " once, "
                                       "then trader-core refreshes it at 8:40 AM and 4:35 PM.",
                                       run, "◎"))
                + card("Forecasts", _forecast(fc)))
    r = macro["regime"]
    up = sum(1 for i in r["indicators"] if i["signal"] > 0)
    down = sum(1 for i in r["indicators"] if i["signal"] < 0)
    body = page_head("Macro", f"As of {e(r['asof'])} · generated "
                     f"{ts(macro.get('generated'), '%m-%d %H:%M')}", run) + tiles([
        tile("Regime", e(r["label"].replace("_", " ").lower()), f"score {r['score']:+.0f}",
             tone=LABEL_TONE.get(r["label"], "")),
        tile("Risk-on signals", str(up), tone="ok"),
        tile("Risk-off signals", str(down), tone="bad"),
        tile("Indicators", str(len(r["indicators"])), "with data")])
    for n in macro.get("notes", []):
        body += f'<p class="muted small">{e(n)}</p>'
    ind = [{"name": i["name"], "value": i["value"], "signal": i["signal"],
            "detail": i["detail"]} for i in r["indicators"]]
    body += card("Indicators", table(ind, [("name", "Indicator"), ("value", "Value"),
                                           ("signal", "Signal"), ("detail", "Detail")], "", {
        "value": lambda v, row: num(v, 2),
        "signal": lambda v, row: chip(*SIGNAL.get(v, ("neutral", "neutral")))}),
        sub="Fixed published rules (MA-5); RISK_ON at +25, RISK_OFF at -25.")
    rel = [{"series": k, **x} for k, x in (r.get("releases") or {}).items()]
    body += card("Latest releases", table(rel, [
        ("series", "Series"), ("date", "Date"), ("value", "Value"), ("change", "Change"),
        ("trend_change", "Trend change"), ("surprise_sd", "Surprise (sd)")], "no releases", {
        "value": lambda v, row: num(v, 2), "change": lambda v, row: num(v, 3, sign=True),
        "trend_change": lambda v, row: num(v, 3, sign=True),
        "surprise_sd": lambda v, row: num(v, 1, sign=True)}, compact=True),
        sub="Surprise vs the trend of the prior 6 releases, not consensus (free data).")
    news = macro.get("news") or []
    body += card("Fed and economy headlines", table(news, [
        ("ts", "Time"), ("source", "Source"), ("theme", "Theme"), ("tone", "Tone"),
        ("headline", "Headline")], "no headlines", {
        "ts": lambda v, row: ts(v, "%m-%d %H:%M"),
        "tone": lambda v, row: chip(v, TONE.get(v, "neutral"))}, compact=True))
    body += card("Forecasts", _forecast(fc), sub="Ranges are the usual move, not a target.")
    return body
