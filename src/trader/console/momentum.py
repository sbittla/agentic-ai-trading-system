"""Momentum page (MO-20): regime, buy-stops, tracked signals, the rotation model, leaders
near their 200-day average (MO-27), the leaderboard, company events ahead (EV-7) and the
walk-forward record. Reads reports/momentum.json, reports/momentum_eval.json and
reports/events.json. Advisory: nothing here places an order."""

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

REGIME_TONE = {"ON": "ok", "CAUTION": "warn", "OFF": "bad", "UNKNOWN": "neutral"}
VERDICT_TONE = {"alert": "ok", "unproven": "neutral", "earnings": "warn", "no_slot": "warn",
                "correlated": "warn", "busy": "neutral", "regime_off": "bad",
                "watch_only": "neutral"}


def _pct(v: Any) -> str:
    return "—" if v is None else f"{float(v) * 100:.0f}%"


def _record(ev: dict[str, Any] | None) -> str:
    if not ev or not ev.get("setups"):
        return empty("Not graded yet", "Run " + code("./run.sh momentum-history") + " once, then "
                     + code("./run.sh momentum-eval") + ". Until then no entry alert is sent "
                     "(MO-14).", icon="◌")
    rows = []
    for name, r in ev["setups"].items():
        pol = r.get("default_policy")
        h = (r.get("policies") or {}).get(pol) or {}
        a = h.get("all") or {}
        rows.append({"setup": name, "policy": pol, "gate": (r.get("gate") or {}).get("pass"),
                     "n": a.get("trades", 0), "win": a.get("win_rate"), "avg": a.get("avg_r"),
                     "pf": a.get("profit_factor"), "is": (h.get("is") or {}).get("avg_r"),
                     "oos": (h.get("oos") or {}).get("avg_r")})
    body = table(rows, [("setup", "Setup"), ("policy", "Exit"), ("gate", "Gate"),
                        ("n", "Trades"), ("win", "Win"), ("avg", "Avg R"), ("pf", "PF"),
                        ("is", "Avg R in-sample"), ("oos", "Avg R out-of-sample")], "", {
        "gate": lambda v, row: chip("alerts" if v else "unproven", "ok" if v else "neutral"),
        "win": lambda v, row: _pct(v), "avg": lambda v, row: num(v, 2, sign=True),
        "is": lambda v, row: num(v, 2, sign=True), "oos": lambda v, row: num(v, 2, sign=True)},
        compact=True)
    rot = ev.get("rotation") or {}
    sel = rot.get("selected") or {}
    run = (rot.get("runs") or {}).get(sel.get("name") or "") or {}
    bench = ev.get("benchmarks") or {}

    def row(label: str, st: dict[str, Any]) -> dict[str, Any]:
        i, o = st.get("is") or {}, st.get("oos") or {}
        return {"name": label, "is_cagr": i.get("cagr_pct"), "is_dd": i.get("max_dd_pct"),
                "oos_cagr": o.get("cagr_pct"), "oos_sh": o.get("sharpe"),
                "oos_dd": o.get("max_dd_pct")}
    if run:
        cmp_rows = [row(f"Rotation ({', '.join(sel.get('factors') or [])})", run)]
        cmp_rows += [row(k.replace("_", " "), v) for k, v in bench.items()]
        body += table(cmp_rows, [("name", ""), ("is_cagr", "CAGR in-sample %"),
                                 ("is_dd", "Max DD in-sample %"),
                                 ("oos_cagr", "CAGR out-of-sample %"),
                                 ("oos_sh", "Sharpe out-of-sample"),
                                 ("oos_dd", "Max DD out-of-sample %")], "", {
            k: (lambda v, row: num(v, 1)) for k in ("is_cagr", "is_dd", "oos_cagr", "oos_sh",
                                                    "oos_dd")}, compact=True)
    per = ev.get("period") or {}
    body += (f'<p class="muted small">{e(per.get("start"))} to {e(per.get("end"))}, '
             f'{per.get("symbols")} symbols, out-of-sample from {e(per.get("split"))}. '
             "The universe is today's large caps (survivorship bias): compare with the "
             "equal-weight line, not only SPY.</p>")
    return body


