"""Server-rendered HTML for the Owner Console: a small component system (sidebar layout,
KPI tiles, cards, chips, heatmap, bar chart, empty states) and the pages built from it.

Rules: everything dynamic is escaped; no inline scripts or styles (CSP); colour is never the
only signal (UI-25: chips carry an icon and a word, deltas carry an arrow and a sign); times
in US Eastern and money in dollars and cents (UI-C7). Chart colours come from the validated
palette in app.css (dataviz reference palette: blue/red diverging pair, gray midpoint).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from html import escape
from typing import Any
from urllib.parse import quote

from trader.timeutil import to_et

# (group, [(href, label)]) - the sidebar
NAV: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Overview", (("/", "Today"), ("/market", "Market recap"), ("/scanner", "Scanner"))),
    ("Research", (("/stock", "Stock lookup"), ("/ratings", "Analyst ratings"),
                  ("/news", "News"), ("/technical", "Technical scan"),
                  ("/catalysts", "Catalysts"),
                  ("/ideas", "Next-day ideas"), ("/options", "Options"),
                  ("/ask", "Ask"))),
    ("Trading", (("/approvals", "Approvals"), ("/controls", "Controls"),
                 ("/copilot", "Co-pilot"), ("/stories", "Trades"))),
    ("Review", (("/notes", "Journal"), ("/latency", "Latency"), ("/alerts", "Alerts"),
                ("/compliance", "Compliance"), ("/reports", "Reports"), ("/runs", "Runs"))),
    ("System", (("/config", "Config"), ("/model", "Model"))),
)
TAGS = ("followed the plan", "FOMO", "hesitated", "early exit", "late entry", "moved stop")
ICON = {"ok": "✓", "warn": "!", "bad": "✕", "info": "•", "neutral": "•"}


# --- formatting --------------------------------------------------------------------------------

def e(x: Any) -> str:
    return escape("" if x is None else str(x))


def money(x: Any) -> str:
    if x is None or x == "":
        return "—"
    try:
        v = Decimal(str(x))
    except Exception:   # noqa: BLE001
        return e(x)
    return f"${v:,.2f}" if v >= 0 else f"-${-v:,.2f}"


def num(x: Any, nd: int = 2, sign: bool = False) -> str:
    if x is None or x == "":
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return e(x)
    return f"{v:+,.{nd}f}" if sign else f"{v:,.{nd}f}"


def ts(x: Any, fmt: str = "%H:%M:%S") -> str:
    if x is None:
        return "—"
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x)
        except ValueError:
            return e(x)
    if isinstance(x, datetime):
        return e(f"{to_et(x):{fmt}}")
    if isinstance(x, date):
        return e(f"{x:{fmt}}" if "%" in fmt and "H" not in fmt else x.isoformat())
    return e(x)


def delta(v: Any, suffix: str = "%", nd: int = 2) -> str:
    """Signed change with an arrow (direction is never colour alone)."""
    if v is None or v == "":
        return '<span class="muted">—</span>'
    f = float(v)
    cls, arrow = ("up", "▲") if f > 0 else ("down", "▼") if f < 0 else ("flat", "■")
    return f'<span class="delta {cls}">{arrow} {f:+,.{nd}f}{e(suffix)}</span>'


def chip(text: Any, tone: str = "neutral") -> str:
    return f'<span class="chip {tone}"><i>{ICON.get(tone, "•")}</i>{e(text)}</span>'


def badge(level: str | None, text: str | None = None) -> str:
    lv = (level or "unknown").lower()
    tone = {"green": "ok", "amber": "warn", "yellow": "warn", "red": "bad", "ok": "ok",
            "pass": "ok", "fail": "bad"}.get(lv, "neutral")
    return chip(text or lv.upper(), tone)


def passfail(ok: bool | None) -> str:
    if ok is None:
        return chip("n/a")
    return chip("PASS", "ok") if ok else chip("FAIL", "bad")


def link(href: str, text: Any, cls: str = "") -> str:
    c = f' class="{cls}"' if cls else ""
    return f'<a href="{e(href)}"{c}>{e(text)}</a>'


def ext_link(href: str, text: Any, cls: str = "") -> str:
    if not href.startswith(("https://", "http://")):
        return e(text)
    c = f' class="{cls}"' if cls else ""
    return f'<a href="{e(href)}"{c} rel="noopener noreferrer" target="_blank">{e(text)}</a>'


def stock_link(sym: Any) -> str:
    return link(f"/stock?symbol={quote(str(sym))}", sym, "sym")


def csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def cmd_button(csrf: str, name: str, arg: str = "", label: str | None = None,
               danger: bool = False, confirm: str = "", primary: bool = False) -> str:
    conf = f' data-confirm="{e(confirm)}"' if confirm else ""
    cls = "danger" if danger else "primary" if primary else ""
    return (f'<form method="post" action="/cmd" class="inline"{conf}>{csrf_field(csrf)}'
            f'<input type="hidden" name="name" value="{e(name)}">'
            f'<input type="hidden" name="arg" value="{e(arg)}">'
            f'<button class="{cls}">{e(label or (name + " " + arg).strip())}</button></form>')


# --- components ----------------------------------------------------------------------------------

def _cell_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return f"{to_et(v):%m-%d %H:%M}"
    if isinstance(v, float):
        return f"{v:,.4g}"
    if isinstance(v, dict | list):
        t = str(v)
        return t if len(t) <= 120 else t[:117] + "..."
    return v


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def table(rows: Sequence[dict[str, Any]], cols: Sequence[tuple[str, str]] | None = None,
          empty: str = "nothing yet", fmt: dict[str, Any] | None = None,
          compact: bool = False) -> str:
    if not rows:
        return f'<p class="muted small">{e(empty)}</p>'
    cols = list(cols or [(k, k.replace("_", " ")) for k in rows[0].keys()])
    fmt = fmt or {}
    numeric = {k for k, _ in cols if any(_is_num(r.get(k)) for r in rows)}
    head = "".join(f'<th class="{"num" if k in numeric else ""}">{e(h)}</th>' for k, h in cols)
    body = []
    for row in rows:
        cells = []
        for k, _ in cols:
            v = row.get(k)
            html_v = fmt[k](v, row) if k in fmt else e(_cell_value(v))
            cells.append(f'<td class="{"num" if k in numeric else ""}">{html_v}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class="tbl"><table class="{"compact" if compact else ""}"><thead><tr>{head}'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def card(title: str, body: str, *, sub: str = "", actions: str = "", cls: str = "") -> str:
    head = (f'<div class="card-head"><div><h3>{e(title)}</h3>'
            + (f'<p class="sub">{e(sub)}</p>' if sub else "") + "</div>"
            + (f'<div class="actions">{actions}</div>' if actions else "") + "</div>")
    return f'<section class="card {cls}">{head}{body}</section>'


def tile(label: str, value: str, sub: str = "", key: str = "", tone: str = "") -> str:
    k = f' data-k="{e(key)}"' if key else ""
    return (f'<div class="tile {tone}"><div class="label">{e(label)}</div>'
            f'<div class="value"{k}>{value}</div>'
            + (f'<div class="tsub">{sub}</div>' if sub else "") + "</div>")


def tiles(items: Iterable[str]) -> str:
    return f'<div class="tiles">{"".join(items)}</div>'


def meter(frac: float | None, tone: str = "") -> str:
    f = 0.0 if frac is None else max(0.0, min(1.0, frac))
    return (f'<div class="meter {tone}" role="meter" aria-valuenow="{f * 100:.0f}" '
            f'aria-valuemin="0" aria-valuemax="100"><svg viewBox="0 0 100 6" '
            f'preserveAspectRatio="none"><rect class="track" x="0" y="0" width="100" '
            f'height="6" rx="3"/><rect class="fill" x="0" y="0" width="{f * 100:.1f}" '
            f'height="6" rx="3"/></svg></div>')


def empty(title: str, why: str, actions: str = "", icon: str = "◌") -> str:
    """A section with nothing to show says why and what fills it (UI-35)."""
    return (f'<div class="empty"><div class="icon">{icon}</div><div><h4>{e(title)}</h4>'
            f'<p>{why}</p>' + (f'<div class="actions">{actions}</div>' if actions else "")
            + "</div></div>")


def code(text: str) -> str:
    return f"<code>{e(text)}</code>"


def page_head(title: str, sub: str = "", actions: str = "") -> str:
    return (f'<div class="page-head"><div><h1>{e(title)}</h1>'
            + (f'<p class="sub">{sub}</p>' if sub else "") + "</div>"
            + (f'<div class="actions">{actions}</div>' if actions else "") + "</div>")


def _hm_class(v: float | None) -> str:
    if v is None:
        return "hm-na"
    a = abs(v)
    lvl = 0 if a < 0.25 else 1 if a < 1 else 2 if a < 2.5 else 3
    if lvl == 0:
        return "hm-0"
    return f"hm-{'up' if v > 0 else 'dn'}{lvl}"


def heatmap(items: Sequence[dict[str, Any]], legend: str = "day % change") -> str:
    """Tiles coloured by a diverging blue (up) / red (down) scale with a gray midpoint;
    every tile also prints its signed value (colour never alone)."""
    if not items:
        return ""
    cells = []
    for it in items:
        v = it.get("value")
        sub = it.get("sub", "")
        title = f"{it['ticker']}: {num(v, 2, True)}%" + (f" · {sub}" if sub else "")
        cells.append(f'<a class="hm {_hm_class(v)}" href="{e(it.get("href", "#"))}" '
                     f'title="{e(title)}"><b>{e(it["ticker"])}</b>'
                     f'<span>{num(v, 2, True) if v is not None else "—"}%</span>'
                     + (f'<small>{e(sub)}</small>' if sub else "") + "</a>")
    scale = "".join(f'<i class="hm {c}"></i>' for c in
                    ("hm-dn3", "hm-dn2", "hm-dn1", "hm-0", "hm-up1", "hm-up2", "hm-up3"))
    return (f'<div class="heat">{"".join(cells)}</div><div class="scale"><span>down</span>'
            f'{scale}<span>up</span><span class="muted">· {e(legend)}: ±0.25 / 1 / 2.5%'
            f'</span></div>')


def hbars(rows: Sequence[tuple[str, float | None, str]], unit: str = "%", title: str = "",
          w: int = 560) -> str:
    """Horizontal bars from a zero baseline (polarity: blue up, red down). Labels and values
    are HTML; each bar is a stretch-only SVG so text never scales with the chart. The value is
    printed on every row and each bar carries a native tooltip. ``rows``: (label, value, href).
    """
    del w
    data = [(lab, v, h) for lab, v, h in rows if v is not None]
    if not data:
        return ""
    lim = max(abs(v) for _, v, _ in data) or 1.0
    out = [f'<div class="hbars" role="img" aria-label="{e(title or "bar chart")}">']
    for lab, v, href in data:
        half = abs(v) / lim * 100
        x = 100 if v >= 0 else 100 - half
        cls = "bar-up" if v >= 0 else "bar-dn"
        out.append(
            f'<a class="brow" href="{e(href)}"><span class="blab">{e(lab)}</span>'
            f'<svg viewBox="0 0 200 10" preserveAspectRatio="none"><line class="axis" x1="100" '
            f'x2="100" y1="0" y2="10"/><rect class="{cls}" x="{x:.2f}" y="1" '
            f'width="{max(half, 0.6):.2f}" height="8"><title>{e(lab)}: {v:+.2f}{e(unit)}'
            f'</title></rect></svg><span class="bval">{v:+.1f}{e(unit)}</span></a>')
    out.append("</div>")
    return "".join(out)


def rating_list_html(rows: Sequence[dict[str, Any]], limit: int = 8, show_ticker: bool = True
                     ) -> str:
    """Compact one-line-per-action list for narrow cards."""
    items = []
    for r in rows[:limit]:
        change = (f"{r['from_rating']} → {r['to_rating']}" if r.get("from_rating")
                  and r.get("to_rating") else r.get("to_rating") or "")
        pt = (f"PT ${r['pt_old']:g} → ${r['pt_new']:g}" if r.get("pt_old") and r.get("pt_new")
              else f"PT ${r['pt_new']:g}" if r.get("pt_new") else "")
        what = " · ".join(x for x in (change, pt) if x)
        items.append(
            f'<li><span class="muted small">{ts(r["ts"], "%b %d")}</span> '
            + (stock_link(r["ticker"]) + " " if show_ticker else "")
            + chip(ACTION_LABEL.get(r["action"], r["action"]),
                   ACTION_TONE.get(r["action"], "neutral"))
            + f' <b>{e(r["firm"])}</b> <span>{e(what)}</span></li>')
    return f'<ul class="plain news">{"".join(items)}</ul>'


def tag_chips(tags: Iterable[str]) -> str:
    tone = {"leader": "info", "near 20d high": "info", "upgraded": "ok", "laggard": "neutral",
            "oversold": "warn", "downgraded": "bad", "earnings soon": "warn"}
    return " ".join(chip(t, tone.get(t, "neutral")) for t in tags)


ACTION_TONE = {"upgrade": "ok", "downgrade": "bad", "initiate": "info", "reiterate": "neutral",
               "pt_raise": "ok", "pt_cut": "bad"}
ACTION_LABEL = {"upgrade": "Upgrade", "downgrade": "Downgrade", "initiate": "Initiated",
                "reiterate": "Reiterated", "pt_raise": "Target raised", "pt_cut": "Target cut"}


def rating_rows_html(rows: Sequence[dict[str, Any]], limit: int = 200, show_ticker: bool = True
                     ) -> str:
    cols = ([("ts", "When")] + ([("ticker", "Ticker")] if show_ticker else [])
            + [("action", "Action"), ("firm", "Firm"), ("change", "Rating"),
               ("pt", "Price target"), ("headline", "Headline")])
    data = []
    for r in rows[:limit]:
        change = (f"{r['from_rating']} → {r['to_rating']}" if r.get("from_rating")
                  and r.get("to_rating") else r.get("to_rating") or "")
        pt = (f"${r['pt_old']:g} → ${r['pt_new']:g}" if r.get("pt_old") and r.get("pt_new")
              else f"${r['pt_new']:g}" if r.get("pt_new") else "")
        data.append({**r, "change": change, "pt": pt})
    return table(data, cols, "no analyst actions", {
        "ts": lambda v, row: ts(v, "%b %d %H:%M"),
        "ticker": lambda v, row: stock_link(v),
        "action": lambda v, row: chip(ACTION_LABEL.get(v, v), ACTION_TONE.get(v, "neutral")),
        "headline": lambda v, row: ext_link(row.get("url") or "", (v or "")[:140])})


# --- layout ----------------------------------------------------------------------------------

def layout(title: str, body: str, *, path: str = "/", env: str | None = None,
           csrf: str = "", flash: str = "", logged_in: bool = True) -> str:
    live = env == "live"
    mode = (f'<span class="mode {"live" if live else "paper"}" data-k="environment">'
            f'{"LIVE" if live else "PAPER"}</span>') if env else ""
    nav = ""
    if logged_in:
        groups = []
        for g, items in NAV:
            links = "".join(f'<a href="{h}" class="{"on" if h == path else ""}">{e(t)}</a>'
                            for h, t in items)
            groups.append(f'<div class="group"><div class="gname">{e(g)}</div>{links}</div>')
        nav = f'<nav class="side">{"".join(groups)}</nav>'
    tools = (f'<form method="get" action="/stock" class="search"><input id="search" '
             f'name="symbol" placeholder="Any symbol  ( / )" maxlength="6" autocomplete="off">'
             f'</form><button id="theme" type="button" title="light/dark theme">◐</button>'
             f'<form method="post" action="/logout" class="inline">{csrf_field(csrf)}'
             f'<button>Log out</button></form>') if logged_in else ""
    fl = f'<div class="flash">{e(flash)}</div>' if flash else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(title)} · Owner Console</title>'
            f'<link rel="stylesheet" href="/static/app.css"><script src="/static/app.js" defer>'
            f'</script></head><body class="{"app" if logged_in else "bare"}">'
            f'<header class="top"><a class="brand" href="/">◆ Owner Console</a>{mode}'
            f'<div class="spacer"></div>{tools}</header>{nav}<main>{fl}{body}'
            f'<footer>Times US Eastern · numbers come from the journal, trader-core and the '
            f'data feeds, never from the model · not advice</footer></main></body></html>')


def login_page(error: str = "") -> str:
    err = f'<p class="flash">{e(error)}</p>' if error else ""
    return layout("Log in", f'<div class="login">{err}<form method="post" action="/login" '
                  'class="card"><h2>Owner Console</h2><label>Password <input type="password" '
                  'name="password" autofocus autocomplete="current-password"></label>'
                  '<button class="primary">Log in</button><p class="muted small">The password '
                  'is in secrets/console_password.txt</p></form></div>', logged_in=False)


# --- shared blocks -----------------------------------------------------------------------------

def health_list(st: dict[str, Any]) -> str:
    h = st.get("health") or {}
    det = st.get("health_detail") or {}
    if not h:
        return empty("No health data yet", "trader-core reports component health every 30 s "
                     "once it runs.")
    return '<ul class="plain">' + "".join(
        f'<li>{badge(v, k)} <span class="muted small" data-k="health.{e(k)}">'
        f'{e(det.get(k, ""))}</span></li>' for k, v in sorted(h.items())) + "</ul>"


def services(beats: dict[str, datetime], llm: dict[str, Any], now: datetime) -> str:
    items = []
    for svc in ("core", "news", "notifier"):
        t = beats.get(svc)
        age = (now - t).total_seconds() if t else None
        lv = "green" if age is not None and age < 120 else "amber" if age is not None and \
            age < 600 else "red"
        items.append(f'<li>{badge(lv, svc)} <span class="muted small">'
                     + e(f"heartbeat {age:.0f} s ago" if age is not None else "no heartbeat in 1 h")
                     + '</span></li>')
    vram = llm.get("vram_gb")
    items.append(f'<li>{chip(llm.get("model") or "model", "info")} <span class="muted small">'
                 f'loaded {e(", ".join(llm.get("loaded") or []) or "no")} · VRAM '
                 f'{e(f"{vram:.1f} GB" if vram is not None else "n/a")} of 8 GB · p95 '
                 f'{e(llm.get("p95_ms") or "—")} ms ({e(llm.get("calls_today", 0))} calls)'
                 f'{" · " + e(llm["error"]) if llm.get("error") else ""}</span></li>')
    return '<ul class="plain">' + "".join(items) + "</ul>"


def notes_list(rows: Iterable[dict[str, Any]]) -> str:
    items = "".join(f'<li>{ts(r["ts"], "%m-%d %H:%M")} '
                    + " ".join(chip(t) for t in r.get("tags") or [])
                    + f' {e(r["note"])}{" (screenshot)" if r.get("screenshot_b64") else ""}</li>'
                    for r in rows)
    return f'<ul class="plain">{items or "<li class=muted>none yet</li>"}</ul>'


def note_form(csrf: str, ref_type: str, ref_id: str) -> str:
    boxes = "".join(f'<label class="chk"><input type="checkbox" name="tags" value="{e(t)}">'
                    f'{e(t)}</label>' for t in TAGS)
    return (f'<form method="post" action="/notes" enctype="multipart/form-data" class="stack">'
            f'{csrf_field(csrf)}<input type="hidden" name="ref_type" value="{e(ref_type)}">'
            f'<input type="hidden" name="ref_id" value="{e(ref_id)}"><div>{boxes}</div>'
            f'<label>Note <textarea name="note" maxlength="4000" rows="3"></textarea></label>'
            f'<label>Screenshot <input type="file" name="screenshot" accept="image/png,'
            f'image/jpeg"></label><div><button class="primary">Save note</button></div></form>')


def checklist_table(items: list[dict[str, Any]]) -> str:
    return table(items, [("id", "Item"), ("passed", "Result"), ("detail", "Detail")],
                 fmt={"passed": lambda v, r: passfail(v)})


def recap_hint(csrf: str) -> str:
    return (cmd_button(csrf, "RECAP", "5", "Build week recap", primary=True) + " "
            + cmd_button(csrf, "RECAP", "21", "Build month recap"))


def backfill_hint() -> str:
    return (f'For the past month at once, run {code("./run.sh backfill")} in WSL: news, '
            f'analyst ratings, the recap and next-session ideas.')
