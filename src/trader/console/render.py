"""Server-rendered HTML for the Owner Console. Everything dynamic is escaped; no inline
scripts or styles (CSP), colour is never the only signal (UI-25), times in US Eastern and
money in dollars and cents (UI-C7)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from html import escape
from typing import Any
from urllib.parse import quote

from trader.timeutil import to_et

NAV = (
    ("/", "Home"),
    ("/scanner", "Scanner"),
    ("/stock", "Stock"),
    ("/ask", "Ask"),
    ("/approvals", "Approvals"),
    ("/controls", "Controls"),
    ("/stories", "Trades"),
    ("/copilot", "Co-pilot"),
    ("/options", "Options"),
    ("/alerts", "Alerts"),
    ("/ideas", "Ideas"),
    ("/latency", "Latency"),
    ("/notes", "Journal"),
    ("/compliance", "Compliance"),
    ("/reports", "Reports"),
    ("/config", "Config"),
    ("/model", "Model"),
    ("/runs", "Runs"),
)


def e(x: Any) -> str:
    return escape("" if x is None else str(x))


def money(x: Any) -> str:
    if x is None or x == "":
        return "—"
    try:
        v = Decimal(str(x))
    except Exception:  # noqa: BLE001
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
        return e(x.isoformat())
    return e(x)


def badge(level: str | None, text: str | None = None) -> str:
    lv = (level or "unknown").lower()
    cls = {
        "green": "ok",
        "amber": "warn",
        "yellow": "warn",
        "red": "bad",
        "ok": "ok",
        "pass": "ok",
        "fail": "bad",
    }.get(lv, "muted")
    return f'<span class="badge {cls}">{e(text or lv.upper())}</span>'


def passfail(ok: bool | None) -> str:
    if ok is None:
        return badge(None, "n/a")
    return badge("pass", "PASS") if ok else badge("fail", "FAIL")


def table(
    rows: Sequence[dict[str, Any]],
    cols: Sequence[tuple[str, str]] | None = None,
    empty: str = "nothing yet",
    fmt: dict[str, Any] | None = None,
) -> str:
    if not rows:
        return f'<p class="muted">{e(empty)}</p>'
    cols = list(cols or [(k, k) for k in rows[0].keys()])
    fmt = fmt or {}
    head = "".join(f"<th>{e(h)}</th>" for _, h in cols)
    body = []
    for r in rows:
        cells = []
        for k, _ in cols:
            v = r.get(k)
            cells.append(f"<td>{fmt[k](v, r) if k in fmt else e(_short(v))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        f'<div class="tbl"><table><thead><tr>{head}</tr></thead><tbody>'
        f"{''.join(body)}</tbody></table></div>"
    )


def _short(v: Any) -> Any:
    if isinstance(v, datetime):
        return f"{to_et(v):%m-%d %H:%M}"
    if isinstance(v, float):
        return f"{v:,.4g}"
    if isinstance(v, dict | list):
        t = str(v)
        return t if len(t) <= 120 else t[:117] + "..."
    return v


def link(href: str, text: Any) -> str:
    return f'<a href="{e(href)}">{e(text)}</a>'


def csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{e(csrf)}">'


def cmd_button(
    csrf: str,
    name: str,
    arg: str = "",
    label: str | None = None,
    danger: bool = False,
    confirm: str = "",
) -> str:
    conf = f' data-confirm="{e(confirm)}"' if confirm else ""
    return (
        f'<form method="post" action="/cmd" class="inline"{conf}>{csrf_field(csrf)}'
        f'<input type="hidden" name="name" value="{e(name)}">'
        f'<input type="hidden" name="arg" value="{e(arg)}">'
        f'<button class="{"danger" if danger else ""}">{e(label or (name + " " + arg))}'
        f"</button></form>"
    )


def layout(
    title: str,
    body: str,
    *,
    path: str = "/",
    env: str | None = None,
    csrf: str = "",
    flash: str = "",
    logged_in: bool = True,
) -> str:
    live = env == "live"
    banner = (
        (
            f'<div class="mode {"live" if live else "paper"}" data-k="environment">'
            f"{'LIVE' if live else 'PAPER'}</div>"
        )
        if env
        else ""
    )
    nav = (
        "".join(f'<a href="{h}" class="{"on" if h == path else ""}">{e(t)}</a>' for h, t in NAV)
        if logged_in
        else ""
    )
    tools = (
        (
            f'<form method="get" action="/stock" class="search"><input id="search" '
            f'name="symbol" placeholder="Symbol ( / )" maxlength="6" autocomplete="off">'
            f'</form><button id="theme" type="button" title="light/dark">◐</button>'
            f'<form method="post" action="/logout" class="inline">{csrf_field(csrf)}'
            f"<button>Log out</button></form>"
        )
        if logged_in
        else ""
    )
    fl = f'<div class="flash">{e(flash)}</div>' if flash else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)} · Owner Console</title>"
        f'<link rel="stylesheet" href="/static/app.css"><script src="/static/app.js" defer>'
        f"</script></head><body><header>{banner}<strong>Owner Console</strong>"
        f"<nav>{nav}</nav>{tools}</header><main><h1>{e(title)}</h1>{fl}{body}</main>"
        f"<footer>Times US Eastern · figures from the journal and trader-core, never from "
        f"the model · not advice</footer></body></html>"
    )


def login_page(error: str = "") -> str:
    err = f'<p class="flash">{e(error)}</p>' if error else ""
    return layout(
        "Log in",
        f'{err}<form method="post" action="/login" class="card narrow">'
        '<label>Password <input type="password" name="password" autofocus '
        'autocomplete="current-password"></label><button>Log in</button>'
        '<p class="muted">Password: secrets/console_password.txt</p></form>',
        logged_in=False,
    )


# --- pages ---------------------------------------------------------------------------------------


def home(
    st: dict[str, Any],
    plans: list[dict[str, Any]],
    ideas: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    beats: dict[str, datetime],
    llm: dict[str, Any],
    pending: int,
    csrf: str,
    now: datetime,
) -> str:
    src = st.get("source")
    stale = (
        ""
        if src == "live"
        else (
            f'<p class="flash">trader-core API unavailable: showing the last snapshot '
            f"({ts(st.get('snapshot_ts'))}) (UI-27)</p>"
            if src == "snapshot"
            else f'<p class="flash">No status yet: {e(st.get("error"))}</p>'
        )
    )
    eq = st.get("equity")
    start = st.get("day_start_equity")
    limit = st.get("loss_limit_pct") or 1.5
    lim_usd = (Decimal(str(start)) * Decimal(str(limit)) / 100) if start else None
    flags = []
    if st.get("kill_switch"):
        flags.append(badge("red", "KILL SWITCH ON"))
    if st.get("halted"):
        flags.append(badge("red", f"HALTED {st['halted']}"))
    for p in st.get("paused") or []:
        flags.append(badge("amber", f"PAUSED {p}"))
    cards = (
        f'<div class="grid"><div class="card"><h3>Account</h3>'
        f'<p>Equity <b data-k="equity">{money(eq)}</b></p>'
        f'<p>Day P&amp;L <b data-k="day_pnl">{money(st.get("day_pnl"))}</b> of the '
        f"-{money(lim_usd)} limit ({e(limit)}%)</p>"
        f'<p>Trades <b data-k="trades_today">{e(st.get("trades_today", "—"))}</b> of '
        f"{e(st.get('max_trades', 3))}</p>"
        f'<p>Window <b data-k="window">{e(st.get("window") or "none")}</b> · regime '
        f"{e(st.get('regime', '—'))}</p><p>{' '.join(flags) or badge('green', 'TRADING OK')}"
        f'</p><p><a href="/approvals">{pending} pending approval(s)</a></p></div>'
        f'<div class="card"><h3>Health</h3>{health_list(st)}</div>'
        f'<div class="card"><h3>Services and model (UI-4a)</h3>{services(beats, llm, now)}'
        f'<p><a href="http://localhost:3000" rel="noopener">Grafana dashboards</a></p></div>'
        f"</div>"
    )
    pos = table(
        st.get("positions") or [],
        [
            ("ticker", "Ticker"),
            ("style", "Style"),
            ("qty", "Qty"),
            ("entry", "Entry"),
            ("stop", "Stop"),
            ("t1", "T1"),
            ("last", "Last"),
            ("unrealized", "Unrealized"),
            ("r", "R"),
            ("opened_at", "In since"),
            ("position_id", "Story"),
        ],
        "no open positions",
        {
            "unrealized": lambda v, r: money(v),
            "r": lambda v, r: num(v, 2, True),
            "opened_at": lambda v, r: ts(v, "%H:%M"),
            "position_id": lambda v, r: link(f"/story/{v}", "story"),
            "ticker": lambda v, r: link(f"/stock?symbol={quote(str(v))}", v),
        },
    )
    plan_t = table(
        plans,
        [
            ("window", "Window"),
            ("ticker", "Ticker"),
            ("setup", "Setup"),
            ("trigger", "Trigger"),
            ("stop", "Stop"),
            ("target", "T1"),
        ],
        "no plan yet today",
        {"ticker": lambda v, r: link(f"/stock?symbol={quote(str(v))}", v)},
    )
    picks = [r for r in ideas if r.get("status") == "pick"]
    idea_t = table(
        picks,
        [
            ("plan_for", "For"),
            ("ticker", "Ticker"),
            ("score", "Score"),
            ("morning_setup", "Morning"),
            ("morning_trigger", "Trigger"),
        ],
        "no next-day ideas yet",
        {"ticker": lambda v, r: link(f"/stock?symbol={quote(str(v))}", v)},
    )
    inc = "".join(
        f"<li>{badge(_incident_level(i['type']), i['type'])} "
        f"{ts(i['ts'], '%m-%d %H:%M')} {e((i['detail'] or '')[:160])}</li>"
        for i in incidents
    )
    inc_block = (
        f'<ul class="plain">{inc}</ul>{cmd_button(csrf, "ACK", label="ACK alerts")}'
        if incidents
        else '<p class="muted">no open incidents</p>'
    )
    return (
        f'<div data-live="home">{stale}{cards}<h2>Open positions (UI-2)</h2>{pos}'
        f'<div class="grid2"><div><h2>Today\'s plan (UI-3)</h2>{plan_t}</div><div>'
        f"<h2>Next-day ideas</h2>{idea_t}</div></div><h2>Incidents (UI-4b)</h2>"
        f"{inc_block}</div>"
    )


def health_list(st: dict[str, Any]) -> str:
    h = st.get("health") or {}
    det = st.get("health_detail") or {}
    if not h:
        return '<p class="muted">no health data</p>'
    return (
        '<ul class="plain">'
        + "".join(
            f'<li>{badge(v, f"{k}: {v}")} <span class="muted" data-k="health.{e(k)}">'
            f"{e(det.get(k, ''))}</span></li>"
            for k, v in sorted(h.items())
        )
        + "</ul>"
    )


def services(beats: dict[str, datetime], llm: dict[str, Any], now: datetime) -> str:
    items = []
    for svc in ("core", "news", "notifier"):
        t = beats.get(svc)
        age = (now - t).total_seconds() if t else None
        lv = (
            "green"
            if age is not None and age < 120
            else "amber"
            if age is not None and age < 600
            else "red"
        )
        items.append(
            f"<li>{badge(lv, svc)} heartbeat "
            f"{e(f'{age:.0f}s ago' if age is not None else 'none in 1 h')}</li>"
        )
    vram = llm.get("vram_gb")
    items.append(
        f"<li>Model {e(llm.get('model'))} · loaded "
        f"{e(', '.join(llm.get('loaded') or []) or 'no')} · VRAM "
        f"{e(f'{vram:.1f} GB' if vram is not None else 'n/a')} of 8 GB · p95 "
        f"{e(llm.get('p95_ms') or '—')} ms ({e(llm.get('calls_today', 0))} calls)"
        f"{' · ' + e(llm['error']) if llm.get('error') else ''}</li>"
    )
    return '<ul class="plain">' + "".join(items) + "</ul>"


def stock(
    sym: str,
    quote_: dict[str, Any] | None,
    chart_html: str,
    daily_html: str,
    ind: dict[str, Any],
    news: list[dict[str, Any]],
    flags: list[dict[str, Any]],
    status: dict[str, Any],
    checklist: dict[str, Any] | None,
    options_iv: dict[str, Any] | None,
    chain: dict[str, Any] | None,
    csrf: str,
    error: str = "",
) -> str:
    if not sym:
        return (
            "<p>Type any US symbol in the search box (press <kbd>/</kbd>).</p>"
            '<p class="muted">Universe tickers show the live checklist; other symbols are '
            "informational only (UR-1).</p>"
        )
    q = quote_ or {}
    qhtml = (
        (
            f'<div class="card" data-symbol="{e(sym)}"><h3>Quote</h3>'
            f'<p>Last <b data-q="last">{num(q.get("last"))}</b> · change '
            f'<b data-q="change_pct">{num(q.get("change_pct"), 2, True)}%</b> · '
            f'bid/ask <span data-q="bid">{num(q.get("bid"))}</span>/'
            f'<span data-q="ask">{num(q.get("ask"))}</span> (spread '
            f'<span data-q="spread_pct">{num(q.get("spread_pct"), 3)}</span>%)</p>'
            f'<p>VWAP <span data-q="vwap">{num(q.get("vwap"))}</span> · RVOL '
            f'<span data-q="rvol">{num(q.get("rvol"))}</span> · volume '
            f'<span data-q="volume">{num(q.get("volume"), 0)}</span> · HOD/LOD '
            f"{num(q.get('hod'))}/{num(q.get('lod'))} · source {e(q.get('source', '—'))}</p>"
            f"</div>"
        )
        if quote_
        else f'<p class="flash">Quote unavailable: {e(error)}</p>'
    )
    macd = ind.get("macd")
    bb = ind.get("bollinger")
    kc = ind.get("keltner")
    ind_html = (
        '<div class="card"><h3>Indicators (A4 code, on demand)</h3><ul class="plain">'
        f"<li>RSI(14, 1m) {num(ind.get('rsi14'), 1)} · RSI(14, daily) "
        f"{num(ind.get('rsi14d'), 1)}</li>"
        f"<li>MACD {num(macd[0], 3) if macd else '—'} / signal "
        f"{num(macd[1], 3) if macd else '—'} / hist {num(macd[2], 3, True) if macd else '—'}</li>"
        f"<li>ATR(5m) {num(ind.get('atr5m'))} · ATR(14d) {num(ind.get('atr14d'))}</li>"
        f"<li>Gap {num(ind.get('gap_pct'), 2, True)}% · range position "
        f"{num(_pct(ind.get('range_pos')), 0)}%"
        f"</li><li>Bollinger {num(bb[0]) if bb else '—'} – {num(bb[2]) if bb else '—'} · "
        f"Keltner {num(kc[0]) if kc else '—'} – {num(kc[2]) if kc else '—'}</li>"
        f"<li>EMA20d {num(ind.get('ema20d'))} · EMA50d {num(ind.get('ema50d'))}</li></ul></div>"
    )
    st_html = (
        '<div class="card"><h3>In this system</h3><ul class="plain">'
        f"<li>Universe: {badge('green', 'YES') if status.get('universe') else badge(None, 'NO')}"
        f"</li><li>Correlated group: {e(status.get('group') or 'none')}</li>"
        f"<li>Locks: {e(', '.join(status.get('locks') or []) or 'none')}</li>"
        f"<li>Ideas: {e(', '.join(status.get('ideas') or []) or 'none')}</li>"
        f"<li>Past trades: {e(status.get('trades_summary') or 'none')}</li></ul>"
        f"<p>{cmd_button(csrf, 'OPT', f'{sym} BULL', 'Suggest bull (options)')} "
        f"{cmd_button(csrf, 'OPT', f'{sym} BEAR', 'Suggest bear (options)')} "
        f'<a class="btn" href="/ask?q={quote("What is the latest on " + sym + "?")}">Ask</a> '
        f'<a class="btn" href="/alerts">Price alerts</a> '
        f'<a class="btn" href="http://localhost:3000" rel="noopener">Grafana</a></p></div>'
    )
    if checklist and checklist.get("tradable"):
        ck = table(
            checklist["items"],
            [
                ("id", "Item"),
                ("passed", "Result"),
                ("value", "Measured"),
                ("threshold", "Threshold"),
            ],
            fmt={"passed": lambda v, r: passfail(v)},
        )
        ck_html = f"<h2>Checklist dry-run now (UI-5)</h2>{ck}"
    elif checklist:
        ck_html = f'<p class="muted">{e(checklist.get("note"))}</p>'
    else:
        ck_html = '<p class="muted">Checklist unavailable (trader-core API)</p>'
    news_t = table(
        news,
        [
            ("published_at", "Time"),
            ("source", "Source"),
            ("headline", "Headline"),
            ("finbert_score", "FinBERT"),
        ],
        "no news in the journal",
        {"finbert_score": lambda v, r: num(v, 2, True)},
    )
    fl = ", ".join(f"{f['flag']} ({f['date']})" for f in flags) or "none"
    opt = options_block(options_iv, chain)
    return (
        f'<div class="grid">{qhtml}{ind_html}{st_html}</div>'
        f"<h2>Intraday</h2>{chart_html}<h2>Daily</h2>{daily_html}{ck_html}"
        f"<h2>Options (OP-16)</h2>{opt}<h2>News and flags</h2><p>Event flags: {e(fl)}</p>"
        f'{news_t}<p class="muted">Data limits (UI-6a): the free Alpaca IEX feed covers '
        f"about 2–3% of volume; Finnhub free tier 60 calls/min (lookups cached 60 s); "
        f"symbols outside the universe get no FinBERT run. Option prices are indicative."
        f"</p>"
    )


def options_block(iv: dict[str, Any] | None, chain: dict[str, Any] | None) -> str:
    if iv is None:
        return '<p class="muted">Options data unavailable (options.mode off or no feed)</p>'
    moves = ", ".join(
        f"{k}: ±{v:.2f}" for k, v in list((iv.get("expected_moves") or {}).items())[:5]
    )
    head = (
        f"<p>ATM IV 30d {num((iv.get('atm_iv_30d') or 0) * 100, 1)}% · IV rank "
        f"{num(iv.get('iv_rank'), 0)} · IV pct {num(iv.get('iv_pct'), 0)} · history "
        f"{e(iv.get('history'))} · term front {num((iv.get('term_front') or 0) * 100, 1)}% "
        f"vs 30d {num((iv.get('term_30d') or 0) * 100, 1)}% · skew "
        f"{num((iv.get('skew') or 0) * 100, 1, True)} pts</p><p>Expected move: "
        f"{e(moves or 'n/a')}</p>"
    )
    if not chain:
        return head
    spot = chain.get("spot") or 0
    rows = sorted(chain["rows"], key=lambda r: abs(r["strike"] - spot))[:18]
    calls = {r["strike"]: r for r in rows if r["kind"] == "C"}
    puts = {r["strike"]: r for r in rows if r["kind"] == "P"}

    def cell(r: dict[str, Any] | None) -> str:
        if r is None:
            return "<td colspan=6>—</td>"
        cls = "ok" if r["liquid"] else "bad"
        return (
            f"<td>{num(r['bid'])}/{num(r['ask'])}</td><td>{num(r['mid'])}</td>"
            f"<td>{num((r['iv'] or 0) * 100, 0)}%</td><td>{num(r['delta'], 2, True)}</td>"
            f'<td>{e(r["oi"])}/{e(r["volume"])}</td><td><span class="badge {cls}">'
            f"{'liquid' if r['liquid'] else e(r['why'])}</span></td>"
        )

    body = "".join(
        f"<tr>{cell(calls.get(k))}<th>{k:g}</th>{cell(puts.get(k))}</tr>"
        for k in sorted(set(calls) | set(puts))
    )
    hdr = "<th>bid/ask</th><th>mid</th><th>IV</th><th>Δ</th><th>OI/vol</th><th>OP-4</th>"
    return (
        f'{head}<p>Chain {e(chain.get("expiry"))} (stock {num(spot)})</p><div class="tbl">'
        f"<table><thead><tr><th colspan=6>Calls</th><th>Strike</th><th colspan=6>Puts</th>"
        f"</tr><tr>{hdr}<th></th>{hdr}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def ask_page(
    csrf: str, answer: Any, history: list[dict[str, Any]], q: str, search: str = ""
) -> str:
    ans = ""
    if answer is not None:
        sug = "".join(
            f'<a class="btn" href="/controls?suggest={quote(c)}">Suggested command: '
            f"{e(c)} (opens Controls, you confirm)</a>"
            for c in answer.suggested
        )
        note = f'<p class="muted">{e(answer.note)}</p>' if answer.note else ""
        ans = (
            f'<div class="card"><p><b>{e(answer.question)}</b></p><p>{e(answer.text)}</p>'
            f'{note}<p class="muted">Source: {e(answer.source)} · tool {e(answer.tool)} '
            f"{e(answer.args)} · {'Qwen wording' if answer.model_used else 'table only'}</p>"
            f"{table(answer.rows[:50])}{sug}</div>"
        )
    hist = "".join(
        f"<li>{ts(h['ts'], '%H:%M')} <b>{e(h['question'])}</b> — "
        f"{e((h['answer'] or '')[:200])}</li>"
        for h in reversed(history)
    )
    return (
        f'<form method="post" action="/ask" class="card">{csrf_field(csrf)}'
        f'<label>Question <input name="q" value="{e(q)}" maxlength="500" '
        f'placeholder="Why were there no trades this morning?" autofocus></label>'
        f'<button>Ask</button><p class="muted">Read-only: answers come from journal tools; '
        f"during entry windows (09:35–10:30, 15:00–15:55 ET) the model waits and the table "
        f"answers (UI-8b).</p></form>{ans}<h2>Today's questions (UI-8d)</h2>"
        f'<form method="get" action="/ask" class="inline"><input name="search" '
        f'value="{e(search)}" placeholder="search"><button>Search</button></form> '
        f'<form method="post" action="/ask/clear" class="inline" data-confirm="Clear '
        f"today's history?\">{csrf_field(csrf)}<button>Clear</button></form>"
        f'<ul class="plain">{hist or "<li class=muted>none</li>"}</ul>'
    )


def approvals_page(csrf: str, pending: list[dict[str, Any]], today: list[dict[str, Any]]) -> str:
    cards = []
    for p in pending:
        cards.append(
            f'<div class="card"><h3>{e(p["ticker"])} {e(p["setup"])} · '
            f'<span data-expires="{e(p["expires"].isoformat())}">…</span> left</h3>'
            f"<p>Entry {num(p['entry'])} · stop {num(p['stop'])} · T1 {num(p['t1'])} · "
            f"{e(p['qty'])} sh · risk {money(p['risk_usd'])}</p><p>{e(p['reasoning'][:400])}</p>"
            f"<p>{link('/story/' + p['proposal_id'], 'trade story')}</p>"
            f'<form method="post" action="/cmd" class="inline">{csrf_field(csrf)}'
            f'<input type="hidden" name="name" value="YES"><label>Code from your phone '
            f'<input name="arg" maxlength="8" autocomplete="off" required></label>'
            f"<button>Approve</button></form> "
            f'<form method="post" action="/cmd" class="inline">{csrf_field(csrf)}'
            f'<input type="hidden" name="name" value="NO"><label>Code '
            f'<input name="arg" maxlength="8" autocomplete="off" required></label>'
            f'<button class="danger">Reject</button></form></div>'
        )
    body = "".join(cards) or '<p class="muted">no pending approvals</p>'
    hist = table(
        today,
        [
            ("sent_at", "Sent"),
            ("ticker", "Ticker"),
            ("setup", "Setup"),
            ("answer", "Outcome"),
            ("answered_at", "Answered"),
            ("proposal_id", "Story"),
        ],
        "none today",
        {"proposal_id": lambda v, r: link(f"/story/{v}", "story")},
    )
    return (
        f'<p class="muted">Type the one-time code shown on your phone (UI-C3): a click '
        f"alone never approves. The first answer wins, from either channel.</p>{body}"
        f"<h2>Today (UI-11)</h2>{hist}"
    )


def controls_page(
    csrf: str,
    st: dict[str, Any],
    audit: list[dict[str, Any]],
    suggest: str = "",
    result: dict[str, Any] | None = None,
) -> str:
    pos = st.get("positions") or []
    flat = (
        "close "
        + (", ".join(f"{p['ticker']} {p['qty']} sh" for p in pos) or "nothing (no open positions)")
        + " at market"
    )
    res = ""
    if result is not None:
        ok = bool(result.get("ok"))
        res = f'<div class="card">{badge("green" if ok else "red", "SENT" if ok else "REFUSED")} ' \
            f'{e(result.get("reason"))}'
        if result.get("need_confirm"):
            res += (
                f'<form method="post" action="/cmd" class="inline">{csrf_field(csrf)}'
                f'<input type="hidden" name="name" value="CONFIRM"><label>Confirmation '
                f'code <input name="arg" maxlength="8" autocomplete="off" required></label>'
                f'<button class="danger">Confirm</button></form>'
            )
        res += "</div>"
    sug = (
        (
            f'<p class="flash">Suggested by Ask: {e(suggest)} — review, then press the button '
            f"below.</p>"
        )
        if suggest
        else ""
    )
    buttons = " ".join(
        [
            cmd_button(csrf, "STATUS"),
            cmd_button(csrf, "PLAN"),
            cmd_button(
                csrf, "KILL", label="KILL (no confirmation: the safe direction)", danger=True
            ),
            cmd_button(csrf, "PAUSE", "DAY", "PAUSE DAY"),
            cmd_button(csrf, "PAUSE", "SWING", "PAUSE SWING"),
            cmd_button(csrf, "ACK"),
            cmd_button(csrf, "ALERTS", "OFF", "Signal alerts OFF"),
            cmd_button(csrf, "ALERTS", "ON", "Signal alerts ON"),
        ]
    )
    destructive = " ".join(
        [
            cmd_button(csrf, "FLATTEN", label="FLATTEN", danger=True, confirm=f"FLATTEN: {flat}?"),
            cmd_button(csrf, "RESUME", label="RESUME", confirm="Resume trading after a kill?"),
            cmd_button(csrf, "UNPAUSE", "DAY", "UNPAUSE DAY", confirm="Unpause day trades?"),
            cmd_button(csrf, "UNPAUSE", "SWING", "UNPAUSE SWING", confirm="Unpause swing?"),
        ]
    )
    aud = table(
        audit,
        [("ts", "Time"), ("text", "Command"), ("accepted", "Accepted"), ("reason", "Result")],
        "no console commands yet",
        {
            "accepted": lambda v, r: passfail(v),
            "reason": lambda v, r: e(
                "confirmation code issued (sent to phone)"
                if "confirmation code issued" in (v or "")
                else v
            ),
        },
    )
    return (
        f'{sug}{res}<div class="card"><h3>Commands (UI-12)</h3><p>{buttons}</p>'
        f"<h3>Need the confirmation code from your phone (SEC-3)</h3><p>{destructive}</p>"
        f'<p class="muted">FLATTEN would {e(flat)}.</p></div>'
        f"<h2>Audit (UI-13, source = console)</h2>{aud}"
    )


def story_page(d: dict[str, Any], chart_html: str, reasons: dict[str, str], csrf: str) -> str:
    def sect(title: str, rows: list[dict[str, Any]], cols: Any = None) -> str:
        return f"<h2>{e(title)}</h2>{table(rows, cols)}"

    decs = d["decisions"]
    expl = "".join(
        f"<li><b>{e(x['reason_code'])}</b>: {e(reasons.get(x['reason_code'] or '', '—'))}</li>"
        for x in decs
        if x.get("reason_code")
    )
    checklist = []
    for x in decs:
        for it in x.get("checklist_json") or []:
            checklist.append(it)
    prop = d["proposal"][0]["json"] if d["proposal"] else {}
    rev = d["review"][0] if d["review"] else {}
    return (
        f'{chart_html}<div class="grid2"><div class="card"><h3>A5 proposal</h3>'
        f'<pre>{e(prop)}</pre></div><div class="card"><h3>A6 review</h3>'
        f"<p>{badge(_verdict_level(rev.get('verdict')), rev.get('verdict') or 'none')}"
        f" {e(rev.get('main_risk'))}</p><pre>{e(rev.get('rules_broken'))}</pre></div></div>"
        f"{sect('Setup and trigger', d['setup'])}"
        f"<h2>Checklist</h2>{checklist_table(checklist)}"
        f'{sect("A7 decision and sizing", decs)}<ul class="plain">{expl}</ul>'
        f"{sect('A12 compliance', d['compliance'])}{sect('Approval', d['approvals'])}"
        f"{sect('Orders', d['orders'])}{sect('Fills', d['fills'])}"
        f"{sect('Position and result', d['positions'])}{sect('Latency', d['latency'])}"
        f"<h2>Owner notes (UI-29)</h2>{notes_list(d['notes'])}"
        f"{note_form(csrf, 'proposal', d['proposal_id'])}"
    )


def notes_list(rows: Iterable[dict[str, Any]]) -> str:
    items = "".join(
        f"<li>{ts(r['ts'], '%m-%d %H:%M')} {e(', '.join(r.get('tags') or []))} — "
        f"{e(r['note'])}{' (screenshot)' if r.get('screenshot_b64') else ''}</li>"
        for r in rows
    )
    return f'<ul class="plain">{items or "<li class=muted>none</li>"}</ul>'


TAGS = ("followed the plan", "FOMO", "hesitated", "early exit", "late entry", "moved stop")


def note_form(csrf: str, ref_type: str, ref_id: str) -> str:
    boxes = "".join(
        f'<label class="chk"><input type="checkbox" name="tags" value="{e(t)}">{e(t)}</label>'
        for t in TAGS
    )
    return (
        f'<form method="post" action="/notes" enctype="multipart/form-data" class="card">'
        f'{csrf_field(csrf)}<input type="hidden" name="ref_type" value="{e(ref_type)}">'
        f'<input type="hidden" name="ref_id" value="{e(ref_id)}"><p>{boxes}</p>'
        f'<label>Note <textarea name="note" maxlength="4000" rows="3"></textarea></label>'
        f'<label>Screenshot <input type="file" name="screenshot" accept="image/png,'
        f'image/jpeg"></label><button>Save note</button></form>'
    )


def _incident_level(kind: str | None) -> str:
    k = kind or ""
    return "red" if "HALT" in k or "DOWN" in k else "amber"


def _verdict_level(v: str | None) -> str:
    return "green" if v == "agree" else "amber"


def _pct(x: Any) -> float | None:
    return float(x) * 100 if x is not None else None


def checklist_table(items: list[dict[str, Any]]) -> str:
    return table(items, [("id", "Item"), ("passed", "Result"), ("detail", "Detail")],
                 fmt={"passed": lambda v, r: passfail(v)})