def _sma200(rows: list[dict[str, Any]]) -> str:
    """MO-27: strong stocks within a few percent above a rising 200-day average."""
    for r in rows:
        h = r.get("history") or {}
        r["past"] = (f"{h['tests']} test{'s' if h['tests'] != 1 else ''}, "
                     f"{h['held_rate'] * 100:.0f}% held, median {h['median_r20_pct']:+.1f}% in "
                     "20 sessions") if h.get("tests") else "no past first test"
    return table(rows, [("ticker", "Ticker"), ("close", "Close"), ("sma200", "200-day"),
                        ("dist_pct", "Above it %"), ("atr_away", "ATRs away"),
                        ("from_high_pct", "From 52w high %"), ("rs", "RS"),
                        ("past", "Its past first tests")], "no leader near its 200-day average", {
        "ticker": lambda v, row: stock_link(v), "close": lambda v, row: num(v),
        "sma200": lambda v, row: num(v), "dist_pct": lambda v, row: num(v, 1),
        "atr_away": lambda v, row: num(v, 1), "from_high_pct": lambda v, row: num(v, 1)},
        compact=True)


def _events(evp: dict[str, Any] | None) -> str:
    """EV-7: upcoming company events, their past pattern and the dated ideas."""
    if not evp or evp.get("error"):
        return empty("No event report yet", (evp or {}).get("error") or (
            "trader-core writes it at 5:05 PM ET, or run " + code("./run.sh events") + "."),
            icon="◌")
    ideas = [i for i in evp.get("ideas") or [] if i["status"] != "passed"]
    body = table(ideas, [("ticker", "Ticker"), ("kind", "Idea"), ("event", "Event"),
                         ("event_date", "Date"), ("enter", "From"), ("exit", "To"),
                         ("mean_pct", "Past move %"), ("same_sign_rate", "Same way"),
                         ("n", "Events"), ("p", "p"), ("basis", "Basis")],
                 "no upcoming event has a consistent past pattern", {
        "ticker": lambda v, row: stock_link(v),
        "kind": lambda v, row: chip(v.replace("_", " ").lower(),
                                    "ok" if row["side"] == "buy" else "warn"),
        "event_date": lambda v, row: e(v) + (" (est.)" if row.get("estimated") else ""),
        "mean_pct": lambda v, row: num(v, 1, sign=True), "same_sign_rate": lambda v, row:
        _pct(v), "p": lambda v, row: num(v, 2)}, compact=True)
    ups = evp.get("upcoming") or []
    for u in ups:
        s = u.get("summary") or {}
        u["into"], u["event_move"], u["after"] = (s.get("run_up", u.get("basis", "")),
                                                  s.get("reaction", ""), s.get("drift", ""))
    body += table(ups, [("event_date", "Date"), ("ticker", "Ticker"), ("event", "Event"),
                        ("sessions_away", "Sessions"), ("into", "Into it"),
                        ("event_move", "Event day"), ("after", "After")], "", {
        "ticker": lambda v, row: stock_link(v),
        "event_date": lambda v, row: e(v) + (" (est.)" if row.get("estimated") else "")},
        compact=True)
    body += (f'<p class="muted small">{evp.get("patterns")} patterns in {evp.get("tests")} '
             f'tests; about {evp.get("expected_false")} could be chance. Moves are net of SPY '
             "and of the stock's usual move. A pattern is a tendency over a handful of events, "
             "not a proven edge.</p>")
    return body


