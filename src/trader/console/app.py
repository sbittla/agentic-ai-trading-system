"""Owner Console web app (Starlette). See docs/UI_REQUIREMENTS.md.

Security (UI-21): bound to 127.0.0.1 by compose; password from a Docker secret; server-side
sessions in an HttpOnly SameSite=Strict cookie with a 30-minute idle timeout; a CSRF token on
every POST; a strict Content-Security-Policy (no inline scripts or styles, no third-party
origins); login attempts are rate-limited.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import inspect
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trader.console import charts, indicators
from trader.console import pages as pg
from trader.console import render as r
from trader.console.ask import Asker
from trader.console.data import ConsoleData, recap_latest, upcoming_events
from trader.console.data import news as news_q
from trader.console.data import ratings as ratings_q
from trader.journal import schema as s
from trader.models import reasons as reasons_mod
from trader.timeutil import to_et

COOKIE = "oc_session"
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
)
STATIC = Path(__file__).parent / "static"
SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,5}$")
MAX_SCREENSHOT = 2_000_000


# --- reason codes in words (UI-9) ----------------------------------------------------------------


def _reason_texts() -> dict[str, str]:
    out = {
        "NT_CK1": "a checklist item failed: market filters",
        "NT_CK2": "outside the window",
        "NT_CK3": "a market filter failed (QQQ/SMH/RVOL/spread/halt/quote)",
        "NT_CK4": "news or event block",
        "NT_CK5": "the trigger bar had not closed",
        "NT_CK6": "entry would chase the trigger",
        "NT_CK7": "stop too wide",
        "NT_CK8": "not enough room to resistance or T2 below 2R",
        "NT_CK9": "the A6 reviewer disagreed",
        "NT_CK10": "compliance lock",
    }
    for line in inspect.getsource(reasons_mod).splitlines():
        m = re.match(r'\s+([A-Z0-9_]+) = "[A-Z0-9_]+"\s+#\s*(.+)$', line)
        if m:
            out.setdefault(m.group(1), m.group(2).strip())
    return out


REASONS = _reason_texts()


# --- auth ----------------------------------------------------------------------------------------


@dataclass
class Session:
    csrf: str
    last: datetime


@dataclass
class Auth:
    password: str
    idle: timedelta = timedelta(minutes=30)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sessions: dict[str, Session] = field(default_factory=dict)
    failures: list[datetime] = field(default_factory=list)

    def login(self, pw: str) -> str | None:
        now = self.now()
        self.failures = [t for t in self.failures if now - t < timedelta(minutes=5)]
        if len(self.failures) >= 5:
            return None  # rate limit: 5 tries / 5 min
        if not self.password or not hmac.compare_digest(pw.encode(), self.password.encode()):
            self.failures.append(now)
            return None
        tok = secrets.token_urlsafe(32)
        self.sessions[tok] = Session(secrets.token_urlsafe(24), now)
        return tok

    def get(self, tok: str | None) -> Session | None:
        if not tok:
            return None
        ses = self.sessions.get(tok)
        now = self.now()
        if ses is None or now - ses.last > self.idle:
            self.sessions.pop(tok or "", None)
            return None
        ses.last = now
        return ses

    def locked_out(self) -> bool:
        now = self.now()
        return len([t for t in self.failures if now - t < timedelta(minutes=5)]) >= 5


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(msg: Message) -> None:
            if msg["type"] == "http.response.start":
                h = list(msg.get("headers", []))
                h += [
                    (b"content-security-policy", CSP.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                ]
                msg["headers"] = h
            await send(msg)

        await self.app(scope, receive, wrapped)


# --- app ---------------------------------------------------------------------------------------


def create_app(
    data: ConsoleData, auth: Auth, asker: Asker | None = None, sse_interval_s: float = 5.0
) -> Starlette:
    ask = asker or Asker(data)

    def page(
        fn: Callable[[Request, Session], Awaitable[Response]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def h(req: Request) -> Response:
            ses = auth.get(req.cookies.get(COOKIE))
            if ses is None:
                if req.url.path.startswith(("/api", "/events")):
                    return JSONResponse({"error": "login required"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            if req.method == "POST":
                form = await req.form()
                if not hmac.compare_digest(str(form.get("csrf", "")), ses.csrf):
                    return PlainTextResponse("CSRF token missing or wrong", status_code=403)
            return await fn(req, ses)

        return h

    async def env() -> str | None:
        st = await data.status()
        e: str | None = st.get("environment")
        return e or getattr(data.cfg.mode, "environment", None)

    async def html(
        req: Request, ses: Session, title: str, body: str, flash: str = ""
    ) -> HTMLResponse:
        return HTMLResponse(
            r.layout(title, body, path=req.url.path, env=await env(), csrf=ses.csrf, flash=flash)
        )

    # -- auth routes ------------------------------------------------------------------------------
    async def login_get(req: Request) -> Response:
        return HTMLResponse(r.login_page())

    async def login_post(req: Request) -> Response:
        if auth.locked_out():
            return HTMLResponse(r.login_page("too many attempts: wait 5 minutes"), status_code=429)
        form = await req.form()
        tok = auth.login(str(form.get("password", "")))
        if tok is None:
            await asyncio.sleep(0.5)
            return HTMLResponse(r.login_page("wrong password"), status_code=401)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, tok, httponly=True, samesite="strict", path="/", secure=False
        )  # http on 127.0.0.1 only (UI-C1)
        return resp

    @page
    async def logout(req: Request, ses: Session) -> Response:
        auth.sessions.pop(req.cookies.get(COOKIE, ""), None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    async def healthz(req: Request) -> Response:
        return JSONResponse({"ok": True})

    # -- Today (UI-1..UI-4b) + SSE (UI-22) -----------------------------------------------------
    async def home_data() -> dict[str, Any]:
        st = await data.status()
        return {"status": st, "pending": len(data.pending_approvals()),
                "incidents": len(data.incidents_open())}

    @page
    async def home(req: Request, ses: Session) -> Response:
        st, sc, *qs = await asyncio.gather(
            data.status(), data.core.get("/scanner"),
            *(data.core.get("/quote", symbol=x) for x in ("SPY", "QQQ", "SMH")))
        run_at, recap = recap_latest(data)
        body = pg.home(
            st, plans=data.plans(data.today()), ideas=data.ideas(),
            incidents=data.incidents_open(), beats=data.heartbeats(), llm=await data.ollama(),
            pending=len(data.pending_approvals()), scanner=(sc or {}).get("rows") or [],
            recap_rows=recap, recap_at=run_at, ratings=ratings_q(data, 14),
            news=news_q(data, 3, limit=8), events=upcoming_events(data),
            quotes={x: q for x, q in zip(("SPY", "QQQ", "SMH"), qs, strict=True) if q},
            csrf=ses.csrf, now=data.now(), technical=data.technical_scan(),
            catalysts=data.catalysts())
        return await html(req, ses, "Today", body)

    @page
    async def events(req: Request, ses: Session) -> Response:
        async def gen() -> AsyncIterator[str]:
            for _ in range(720):  # one hour per connection, then the browser reconnects
                if await req.is_disconnected():
                    break
                d = await home_data()
                yield f"data: {json.dumps(d, default=str)}\n\n"
                await asyncio.sleep(sse_interval_s)
        return StreamingResponse(gen(), media_type="text/event-stream")

    @page
    async def api_home(req: Request, ses: Session) -> Response:
        return JSONResponse(json.loads(json.dumps(await home_data(), default=str)))

    @page
    async def api_quote(req: Request, ses: Session) -> Response:
        sym = req.query_params.get("symbol", "").upper()
        if not SYMBOL.match(sym):
            return JSONResponse({"error": "bad symbol"}, status_code=400)
        q = await data.core.get("/quote", symbol=sym)
        return JSONResponse(q or {"error": data.core.last_error}, status_code=200 if q else 503)

    # -- Market recap, ratings, news (UI-33, UI-34) -----------------------------------------------
    @page
    async def market(req: Request, ses: Session) -> Response:
        run_at, rows = recap_latest(data)
        return await html(req, ses, "Market recap", pg.market(run_at, rows, ses.csrf))

    @page
    async def ratings_page(req: Request, ses: Session) -> Response:
        qp = req.query_params
        days = int(qp["days"]) if qp.get("days", "").isdigit() else 30
        days = days if days in (7, 30, 90) else 30
        tick = qp.get("ticker", "").upper()
        tick = tick if SYMBOL.match(tick) else ""
        action = qp.get("action", "")
        action = action if action in r.ACTION_LABEL else ""
        rows = ratings_q(data, days, tick or None, action or None)
        return await html(req, ses, "Analyst ratings", pg.ratings(
            rows, days=days, ticker=tick, action=action,
            universe=tuple(data.cfg.universe.tickers)))

    @page
    async def news_page(req: Request, ses: Session) -> Response:
        qp = req.query_params
        days = int(qp["days"]) if qp.get("days", "").isdigit() else 7
        days = days if days in (1, 7, 30) else 7
        tick = qp.get("ticker", "").upper()
        tick = tick if SYMBOL.match(tick) else ""
        return await html(req, ses, "News", pg.news_page(
            news_q(data, days, tick or None, limit=300), tick,
            tuple(data.cfg.universe.tickers), days))

    # -- Stock lookup (UI-4..UI-6a) -------------------------------------------------------------
    @page
    async def stock(req: Request, ses: Session) -> Response:
        sym = req.query_params.get("symbol", "").upper().strip().lstrip("$")
        if not sym or not SYMBOL.match(sym):
            body = pg.stock("", q=None, chart_html="", daily_html="", ind={}, news=[],
                            flags=[], status={}, checklist=None, iv=None, chain=None,
                            ratings=[], recap=None, csrf=ses.csrf)
            return await html(req, ses, "Stock", body,
                              flash="not a valid symbol" if sym else "")
        q, bars, daily, ck, iv, chain = await asyncio.gather(
            data.core.get("/quote", symbol=sym), data.core.get("/bars", symbol=sym),
            data.core.get("/daily", symbol=sym), data.core.get("/checklist", symbol=sym),
            data.core.get("/options/iv", symbol=sym),
            data.core.get("/options/chain", symbol=sym))
        intraday = (bars or {}).get("bars") or []
        dbars = (daily or {}).get("bars") or []
        lv = (bars or {}).get("levels") or {}
        chart_html = charts.candles(
            intraday, vwap=_vwap_series(intraday), title=f"{sym} 1-minute",
            levels={"prior high": lv.get("prior_high"), "prior low": lv.get("prior_low"),
                    "prior close": lv.get("prior_close"), "PM high": lv.get("pm_high")},
            opening_range=(bars or {}).get("or"), markers=_fill_markers(data, sym, intraday))
        daily_html = charts.candles(dbars[-90:], title=f"{sym} daily")
        ind = indicators.summary(intraday, dbars, (q or {}).get("prior_close"))
        _, recap = recap_latest(data)
        body = pg.stock(
            sym, q=q, chart_html=chart_html, daily_html=daily_html, ind=ind,
            news=data.rows(s.news_items, {"ticker": sym}, order_by="published_at",
                           descending=True, limit=10),
            flags=data.rows(s.event_flags, {"ticker": sym}, order_by="date", descending=True,
                            limit=10),
            status=_system_status(data, sym), checklist=ck, iv=iv, chain=chain,
            ratings=ratings_q(data, 90, sym), recap=next(
                (x for x in recap if x.get("ticker") == sym), None),
            csrf=ses.csrf, error=data.core.last_error)
        return await html(req, ses, sym, body)

    # -- Ask (UI-7..UI-8d) ------------------------------------------------------------------------
    @page
    async def ask_get(req: Request, ses: Session) -> Response:
        search = req.query_params.get("search", "")
        q = req.query_params.get("q", "")
        ans = await ask.ask(q) if q else None
        return await html(req, ses, "Ask", pg.ask_page(ses.csrf, ans, data.chats(q=search), q,
                                                       search))

    @page
    async def ask_post(req: Request, ses: Session) -> Response:
        q = str((await req.form()).get("q", "")).strip()
        ans = await ask.ask(q) if q else None
        return await html(req, ses, "Ask", pg.ask_page(ses.csrf, ans, data.chats(), q))

    @page
    async def ask_clear(req: Request, ses: Session) -> Response:
        data.clear_chats()
        return RedirectResponse("/ask", status_code=303)

    # -- Approvals, controls, commands (UI-9..UI-13) --------------------------------------------
    @page
    async def approvals(req: Request, ses: Session) -> Response:
        return await html(req, ses, "Approvals", pg.approvals_page(
            ses.csrf, data.pending_approvals(), data.approvals_today()))

    @page
    async def controls(req: Request, ses: Session) -> Response:
        return await html(req, ses, "Controls", pg.controls_page(
            ses.csrf, await data.status(), data.audit(), req.query_params.get("suggest", "")))

    @page
    async def cmd(req: Request, ses: Session) -> Response:
        form = await req.form()
        name, arg = str(form.get("name", "")), str(form.get("arg", ""))
        res = await data.command(name, arg)
        if name.upper() in ("YES", "NO"):
            flash = ("Approval sent: " if res.get("ok") else "Approval refused: ") + \
                str(res.get("reason"))
            return await html(req, ses, "Approvals", pg.approvals_page(
                ses.csrf, data.pending_approvals(), data.approvals_today()), flash=flash)
        return await html(req, ses, "Controls", pg.controls_page(
            ses.csrf, await data.status(), data.audit(), result=res))

    @page
    async def stories(req: Request, ses: Session) -> Response:
        rows = data.recent_proposals(7)
        body = r.page_head("Trades", "Every decision of the last 7 days, taken or not")
        if rows:
            body += r.card(f"{len(rows)} decisions", r.table(rows, [
                ("ts", "Time"), ("ticker", "Ticker"), ("setup", "Setup"), ("result", "Result"),
                ("reason_code", "Reason"), ("qty", "Qty"), ("proposal_id", "")], "", {
                "proposal_id": lambda v, row: r.link(f"/story/{v}", "story"),
                "ticker": lambda v, row: r.stock_link(v),
                "result": lambda v, row: r.chip(v, "ok" if v in ("APPROVE", "SUBMIT") else
                                                "neutral"),
                "reason_code": lambda v, row: r.e(f"{v} — {REASONS.get(v or '', '')}"
                                                  if v else "")}))
        else:
            body += r.card("Decisions", r.empty(
                "No decisions in 7 days", "A decision is journaled for every setup that "
                "triggers, taken or rejected, with the reason. Setups trigger only inside the "
                "trading windows on market days.", icon="≡"))
        return await html(req, ses, "Trades", body)

    @page
    async def story(req: Request, ses: Session) -> Response:
        pid = req.path_params["pid"][:64]
        d = data.story(pid)
        tick = (d["setup"][0]["ticker"] if d["setup"] else
                d["decisions"][0]["ticker"] if d["decisions"] else None)
        chart_html = ""
        if tick:
            b = await data.core.get("/bars", symbol=tick)
            bars = (b or {}).get("bars") or []
            chart_html = charts.candles(bars, vwap=_vwap_series(bars), title=f"{tick} story",
                                        markers=_fill_markers(data, tick, bars, pid))
        return await html(req, ses, f"Trade story {tick or pid}",
                          pg.story_page(d, chart_html, REASONS, ses.csrf))

    # -- Co-pilot (UI-32) -----------------------------------------------------------------------
    @page
    async def copilot(req: Request, ses: Session) -> Response:
        cards = []
        for t in data.copilot_tickets():
            cards.append(r.card(
                f"{t['ticker']} {t['setup']} · {r.ts(t['ts'], '%H:%M')}",
                f'<dl class="kv"><dt>Buy</dt><dd>{r.e(t["qty"])} near {r.num(t["entry"])}</dd>'
                f'<dt>Stop</dt><dd>{r.num(t["stop"])}</dd><dt>T1</dt><dd>{r.num(t["t1"])}</dd>'
                f'<dt>Risk</dt><dd>{r.money(t["risk_usd"])}</dd></dl>'
                f'<form method="post" action="/copilot/fill" class="inline">'
                f'{r.csrf_field(ses.csrf)}<input type="hidden" name="ticker" '
                f'value="{r.e(t["ticker"])}"><label>Qty <input name="qty" size="5" '
                f'value="{r.e(t["qty"] or "")}"></label><label>Price <input name="price" '
                f'size="8" value="{r.e(t["entry"] or "")}"></label><button class="primary">'
                f'I filled</button></form> {r.link("/story/" + t["proposal_id"], "plan story")}',
                cls="attn"))
        body = r.page_head("Co-pilot", "Plans the system would trade; you place the order, "
                           "it tracks the exits") + ("".join(cards) or r.card("Tickets", r.empty(
                               "No co-pilot plans today", "Set " + r.code("mode.day_trades: "
                               "copilot") + " in config/config.yaml: at a trigger the plan "
                               "appears here and on Telegram; press I filled after your order "
                               "fills and exit alerts follow (CP-2).", icon="✈")))
        return await html(req, ses, "Co-pilot", body)

    @page
    async def copilot_fill(req: Request, ses: Session) -> Response:
        f = await req.form()
        tick, qty, price = (str(f.get("ticker", "")).upper(), str(f.get("qty", "")),
                            str(f.get("price", "")))
        if not (SYMBOL.match(tick) and qty.isdigit() and re.fullmatch(r"\d+(\.\d+)?", price)):
            return await html(req, ses, "Co-pilot", "", flash="ticker, qty and price needed")
        res = await data.command("FILLED", f"{tick} {qty} @ {price}")
        return await html(req, ses, "Co-pilot", r.card("Result", f"<p>{r.e(res.get('reason'))}"
                                                       f"</p><p>{r.link('/copilot', 'back')}</p>"))

    # -- Options (OP-16..OP-19, UI-36) ----------------------------------------------------------
    @page
    async def options(req: Request, ses: Session) -> Response:
        from trader.options import monitor

        qp = req.query_params
        rows = data.suggestions(30)
        board = await data.core.get("/options/board")
        err = "" if board else data.core.last_error
        preview = None
        psym = qp.get("preview", "").upper()
        if psym and SYMBOL.match(psym):
            preview = await data.core.get("/options/preview", symbol=psym,
                                          direction=qp.get("dir", "bull"),
                                          style=qp.get("style", "day"))
            if preview is None:
                preview = {"symbol": psym, "view": {}, "suggestions": [],
                           "notes": [data.core.last_error]}
        body = pg.options(rows, board, preview, monitor.report(rows) if rows else "",
                          monitor.promotion_status(rows), ses.csrf, err)
        return await html(req, ses, "Options", body)

    @page
    async def option_detail(req: Request, ses: Session) -> Response:
        row = data.suggestion(req.path_params["sid"][:64])
        if row is None:
            return await html(req, ses, "Option", "", flash="not found")
        from trader.options.monitor import from_row

        sug = from_row(row)
        body = r.page_head(f"{row['ticker']} {row['structure'].replace('_', ' ')}",
                           f"Suggested {r.ts(row['ts'], '%b %d %H:%M')} ET · status "
                           f"{r.e(row['status'])}")
        if sug is not None:
            body += r.card("Payoff", payoff_svg(sug, row))
        body += r.card("Message", f"<pre>{r.e(row['text'])}</pre>")
        return await html(req, ses, "Option", body)

    # -- Alerts, ideas, reports, compliance (UI-14..UI-17) ----------------------------------------
    @page
    async def alerts(req: Request, ses: Session) -> Response:
        rows = data.alerts_today()
        rules, wl = data.bot_rules()
        body = (r.page_head("Alerts", "trader-core's signal alerts today and the standalone bot's "
                            "price rules", r.cmd_button(ses.csrf, "ALERTS", "ON",
                                                        "Signal alerts ON") + " "
                            + r.cmd_button(ses.csrf, "ALERTS", "OFF", "Mute signal alerts"))
                + r.card("Today's signal alerts", r.table(rows, [
                    ("ts", "Time"), ("ticker", "Ticker"), ("kind", "Signal"),
                    ("direction", "Dir"), ("label", "Label"), ("checklist_score", "Score"),
                    ("first_failed", "First failed"), ("delivery", "Delivery"),
                    ("hit", "+1R first")], "", {"ticker": lambda v, row: r.stock_link(v)})
                    if rows else r.empty(
                        "No alerts today", "Signal alerts (opening-range breaks, VWAP, new "
                        "highs and lows, volume spikes) are sent pre-market, 9:30-10:30 and "
                        "15:00-16:00 ET on market days, ranked and capped (AL-4..AL-6).",
                        icon="◔"))
                + r.card("Standalone bot rules", (f'<p class="muted small">{r.e(wl)}</p>'
                                                  + r.table(rules)) if rules else r.empty(
                    "No bot rules", r.e(wl) + ". The optional alerts bot keeps its own "
                    "database; add rules from Telegram (" + r.code("/add NVDA above 140")
                    + ").", icon="◔")))
        return await html(req, ses, "Alerts", body)

    @page
    async def ideas(req: Request, ses: Session) -> Response:
        _, recap = recap_latest(data)
        watch = [x for x in recap if x.get("kind") == "stock" and x.get("tags")]
        return await html(req, ses, "Next-day ideas", pg.ideas(
            data.ideas(), data.idea_history(20), watch, ses.csrf))

    @page
    async def technical(req: Request, ses: Session) -> Response:
        from trader.console import technical as tech

        sc = getattr(data.cfg, "scanner", None)
        body = tech.page(data.technical_scan(), data.scanner_eval(), ses.csrf,
                         tuple(sc.disabled) if sc else ())
        return await html(req, ses, "Technical scan", body)

    @page
    async def macro_page(req: Request, ses: Session) -> Response:
        from trader.console import macro as mp

        return await html(req, ses, "Macro", mp.page(data.macro(), data.forecast(), ses.csrf))

    @page
    async def momentum_page(req: Request, ses: Session) -> Response:
        from trader.console import momentum as mp

        return await html(req, ses, "Momentum", mp.page(data.momentum(), data.momentum_eval(),
                                                        ses.csrf, data.events()))

    @page
    async def catalysts(req: Request, ses: Session) -> Response:
        from trader.console import catalysts as cat

        return await html(req, ses, "News catalysts",
                          cat.page(data.catalysts(), data.catalyst_eval(), ses.csrf))

    @page
    async def reports(req: Request, ses: Session) -> Response:
        name = req.query_params.get("name")
        if name:
            p = data.report_path(name)
            if p is None:
                return await html(req, ses, "Reports", "", flash="not found")
            if req.query_params.get("download"):
                return FileResponse(p, filename=p.name)
            return await html(req, ses, f"Report {p.name}", r.page_head(
                p.name, r.link(f"/reports?name={quote(name)}&download=1", "download"))
                + r.card("Report", "<pre>" + r.e(
                    p.read_text(encoding="utf-8", errors="replace")[:400_000]) + "</pre>"))
        rows = data.reports()
        body = r.page_head("Reports", "Daily, weekly and monthly reviews written by trader-core")
        body += r.card(f"{len(rows)} files", r.table(rows, [
            ("name", "Report"), ("mtime", "Updated"), ("size", "Bytes")], "", {
            "name": lambda v, row: r.link(f"/reports?name={quote(str(v))}", v)})) if rows \
            else r.card("Archive", r.empty(
                "No reports yet", "trader-core writes the daily review at 4:30 PM ET, the weekly "
                "on Fridays at 5:00 and the monthly at month end into reports/.", icon="▤"))
        return await html(req, ses, "Reports", body)

    @page
    async def compliance(req: Request, ses: Session) -> Response:
        c = data.compliance()
        lock_cols = [("ticker", "Ticker"), ("lock_type", "Type"), ("start", "From"),
                     ("until", "Unlocks"), ("reason", "Cause")]
        lfmt = {"ticker": lambda v, row: r.stock_link(v),
                "lock_type": lambda v, row: r.chip(v, "warn")}
        body = (r.page_head("Compliance", "Wash-sale locks, tax lots and disallowed losses")
                + r.tiles([r.tile("Active locks", str(len(c["active"]))),
                           r.tile("Tax lots", str(len(c["lots"]))),
                           r.tile("Wash sales", str(len(c["wash"])))])
                + r.card("Active locks", r.table(c["active"], lock_cols, fmt=lfmt) if c["active"]
                         else r.empty("No active locks", "Every universe ticker is tradable. A "
                                      "loss locks its ticker (and options) for 31 days.",
                                      icon="✓"))
                + r.card("Recent locks", r.table(c["recent"], lock_cols, "none yet", lfmt))
                + r.card("Tax lots (adjusted basis)", r.table(c["lots"], [
                    ("ticker", "Ticker"), ("acquired_at", "Acquired"), ("qty", "Qty"),
                    ("remaining_qty", "Open"), ("cost", "Cost"), ("adjusted_basis", "Adj. basis"),
                    ("realized_pnl", "Realized"), ("disallowed_loss", "Disallowed"),
                    ("route", "Route")], "No lots yet: a lot is created with each fill."))
                + r.card("Wash sales (disallowed losses)", r.table(c["wash"], empty="None.")))
        return await html(req, ses, "Compliance", body)

    # -- Journal, scanner, latency (UI-29..UI-31) -------------------------------------------------
    @page
    async def notes_get(req: Request, ses: Session) -> Response:
        notes = data.notes()
        tags: dict[str, int] = {}
        for n in notes:
            for t in n.get("tags") or []:
                tags[t] = tags.get(t, 0) + 1
        closed = data.closed_positions(30)
        rs = [float(p.get("r_multiple") or 0) for p in closed]
        body = (r.page_head("Journal", "Your notes and tags on each trade")
                + r.tiles([r.tile("Closed trades (30 days)", str(len(closed))),
                           r.tile("Total R", f"{sum(rs):+.2f}R" if rs else "—"),
                           r.tile("Notes", str(len(notes))),
                           r.tile("Top tag", r.e(max(tags, key=tags.__getitem__))
                                  if tags else "—")])
                + r.card("Closed trades", r.table(closed, [
                    ("opened_at", "Date"), ("ticker", "Ticker"), ("setup", "Setup"),
                    ("route", "Route"), ("r_multiple", "R"), ("exit_reason", "Exit"),
                    ("position_id", "")], "", {
                    "position_id": lambda v, row: r.link(f"/story/{v}", "story / add note"),
                    "ticker": lambda v, row: r.stock_link(v),
                    "r_multiple": lambda v, row: r.num(v, 2, True)}) if closed else r.empty(
                    "No closed trades in 30 days", "Each closed trade (system, co-pilot or "
                    "shadow) gets a row here; open its story to add notes, tags and a "
                    "screenshot.", icon="✎"))
                + r.card("Tag statistics", " ".join(r.chip(f"{k}: {v}") for k, v in sorted(
                    tags.items(), key=lambda kv: -kv[1])) or '<p class="muted small">No tags '
                    "yet.</p>")
                + r.card("Notes", r.notes_list(notes)))
        return await html(req, ses, "Journal", body)

    @page
    async def notes_post(req: Request, ses: Session) -> Response:
        f = await req.form()
        shot = f.get("screenshot")
        b64 = None
        if shot is not None and hasattr(shot, "read"):
            raw = await shot.read()
            if raw:
                if len(raw) > MAX_SCREENSHOT or not raw[:8].startswith((b"\x89PNG", b"\xff\xd8")):
                    return await html(req, ses, "Journal", "",
                                      flash="screenshot must be a PNG/JPEG under 2 MB")
                b64 = base64.b64encode(raw).decode()
        ref_id = str(f.get("ref_id", ""))[:64]
        data.add_note(str(f.get("ref_type", "proposal")), ref_id,
                      [str(t) for t in f.getlist("tags") if str(t) in r.TAGS],
                      str(f.get("note", "")), b64)
        return RedirectResponse(f"/story/{quote(ref_id)}" if ref_id else "/notes",
                                status_code=303)

    @page
    async def scanner(req: Request, ses: Session) -> Response:
        sc = await data.core.get("/scanner")
        labels: dict[str, str] = {}
        for a in reversed(data.alerts_today()):
            labels[a["ticker"]] = f"{a['label']} {a['checklist_score']}/10"
        rows = [{**x, "alert": labels.get(x["ticker"], "")} for x in (sc or {}).get("rows", [])]
        live = [x for x in rows if x.get("last") is not None]
        body = r.page_head("Scanner", "The universe and in-play names, live from trader-core")
        if live:
            body += r.card("Day change", r.heatmap([
                {"ticker": x["ticker"], "value": x.get("change_pct"),
                 "sub": f"RVOL {r.num(x.get('rvol'), 1)}" if x.get("rvol") else "",
                 "href": f"/stock?symbol={quote(x['ticker'])}"} for x in live]))
            body += r.card("Ranked by move", r.table(live, [
                ("ticker", "Ticker"), ("last", "Last"), ("change_pct", "Day %"),
                ("rvol", "RVOL"), ("rs_qqq", "vs QQQ"), ("vwap_dist_pct", "From VWAP %"),
                ("alert", "Last alert"), ("in_play", "In play")], "", {
                "ticker": lambda v, row: r.stock_link(v),
                "change_pct": lambda v, row: r.delta(v),
                "rs_qqq": lambda v, row: r.delta(v, " pts"),
                "vwap_dist_pct": lambda v, row: r.num(v, 2, True),
                "in_play": lambda v, row: r.chip("in play", "info") if v else ""}))
        else:
            _, recap = recap_latest(data)
            stocks = [x for x in recap if x.get("kind") == "stock"]
            why = ("trader-core's API is not answering: " + r.e(data.core.last_error)
                   if sc is None else "No live prices yet: the stream runs on market days from "
                   "9:30 ET (pre-market from 4:00 for the in-play scan).")
            body += r.card("Live", r.empty("Scanner idle", why, r.recap_hint(ses.csrf), "▦"))
            if stocks:
                body += r.card("Last session (from the recap)", r.heatmap([
                    {"ticker": x["ticker"], "value": x.get("ret_1d"),
                     "sub": f"5d {r.num(x.get('ret_5d'), 1, True)}%",
                     "href": f"/stock?symbol={quote(x['ticker'])}"} for x in stocks]))
        return await html(req, ses, "Scanner", body)

    @page
    async def latency(req: Request, ses: Session) -> Response:
        lat = await data.core.get("/latency")
        rows = data.latency_today()
        missed = [x for x in rows if x.get("outcome") == "MISSED"]
        by_reason: dict[str, int] = {}
        for m in missed:
            by_reason[m["reason_code"] or "?"] = by_reason.get(m["reason_code"] or "?", 0) + 1
        shadow = {x["setup_id"]: x for x in data.shadow_today()}
        for x in rows:
            sh = shadow.get(x.get("setup_id"))
            x["shadow"] = f"{sh['exit_reason'] or 'open'} {sh['exit_price'] or ''}" if sh else ""
        lats = sorted(float(x["latency_s"]) for x in rows if x.get("latency_s") is not None)
        body = (r.page_head("Latency and missed trades", "Trigger-to-order time and every missed "
                            "trigger with the shadow result")
                + r.tiles([r.tile("Triggers today", str(len(rows))),
                           r.tile("Orders", str(len(rows) - len(missed))),
                           r.tile("Missed", str(len(missed))),
                           r.tile("p95 trigger→order", f"{lats[int(len(lats) * 0.95) - 1]:.1f} s"
                                  if lats else "—", '<span class="muted small">budget 10 s'
                                  '</span>')])
                + (f'<p class="small">{r.e((lat or {}).get("summary", ""))}</p>' if lat else "")
                + ('<p class="small">Missed by reason: '
                   + " ".join(r.chip(f"{k}: {v}", "warn") for k, v in by_reason.items())
                   + "</p>" if by_reason else "")
                + r.card("Triggers", r.table(rows, [
                    ("trigger_ts", "Trigger"), ("ticker", "Ticker"), ("setup", "Setup"),
                    ("mode", "Mode"), ("latency_s", "s"), ("model_s", "Model s"),
                    ("owner_s", "Owner s"), ("prearmed", "Pre-armed"), ("outcome", "Outcome"),
                    ("reason_code", "Reason"), ("shadow", "Shadow result")], "", {
                    "ticker": lambda v, row: r.stock_link(v),
                    "outcome": lambda v, row: r.chip(v, "ok" if v == "ORDER" else "warn")})
                    if rows else r.empty(
                        "No triggers today", "Each trigger inside a window is timed from the "
                        "candle close to the order (LAT-1); missed ones are kept with their "
                        "reason and followed as shadow trades (SH-1).", icon="⏱")))
        return await html(req, ses, "Latency", body)

    # -- Config, model, runs (UI-18..UI-20) -----------------------------------------------------
    @page
    async def config(req: Request, ses: Session) -> Response:
        import difflib

        c = data.config_view()
        snap = c["snapshot"]
        snap_text = (snap or {}).get("yaml_text") or ""
        diff = ("".join(difflib.unified_diff(snap_text.splitlines(True),
                                             c["file"].splitlines(True), "effective today",
                                             c["file_name"]))
                if snap_text else "(today's snapshot stores only the config hash)")
        body = (r.page_head("Config", f"Effective {r.e((snap or {}).get('effective_date'))} · "
                            f"hash {r.e(((snap or {}).get('yaml_hash') or '')[:12])}. Edits apply "
                            "next session (SR-5); edit config/config.yaml on the host (DV-3)")
                + r.card("Pending edits (diff)", f"<pre>{r.e(diff)}</pre>")
                + r.card(c["file_name"], f"<pre>{r.e(c['file'])}</pre>"))
        return await html(req, ses, "Config", body)

    @page
    async def model(req: Request, ses: Session) -> Response:
        from trader.hardware import PROFILES

        llm = await data.ollama()
        rows = [{"model": p.tag, "weights_gb": p.weights_gb,
                 "vram_needed_gb_8k": p.vram_needed_gb(), "tier": p.tier, "use": p.use}
                for p in PROFILES]
        vram = llm.get("vram_gb")
        body = (r.page_head("Model and hardware", "Qwen on the local GPU; change it with "
                            + r.code("./run.sh model --apply"))
                + r.tiles([r.tile("Configured", r.e(llm.get("model"))),
                           r.tile("Loaded", r.e(", ".join(llm.get("loaded") or []) or "no")),
                           r.tile("VRAM", f"{vram:.1f} GB" if vram is not None else "—",
                                  r.meter((vram or 0) / 8) + '<span class="muted small">of 8 GB'
                                  '</span>'),
                           r.tile("p95 today", f"{llm['p95_ms']} ms" if llm.get("p95_ms")
                                  else "—", f'<span class="muted small">'
                                  f'{r.e(llm.get("calls_today", 0))} calls</span>')])
                + r.card("Models this system supports", r.table(rows),
                         sub="Reference machine: RTX 4060 Laptop 8 GB (docs/HARDWARE.md)"))
        return await html(req, ses, "Model", body)

    @page
    async def runs(req: Request, ses: Session) -> Response:
        rs = data.runs()
        a, b = req.query_params.get("a"), req.query_params.get("b")
        comp = ""
        if a and b:
            ra = next((x for x in rs if x["name"] == a), None)
            rb = next((x for x in rs if x["name"] == b), None)
            if ra and rb:
                keys = sorted(set(ra["summary"]) | set(rb["summary"]))
                comp = r.card(f"{a} vs {b}", r.table([
                    {"metric": k, a: ra["summary"].get(k), b: rb["summary"].get(k)}
                    for k in keys if not isinstance(ra["summary"].get(k), dict | list)]))
        rows = [{"run": x["name"], **{k: v for k, v in x["summary"].items()
                                      if not isinstance(v, dict | list)}} for x in rs]
        form = ('<form method="get" action="/runs" class="filters"><input name="a" '
                'placeholder="run A"><input name="b" placeholder="run B"><button>Compare'
                '</button></form>')
        body = (r.page_head("Replay and backtest runs") + form + comp
                + (r.card(f"{len(rows)} runs", r.table(rows)) if rows else r.card("Runs", r.empty(
                    "No runs yet", "Run " + r.code("./run.sh replay 10") + " (synthetic days) or "
                    + r.code("./run.sh backtest") + " (real history); results land in runs/.",
                    icon="▶"))))
        return await html(req, ses, "Runs", body)

    routes = [
        Route("/login", login_get),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/", home),
        Route("/events", events),
        Route("/api/home", api_home),
        Route("/api/quote", api_quote),
        Route("/stock", stock),
        Route("/market", market),
        Route("/ratings", ratings_page),
        Route("/news", news_page),
        Route("/ask", ask_get),
        Route("/ask", ask_post, methods=["POST"]),
        Route("/ask/clear", ask_clear, methods=["POST"]),
        Route("/approvals", approvals),
        Route("/controls", controls),
        Route("/cmd", cmd, methods=["POST"]),
        Route("/stories", stories),
        Route("/story/{pid}", story),
        Route("/copilot", copilot),
        Route("/copilot/fill", copilot_fill, methods=["POST"]),
        Route("/options", options),
        Route("/options/{sid}", option_detail),
        Route("/alerts", alerts),
        Route("/ideas", ideas),
        Route("/technical", technical),
        Route("/catalysts", catalysts),
        Route("/macro", macro_page),
        Route("/momentum", momentum_page),
        Route("/reports", reports),
        Route("/compliance", compliance),
        Route("/notes", notes_get),
        Route("/notes", notes_post, methods=["POST"]),
        Route("/scanner", scanner),
        Route("/latency", latency),
        Route("/config", config),
        Route("/model", model),
        Route("/runs", runs),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(SecurityHeaders)
    return app


# --- helpers --------------------------------------------------------------------------------------


def _vwap_series(bars: list[dict[str, Any]]) -> list[float | None]:
    pv = vol = 0.0
    out: list[float | None] = []
    for b in bars:
        pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        vol += b["v"]
        out.append(pv / vol if vol else None)
    return out


def _fill_markers(
    data: ConsoleData, sym: str, bars: list[dict[str, Any]], pid: str | None = None
) -> list[dict[str, Any]]:
    if not bars:
        return []
    try:
        start = datetime.fromisoformat(bars[0]["t"])
    except (KeyError, ValueError):
        return []
    fills = [f for f in data._since(s.fills, "ts", start, 100) if f["ticker"] == sym]
    if pid:
        oids = {o["client_order_id"] for o in data.rows(s.orders, {"proposal_id": pid})}
        fills = [f for f in fills if f["client_order_id"] in oids]
    times = [datetime.fromisoformat(b["t"]) for b in bars]
    out = []
    for f in fills:
        i = min(range(len(times)), key=lambda k: abs((times[k] - f["ts"]).total_seconds()))
        out.append(
            {
                "i": i,
                "price": float(f["price"]),
                "kind": "buy" if f["side"] == "buy" else "sell",
                "label": f"{f['side']} {f['qty']} @ {f['price']} {to_et(f['ts']):%H:%M}",
            }
        )
    return out


def _system_status(data: ConsoleData, sym: str) -> dict[str, Any]:
    cfg = data.cfg
    group = next((n for n, g in cfg.universe.correlated_groups.items() if sym in g.tickers), None)
    now = data.now()
    locks = [
        f"{x['lock_type']} until {to_et(x['until']):%Y-%m-%d}"
        for x in data.rows(s.locks, {"ticker": sym})
        if x["start"] <= now < x["until"]
    ]
    ideas = [f"{x['plan_for']} {x['status']}" for x in data.ideas() if x["ticker"] == sym]
    trades = [p for p in data.rows(s.positions, {"ticker": sym}) if p.get("closed_at")]
    rsum = sum(float(p["r_multiple"] or 0) for p in trades)
    return {
        "universe": sym in cfg.universe.tickers,
        "group": group,
        "locks": locks,
        "ideas": ideas,
        "trades_summary": f"{len(trades)} trades, total {rsum:+.2f}R" if trades else "",
    }


def payoff_svg(sug: Any, row: dict[str, Any]) -> str:
    from trader.options import pricing
    from trader.options.data import years_to

    v = sug.view
    em = sug.expected_move or v.spot * 0.05
    lo, hi = v.spot - 3 * em, v.spot + 3 * em
    prices = [lo + (hi - lo) * i / 80 for i in range(81)]

    def value(p: float, t: float) -> float:
        return float(
            sum(
                lg.side * pricing.price(lg.quote.kind, p, lg.quote.strike, t, lg.quote.iv or 0.3)
                for lg in sug.legs
            )
        )

    t_exit = years_to(sug.expiry, sug.exit_at)
    exp_pl = [(value(p, 0.0) - sug.est_fill) * 100 for p in prices]
    exit_pl = [(value(p, t_exit) - sug.est_fill) * 100 for p in prices]
    marks = {"now": v.spot, "T1": v.t1, "invalidation": v.invalidation}
    return (
        charts.payoff(prices, exp_pl, exit_pl, marks, (v.spot - em, v.spot + em))
        + '<p class="muted small">P/L per contract at expiry and at the planned exit '
        f"({r.ts(sug.exit_at, '%b %d %H:%M')}), Black-Scholes at entry IV (estimate). Shaded: "
        "1x expected move.</p>"
    )
