"""Catalysts page (NEWS_CATALYST_REQUIREMENTS NC-27).

Reads ``reports/catalysts.json`` (written by trader-core at 8:45, through the day, at 4:25 PM
ET, by the CATALYST command and by ``trader catalyst``) and ``reports/catalyst_eval.json``
(``trader catalyst-eval``, weekly). Advisory: nothing on this page places an order.
"""

from __future__ import annotations

from typing import Any

from trader.catalyst.report import DISCLAIMER, VEHICLES, hname
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

LABEL_TONE = {"MAJOR": "bad", "STRONG": "warn", "MODERATE": "info", "WEAK": "neutral"}
STANCE_TONE = {"BULLISH": "ok", "BEARISH": "bad", "NEUTRAL": "neutral", "NO_TRADE": "neutral"}
VERDICT_TONE = {"supports": "ok", "against": "bad", "neutral": "neutral"}


def _pct(v: Any) -> str:
    return "—" if v is None else f"{float(v):+.1f}%"


def _horizons(o: dict[str, Any]) -> str:
    rows = [{"h": hname(int(v["h"])), "p_up": f"{float(v['p_up']):.0%}",
             "median": _pct(v["median_pct"]),
             "range": f"{_pct(v['p10_pct'])} … {_pct(v['p90_pct'])}",
             "excess": _pct(v["ex_median_pct"]), "conf": v["confidence"],
             "basis": v["basis"]} for v in o.get("horizons", [])]
    return table(rows, [("h", "Horizon"), ("p_up", "P(up)"), ("median", "Typical"),
                        ("range", "Likely range 10-90%"), ("excess", "Typical vs SPY"),
                        ("conf", "Confidence"), ("basis", "Based on")], "no outlook", {
        "conf": lambda v, row: chip(v, {"HIGH": "ok", "MEDIUM": "info"}.get(v, "neutral"))},
        compact=True)


def _confluence(o: dict[str, Any]) -> str:
    return table(o.get("confluence", []), [("group", "Group"), ("name", "Check"),
                                           ("value", "Value"), ("verdict", "Verdict"),
                                           ("detail", "Detail")], "no checks", {
        "verdict": lambda v, row: chip(v, VERDICT_TONE.get(v, "neutral"))}, compact=True)


def _factors(o: dict[str, Any]) -> str:
    f = o.get("factors") or {}
    return " · ".join(f"{k} {num(v, 0)}" for k, v in f.items())


def _detail(o: dict[str, Any]) -> str:
    view = o.get("view") or {}
    pl = view.get("plan") or {}
    plan = ""
    if pl:
        size = (f" · {pl['qty']} sh, risk ${num(pl.get('risk_usd'), 0)} "
                f"({e(pl.get('size_binding'))})" if pl.get("qty") else "")
        plan = (f"<p><b>Plan</b> ({e(pl.get('side'))}): trigger {num(pl['trigger'])} · "
                f"invalidation {num(pl['invalidation'])} · T1 {num(pl['t1'])} · "
                f"T2 {num(pl['t2'])}{size}</p>")
    soc = o.get("social")
    social = ("<p class='muted small'>No social data (set Reddit keys in .env.news).</p>"
              if not soc else
              f"<p><b>Social</b>: {soc['mentions']} mentions"
              + (f" (z {num(soc['mention_z'], 1)})" if soc.get("mention_z") is not None else "")
              + f", net {num(soc['net_sentiment'], 2, sign=True)}, bull share "
              f"{float(soc['bull_share']):.0%} · {chip(o.get('agreement'), 'neutral')}"
              + (f" {chip('crowded ' + str(o['crowding']), 'warn')}" if o.get("crowding")
                 else "") + "</p>")
    rx = o.get("reaction")
    react = (f"<p><b>Day 0</b>: {_pct(rx['ret_pct'])} (excess {_pct(rx['abnormal_pct'])})"
             + (f" on {num(rx['rvol'], 1)}x volume" if rx.get("rvol") else "") + "</p>"
             if rx else "")
    priced = ("<p>" + chip("largely priced in", "warn") + " the day-0 move already exceeds "
              "the usual range</p>") if o.get("priced_in") else ""
    sc = "".join(f"<li>{e(x)}</li>" for x in o.get("scenarios", []))
    return (f"<p class='small'>{e(o.get('headline'))}</p>"
            f"<p class='small muted'>Strength factors: {e(_factors(o))}</p>"
            + react + priced + social + _horizons(o) + plan
            + (f"<ul class='small'>{sc}</ul>" if sc else "")
            + "<details><summary>Confluence checks</summary>" + _confluence(o) + "</details>")


def _row(o: dict[str, Any]) -> dict[str, Any]:
    view = o.get("view") or {}
    t = o.get("tally") or {}
    best = next((v for v in o.get("horizons", []) if v["h"] == view.get("horizon")), None)
    return {"ticker": o["ticker"], "strength": o["strength"], "label": o["label"],
            "type": f"{o['ctype'].replace('_', ' ').lower()} ({o['direction']})",
            "stance": view.get("stance"),
            "vehicle": VEHICLES.get(view.get("vehicle", ""), view.get("vehicle")),
            "best": (f"{hname(best['h'])}: {float(best['p_up']):.0%} up, "
                     f"{_pct(best['median_pct'])}") if best else "—",
            "confluence": f"{t.get('supports', 0)}+ / {t.get('against', 0)}−",
            "detail": _detail(o)}


