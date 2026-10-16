"""Technical scan page (SCANNER_REQUIREMENTS TS-19, UI_REQUIREMENTS UI-37).

Reads ``reports/technical_scan.json`` (written by trader-core at 16:20 ET, by the SCAN
command and by ``trader scan``) and ``reports/scanner_eval.json`` (``trader scan-eval``).
Shows every idea with its decision checks, the day watchlist and the technique scorecard.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from trader.console.render import (
    card,
    chip,
    cmd_button,
    code,
    e,
    empty,
    link,
    num,
    page_head,
    stock_link,
    table,
    tile,
    tiles,
    ts,
)
from trader.scanner.checks import CATALOG

TONE = {"go": "ok", "caution": "warn", "blocked": "bad", "watch": "neutral", "options": "info"}
LABEL = {"go": "GO", "caution": "CAUTION", "blocked": "BLOCKED", "watch": "WATCH",
         "options": "OPTIONS"}


def verdict_chip(v: str | None) -> str:
    return chip(LABEL.get(v or "", "—"), TONE.get(v or "", "neutral"))


def _check_chip(ok: bool | None) -> str:
    return chip("unknown") if ok is None else chip("pass", "ok") if ok else chip("fail", "bad")


def _summary(idea: dict[str, Any]) -> str:
    known = [c for c in idea.get("checks", []) if c["ok"] is not None]
    passed = sum(1 for c in known if c["ok"])
    failed = [c["label"] for c in known if not c["ok"] and c["key"] != "direction"]
    s = f"{passed}/{len(known)} pass"
    return e(s) + ("" if not failed else
                   '<br><span class="muted small">' + e("; ".join(failed)) + "</span>")


def _details(idea: dict[str, Any]) -> str:
    rows = [{"check": c["label"], "result": c["ok"], "detail": c["detail"],
             "rule": c["rule"], "hard": "hard" if c["hard"] else "soft"}
            for c in idea.get("checks", [])]
    tbl = table(rows, [("check", "Check"), ("result", "Result"), ("detail", "Detail"),
                       ("hard", "Kind"), ("rule", "Rule")], "no checks ran", {
        "result": lambda v, row: _check_chip(v)}, compact=True)
    why = f'<p class="small">{e(idea.get("why"))}</p>'
    plan = f'<p class="small">{e(idea["plan"])}</p>' if idea.get("plan") else ""
    return (f'<details><summary>{e(idea["symbol"])} {e(idea["technique"])}: why and all '
            f'{len(rows)} checks</summary>{why}{plan}{tbl}</details>')


def price(v: Any, row: dict[str, Any] | None = None) -> str:
    """Trade levels always to the cent (the generic cell format keeps 4 digits)."""
    return num(v, 2)


def _ideas_table(rows: list[dict[str, Any]], bearish: bool = False) -> str:
    cols = [("verdict", "Verdict"), ("score", "Score"), ("symbol", "Ticker"),
            ("technique", "Technique"), ("trigger", "Level" if bearish else "Entry"),
            ("stop", "Invalidation" if bearish else "Stop"), ("t1", "T1"), ("t2", "T2"),
            ("risk", "R / share"), ("room_r", "Room (R)")]
    if not bearish:
        cols.append(("shares", "Size"))
    cols.append(("checks", "Checks"))
    if bearish:
        cols.append(("options", "Express it"))

    def entry(v: Any, row: dict[str, Any]) -> str:
        if bearish:
            return num(v)
        kind = "next open, ref" if row.get("entry") == "open" else "buy-stop"
        return f'{num(v)}<br><span class="muted small">{kind}</span>'

    def tech(v: Any, row: dict[str, Any]) -> str:
        conf = row.get("confluence") or []
        return e(v) + ("" if not conf else
                       '<br><span class="muted small">+ ' + e(", ".join(conf)) + "</span>")

    def size(v: Any, row: dict[str, Any]) -> str:
        if v is None:
            return '<span class="muted small">sized by trader-core</span>'
        risk = num(row.get("risk_usd"), 0)
        return f'{int(v)} sh<br><span class="muted small">${risk} risk</span>'

    return table(rows, cols, "none", {
        "verdict": lambda v, row: verdict_chip(v),
        "symbol": lambda v, row: stock_link(v),
        "technique": tech, "trigger": entry, "shares": size,
        "stop": price, "t1": price, "t2": price, "risk": price,
        "room_r": lambda v, row: "open sky" if v is None else num(v, 1),
        "checks": lambda v, row: _summary(row),
        "options": lambda v, row: link(
            f"/options?preview={quote(row['symbol'])}&dir=bear&style=swing", "Puts preview →"),
    }) + "".join(_details(x) for x in rows)


def _scorecard(ev: dict[str, Any] | None, disabled: tuple[str, ...]) -> str:
    if not ev:
        return empty("No technique grades yet",
                     f"Run {code('./run.sh scan-eval')} in WSL: it grades every technique "
                     "walk-forward on the stored history and feeds the measured edge into the "
                     "score.", icon="▤")
    rep = ev.get("report") or {}
    off = set(disabled)
    rows = []
    for t in rep.get("techniques", []):
        name = f"{t['technique']}:{t['side']}"
        status = ("disabled" if {t["technique"], name} & off else
                  "weak" if t.get("weak") else "measuring" if t["filled"] < rep.get(
                      "min_samples", 30) else "ok")
        rows.append({**t, "status": status})
    tone = {"disabled": "neutral", "weak": "bad", "measuring": "info", "ok": "ok"}
    tbl = table(rows, [("technique", "Technique"), ("side", "Side"), ("ideas", "Ideas"),
                       ("filled", "Filled"), ("hit_1r", "Hit +1R"), ("avg_r", "Avg R"),
                       ("total_r", "Total R"), ("profit_factor", "PF"), ("status", "Status")],
                "no graded ideas", {
        "hit_1r": lambda v, row: "—" if v is None else f"{float(v):.0%}",
        "avg_r": lambda v, row: num(v, 2, True),
        "total_r": lambda v, row: num(v, 1, True),
        "profit_factor": lambda v, row: num(v, 2),
        "status": lambda v, row: chip(v, tone.get(v, "neutral"))}, compact=True)
    ip = ev.get("in_play") or {}
    lift = (f'<p class="small">Day watchlist: the top {ip["top_n"]} moved '
            f'{num(ip["top_avg_range_pct"], 2)}% the next day vs {num(ip["all_avg_range_pct"], 2)}'
            f'% for the average ticker (lift {num(ip["lift"], 2)}x, beat the average on '
            f'{float(ip["days_top_beat_avg"]):.0%} of {ip["days"]} days).</p>'
            if ip.get("days") else "")
    buckets = table(rep.get("score_buckets", []), [
        ("bucket", "Score"), ("filled", "Filled"), ("avg_r", "Avg R"), ("hit_1r", "Hit +1R")],
        "", {"avg_r": lambda v, row: num(v, 2, True),
             "hit_1r": lambda v, row: "—" if v is None else f"{float(v):.0%}"}, compact=True)
    return (f'<p class="muted small">Graded {e(str(ev.get("generated", ""))[:16])} on stored '
            f'history; weak = negative average R over {rep.get("min_samples", 30)}+ filled '
            f'ideas. Past results, not a promise.</p>' + tbl + lift
            + "<h4>By score</h4>" + buckets)


def _catalog() -> str:
    rows = [{"label": lb, "kind": "hard: blocks the idea" if hard else "soft: caution",
             "rule": rule, "why": why} for _, lb, hard, rule, why in CATALOG]
    return table(rows, [("label", "Decision"), ("kind", "Kind"), ("rule", "Rule"),
                        ("why", "Why")], "", compact=True)


def page(scan: dict[str, Any] | None, ev: dict[str, Any] | None, csrf: str,
         disabled: tuple[str, ...]) -> str:
    run = cmd_button(csrf, "SCAN", "", "Run scan now", primary=True)
    head_actions = run + " " + link("/ideas", "Next-day ideas", "btn")
    if not scan:
        return (page_head("Technical scan", "Swing ideas and the day watchlist from 9 "
                          "classic techniques, with every trading decision checked",
                          head_actions)
                + card("Latest scan", empty(
                    "No technical scan yet",
                    "trader-core writes it every trading day at 4:20 PM ET. Press "
                    f"<b>Run scan now</b>, or run {code('./run.sh scan')} in WSL.",
                    run, "▦"))
                + card("Technique scorecard", _scorecard(ev, disabled))
                + card("Decisions checked on every idea", _catalog()))
    swing = scan.get("swing", [])
    min_score = int(scan.get("min_score") or 0)
    shown = [x for x in swing if x.get("score", 0) >= min_score]
    by: dict[str, list[dict[str, Any]]] = {}
    for x in shown:
        by.setdefault(x.get("verdict") or "go", []).append(x)
    act = by.get("go", []) + by.get("caution", [])
    sub = (f'As of {e(scan.get("asof"))} · from {e(scan.get("source", "?"))} · generated '
           f'{ts(scan.get("generated"), "%m-%d %H:%M")} · score ≥ {min_score}')
    counts = tiles([
        tile("Go", str(len(by.get("go", []))), "all hard checks pass", tone="ok"),
        tile("Caution", str(len(by.get("caution", []))), "2+ soft checks fail", tone="warn"),
        tile("Bearish (options)", str(len(by.get("options", []))), "puts, never short stock"),
        tile("Blocked", str(len(by.get("blocked", []))), "a rule says no", tone="bad"),
        tile("Day watchlist", str(len(scan.get("in_play", []))), "ranked by expected range")])
    note = (f'<p class="muted small">{e(scan["context_note"])}</p>'
            if scan.get("context_note") else "")
    body = page_head("Technical scan", sub, head_actions) + counts + note
    body += card("Swing ideas", _ideas_table(act) if act else empty(
        "No actionable swing idea", "Nothing passed the score and the hard checks today. "
        "No trade is a position (NT-1).", icon="✓"),
        sub="Next-session entries. Open a row's details for every check and its rule.")
    if by.get("options"):
        body += card("Bearish views (options only)", _ideas_table(by["options"], bearish=True),
                     sub="The stock side is long-only (ADR-008): express these with puts or "
                         "put spreads on the Options page.")
    if by.get("watch"):
        body += card("Watch only", _ideas_table(by["watch"]),
                     sub="Not on universe.tickers: research only (UR-1).")
    if by.get("blocked"):
        rows = [{**x, "rule": "; ".join(f'{c["label"]}: {c["detail"]}' for c in x["checks"]
                                        if c["hard"] and c["ok"] is False)}
                for x in by["blocked"]]
        body += card("Blocked by a rule", table(rows, [
            ("symbol", "Ticker"), ("technique", "Technique"), ("score", "Score"),
            ("rule", "Why it is blocked")], "", {
            "symbol": lambda v, row: stock_link(v)}))
    plays = scan.get("in_play", [])
    bias_tone = {"long": "ok", "short": "bad", "neutral": "neutral"}
    body += card("Day watchlist", table(plays, [
        ("rank", "#"), ("symbol", "Ticker"), ("expected_range_pct", "Exp. range %"),
        ("atr_pct", "ATR %"), ("rvol", "RVOL"), ("bias", "Bias"), ("levels", "Levels"),
        ("plan", "Plan")], "no data", {
        "symbol": lambda v, row: stock_link(v),
        "expected_range_pct": lambda v, row: num(v, 1),
        "bias": lambda v, row: chip(v, bias_tone.get(v, "neutral")),
        "levels": lambda v, row: e(
            f'PDH {num(v["prior_high"])} · PDL {num(v["prior_low"])} · PDC '
            f'{num(v["prior_close"])} · 20d {num(v["low_20d"])}-{num(v["high_20d"])}')}),
        sub="In play next session. Entries come from the intraday playbook (ORB, VWAP) and "
            "its checklist; the bias favours one side.")
    if scan.get("skipped"):
        body += ('<p class="muted small">Skipped: ' + e("; ".join(
            f"{k} ({v})" for k, v in sorted(scan["skipped"].items()))) + "</p>")
    body += card("Technique scorecard", _scorecard(ev, disabled),
                 sub=f"scanner.disabled: {', '.join(disabled) or 'none'}")
    body += card("Decisions checked on every idea", _catalog(),
                 sub="Hard checks block an idea; two or more soft failures make it a caution.")
    return body


def home_card(scan: dict[str, Any] | None) -> str:
    """Today page: the top actionable ideas (UI-37)."""
    if not scan:
        return card("Technical scan", '<p class="muted small">No scan yet: trader-core '
                    'writes it at 4:20 PM ET.</p>', actions=link("/technical", "Open →"))
    min_score = int(scan.get("min_score") or 0)
    act = [x for x in scan.get("swing", []) if x.get("verdict") in ("go", "caution")
           and x.get("score", 0) >= min_score][:3]
    plays = scan.get("in_play", [])[:3]
    ideas = table(act, [("verdict", ""), ("symbol", "Ticker"), ("technique", "Technique"),
                        ("trigger", "Entry"), ("stop", "Stop"), ("score", "Score")],
                  "no actionable swing idea", {
        "verdict": lambda v, row: verdict_chip(v),
        "symbol": lambda v, row: stock_link(v), "trigger": price, "stop": price},
        compact=True)
    watch = ('<p class="small">In play next: ' + ", ".join(
        f'{stock_link(p["symbol"])} {num(p["expected_range_pct"], 1)}%' for p in plays)
        + "</p>") if plays else ""
    return card("Technical scan", ideas + watch, sub=f'As of {e(scan.get("asof"))}',
                actions=link("/technical", "All ideas and checks →"))