def page(p: dict[str, Any] | None, ev: dict[str, Any] | None, csrf: str,
         evp: dict[str, Any] | None = None) -> str:
    run = cmd_button(csrf, "MOMENTUM", "", "Refresh now", primary=True)
    sub = "Leaders, buy-stops with stops and sizes, sell alerts to the exit, and the rotation model"
    if not p or p.get("error"):
        why = (p or {}).get("error") or ("trader-core writes it at 4:50 PM ET; first run "
                                         + code("./run.sh momentum-history") + ".")
        return (page_head("Momentum", sub, run) + card("Today", empty("No momentum run yet",
                                                                      why, run, "◎"))
                + card("Company events ahead", _events(evp))
                + card("Walk-forward record", _record(ev)))
    reg = p["regime"]
    tr = p.get("tracked") or {}
    alerts = [c for c in p["candidates"] if c["verdict"] == "alert"]
    body = page_head("Momentum", f"As of {e(p['asof'])} · generated "
                     f"{ts(p.get('generated'), '%m-%d %H:%M')}", run) + tiles([
        tile("Regime", e(reg["state"].lower()), f"size ×{reg['size_mult']:g}",
             tone=REGIME_TONE.get(reg["state"], "")),
        tile("Buy-stops", str(len(alerts)), "alerted for the next session",
             tone="ok" if alerts else ""),
        tile("Open signals", str(sum(1 for o in tr.get("open", []) if o.get("alerted")))),
        tile("Proven setups", str(len((p.get("eval") or {}).get("gate_pass") or [])),
             "passed the walk-forward gate")])
    body += f'<p class="muted small">{e("; ".join(reg["reasons"]))}</p>'
    ev_rows = tr.get("events") or []
    if ev_rows:
        body += card("Sell alerts and fills", table(ev_rows, [
            ("ticker", "Ticker"), ("kind", "Action"), ("price", "Price"), ("r", "R"),
            ("setup", "Setup"), ("note", "Why")], "", {
            "ticker": lambda v, row: stock_link(v),
            "kind": lambda v, row: chip(v, "bad" if v in ("STOP", "TRAIL", "TIME") else "warn"),
            "price": lambda v, row: num(v), "r": lambda v, row: num(v, 2, sign=True)},
            compact=True), sub="Decided on the close: act at the next open unless it says live.")
    cands = p["candidates"]
    body += card("Buy-stops for the next session", table(cands, [
        ("ticker", "Ticker"), ("setup", "Setup"), ("verdict", "Status"), ("trigger", "Buy-stop"),
        ("stop", "Stop"), ("risk_pct", "Risk %"), ("shares", "Shares"),
        ("partial_at", "Sell 1/3 at"), ("policy", "Trail"), ("time_stop", "Time stop"),
        ("rs", "RS"), ("template", "Template"), ("why", "Why")], "no setups today", {
        "ticker": lambda v, row: stock_link(v),
        "verdict": lambda v, row: chip(v.replace("_", " "), VERDICT_TONE.get(v, "neutral")),
        "trigger": lambda v, row: num(v), "stop": lambda v, row: num(v),
        "risk_pct": lambda v, row: num(v, 1), "partial_at": lambda v, row: num(v),
        "template": lambda v, row: f"{v}/8"}, compact=True),
        sub="Only setups that passed the gate are alerted; the rest are shown for study.")
    op = tr.get("open") or []
    body += card("Tracked signals", table(op + (tr.get("armed") or []), [
        ("ticker", "Ticker"), ("state", "State"), ("setup", "Setup"), ("entry_price", "Entry"),
        ("stop", "Stop"), ("last", "Last"), ("r", "R"), ("open_frac", "Open"),
        ("alerted", "Alerted")], "nothing tracked yet", {
        "ticker": lambda v, row: stock_link(v), "entry_price": lambda v, row: num(v),
        "stop": lambda v, row: num(v), "last": lambda v, row: num(v),
        "r": lambda v, row: num(v, 2, sign=True), "open_frac": lambda v, row: _pct(v),
        "alerted": lambda v, row: chip("yes" if v else "no", "ok" if v else "neutral")},
        compact=True))
    rot = p.get("rotation") or {}
    if rot.get("holdings"):
        head = "rebalance today" if rot.get("due") else f"next rebalance {rot['next_rebalance']}"
        body += card("Rotation model", table(rot["holdings"], [
            ("ticker", "Ticker"), ("action", "Action"), ("weight", "Weight"),
            ("prev_weight", "Was"), ("close", "Close"), ("shares", "Shares")], "", {
            "ticker": lambda v, row: stock_link(v),
            "action": lambda v, row: chip(v, {"BUY": "ok", "SELL": "bad"}.get(v, "neutral")),
            "weight": lambda v, row: _pct(v), "prev_weight": lambda v, row: _pct(v),
            "close": lambda v, row: num(v)}, compact=True),
            sub=f"Top {len([h for h in rot['holdings'] if h['weight'] > 0])} by "
                f"{', '.join(rot['factors'])}, inverse-volatility weights capped at 20% · "
                f"{head} · {'proven' if rot.get('proven') else 'unproven: not alerted'}")
    body += card("Near the 200-day average", _sma200(list(p.get("sma200_watch") or [])),
                 sub="Leaders with a rising 200-day average, within a few percent above it: "
                     "SMA200_PULLBACK arms when the low touches it (watch-only, MO-26).")
    board = p.get("leaderboard") or []
    body += card("Leaders", table(board, [
        ("rank", "#"), ("symbol", "Ticker"), ("composite", "Score"), ("rs", "RS"),
        ("template", "Template"), ("stage", "Stage"), ("close", "Close"), ("adr", "ADR %"),
        ("gap_flag", "Gap > 15%")], "", {
        "symbol": lambda v, row: stock_link(v), "composite": lambda v, row: num(v, 2),
        "close": lambda v, row: num(v), "template": lambda v, row: f"{v}/8",
        "gap_flag": lambda v, row: chip("yes", "warn") if v else ""}, compact=True),
        sub="Ranked on " + e(", ".join(p.get("rank_factors") or [])) + " (MO-5).")
    body += card("Company events ahead", _events(evp),
                 sub="Product launches and developer days: the past price pattern around "
                     "each, and dated ideas where it was consistent (EV-6).")
    body += card("Walk-forward record", _record(ev))
    return body