def page(latest: dict[str, Any] | None, ev: dict[str, Any] | None, csrf: str) -> str:
    run = cmd_button(csrf, "CATALYST", "", "Run catalysts now", primary=True)
    sub = "How strong is the news, what may follow in 1 day to 1 month, and what fits"
    if not latest:
        return (page_head("News catalysts", sub, run)
                + card("Live catalysts", empty(
                    "No catalyst run yet",
                    "trader-core runs at 8:45 AM, through the session and at 4:25 PM ET. "
                    f"Press <b>Run catalysts now</b>, or run {code('./run.sh catalyst')}. "
                    f"For measured ranges, run {code('./run.sh backfill-catalysts 365')} once.",
                    run, "✶"))
                + card("Track record", _track(ev)))
    outs = latest.get("outlooks", [])
    gate = latest.get("gate") or {}
    head = (f"As of {e(latest.get('as_of'))} · generated "
            f"{ts(latest.get('generated'), '%m-%d %H:%M')} · "
            + ("calibrated" if gate.get("pass") else "UNCALIBRATED: the track record is still "
               "building"))
    stances = [((o.get("view") or {}).get("stance")) for o in outs]
    body = page_head("News catalysts", head, run) + tiles([
        tile("Live catalysts", str(len(outs)), "strength ≥ threshold or a hard event"),
        tile("Bullish", str(stances.count("BULLISH")), "stock or call spread", tone="ok"),
        tile("Bearish", str(stances.count("BEARISH")), "put spreads, never short",
             tone="bad"),
        tile("No trade", str(stances.count("NO_TRADE") + stances.count("NEUTRAL")),
             "weak, unclear or event risk"),
        tile("Alerts", "on" if gate.get("pass") else "off", "Telegram needs the gate (NC-34)")])
    for n in latest.get("notes", []):
        body += f'<p class="muted small">{e(n)}</p>'
    rows = [_row(o) for o in outs]
    body += card("Live catalysts", table(rows, [
        ("ticker", "Ticker"), ("strength", "Strength"), ("label", ""), ("type", "Catalyst"),
        ("stance", "View"), ("vehicle", "Vehicle"), ("best", "Best horizon"),
        ("confluence", "Checks"), ("detail", "Details")], "no live catalyst", {
        "ticker": lambda v, row: stock_link(v),
        "strength": lambda v, row: num(v, 0),
        "label": lambda v, row: chip(v, LABEL_TONE.get(v, "neutral")),
        "stance": lambda v, row: chip(v, STANCE_TONE.get(v, "neutral")),
        "detail": lambda v, row: f"<details><summary>open</summary>{v}</details>"}) if rows
        else empty("No live catalyst", "No story on the watchlist is strong enough right "
                   "now, and there is no hard event in the last 72 hours.", icon="✓"),
        sub=DISCLAIMER)
    body += card("Track record", _track(ev or {"horizons": latest.get("track")}))
    return body


def _track(ev: dict[str, Any] | None) -> str:
    hs = (ev or {}).get("horizons") or {}
    rows = [{"h": hname(int(h)), "graded": m.get("graded", 0), "brier": m.get("brier"),
             "coverage": m.get("coverage"),
             "bull": ((m.get("stance") or {}).get("BULLISH") or {}).get("avg_excess_pct")}
            for h, m in sorted(hs.items(), key=lambda x: int(x[0]))]
    if not any(r["graded"] for r in rows):
        return empty("No graded outlooks yet",
                     f"Run {code('./run.sh backfill-catalysts 365')}: it loads a year of news "
                     "and grades every past outlook walk-forward (no look-ahead).", icon="◌")
    gate = (ev or {}).get("gate") or {}
    adj = ", ".join((ev or {}).get("adjustments") or []) or "none"
    return (table(rows, [("h", "Horizon"), ("graded", "Graded"), ("brier", "Brier (coin 0.25)"),
                         ("coverage", "Range hit (target 80%)"),
                         ("bull", "Avg excess after BULLISH %")], "", {
        "brier": lambda v, row: num(v, 3), "coverage": lambda v, row: num(v, 2),
        "bull": lambda v, row: num(v, 2, sign=True)}, compact=True)
        + f"<p class='small muted'>Adjustments kept by the walk-forward: {e(adj)} · alert "
          f"gate: {'pass' if gate.get('pass') else 'not passed'} · events "
          f"{e((ev or {}).get('events', '—'))}</p>")


def home_card(latest: dict[str, Any] | None) -> str:
    """Today page: the strongest live catalysts."""
    from trader.console.render import link

    if not latest or not latest.get("outlooks"):
        return card("News catalysts", '<p class="muted small">No live catalyst.</p>',
                    actions=link("/catalysts", "Open →"))
    rows = [_row(o) for o in latest["outlooks"][:3]]
    return card("News catalysts", table(rows, [
        ("ticker", "Ticker"), ("strength", "Strength"), ("type", "Catalyst"),
        ("stance", "View"), ("best", "Best horizon")], "", {
        "ticker": lambda v, row: stock_link(v), "strength": lambda v, row: num(v, 0),
        "stance": lambda v, row: chip(v, STANCE_TONE.get(v, "neutral"))}, compact=True),
        sub=f"As of {e(latest.get('as_of'))}", actions=link("/catalysts", "All catalysts →"))
