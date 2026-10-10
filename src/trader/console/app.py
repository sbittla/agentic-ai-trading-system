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
from trader.console import render as r
from trader.console.ask import Asker
from trader.console.data import ConsoleData
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

    # -- UI-1..UI-4b home + SSE (UI-22) -----------------------------------------------------------
    async def home_data() -> dict[str, Any]:
        st = await data.status()
        return {
            "status": st,
            "pending": len(data.pending_approvals()),
            "incidents": len(data.incidents_open()),
        }

    @page
    async def home(req: Request, ses: Session) -> Response:
        st = await data.status()
        body = r.home(
            st,
            data.plans(data.today()),
            data.ideas(),
            data.incidents_open(),
            data.heartbeats(),
            await data.ollama(),
            len(data.pending_approvals()),
            ses.csrf,
            data.now(),
        )
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

    # -- UI-4..UI-6a stock ------------------------------------------------------------------------
    @page
    async def stock(req: Request, ses: Session) -> Response:
        sym = req.query_params.get("symbol", "").upper().strip().lstrip("$")
        if sym and not SYMBOL.match(sym):
            return await html(
                req,
                ses,
                "Stock",
                r.stock("", None, "", "", {}, [], [], {}, None, None, None, ses.csrf),
                flash="not a valid symbol",
            )
        if not sym:
            return await html(
                req,
                ses,
                "Stock",
                r.stock("", None, "", "", {}, [], [], {}, None, None, None, ses.csrf),
            )
        q, bars, daily, ck, iv, chain = await asyncio.gather(
            data.core.get("/quote", symbol=sym),
            data.core.get("/bars", symbol=sym),
            data.core.get("/daily", symbol=sym),
            data.core.get("/checklist", symbol=sym),
            data.core.get("/options/iv", symbol=sym),
            data.core.get("/options/chain", symbol=sym),
        )
        intraday = (bars or {}).get("bars") or []
        dbars = (daily or {}).get("bars") or []
        lv = (bars or {}).get("levels") or {}
        markers = _fill_markers(data, sym, intraday)
        chart_html = charts.candles(
            intraday,
            vwap=_vwap_series(intraday),
            title=f"{sym} 1-minute",
            levels={
                "prior high": lv.get("prior_high"),
                "prior low": lv.get("prior_low"),
                "prior close": lv.get("prior_close"),
                "PM high": lv.get("pm_high"),
            },
            opening_range=(bars or {}).get("or"),
            markers=markers,
        )
        daily_html = charts.candles(dbars[-90:], title=f"{sym} daily")
        ind = indicators.summary(intraday, dbars, (q or {}).get("prior_close"))
        news = data.rows(
            s.news_items, {"ticker": sym}, order_by="published_at", descending=True, limit=10
        )
        flags = [
            f
            for f in data.rows(
                s.event_flags, {"ticker": sym}, order_by="date", descending=True, limit=10
            )
        ]
        status = _system_status(data, sym)
        body = r.stock(
            sym,
            q,
            chart_html,
            daily_html,
            ind,
            news,
            flags,
            status,
            ck,
            iv,
            chain,
            ses.csrf,
            data.core.last_error,
        )
        return await html(req, ses, f"{sym}", body)

    # -- UI-7..UI-8d ask --------------------------------------------------------------------------
    @page
    async def ask_get(req: Request, ses: Session) -> Response:
        search = req.query_params.get("search", "")
        body = r.ask_page(
            ses.csrf, None, data.chats(q=search), req.query_params.get("q", ""), search
        )
        return await html(req, ses, "Ask", body)

    @page
    async def ask_post(req: Request, ses: Session) -> Response:
        q = str((await req.form()).get("q", "")).strip()
        ans = await ask.ask(q) if q else None
        return await html(req, ses, "Ask", r.ask_page(ses.csrf, ans, data.chats(), q))

    @page
    async def ask_clear(req: Request, ses: Session) -> Response:
        data.clear_chats()
        return RedirectResponse("/ask", status_code=303)

    # -- UI-9..UI-13 ------------------------------------------------------------------------------
    @page
    async def approvals(req: Request, ses: Session) -> Response:
        return await html(
            req,
            ses,
            "Approvals",
            r.approvals_page(ses.csrf, data.pending_approvals(), data.approvals_today()),
        )

    @page
    async def controls(req: Request, ses: Session) -> Response:
        return await html(
            req,
            ses,
            "Controls",
            r.controls_page(
                ses.csrf, await data.status(), data.audit(), req.query_params.get("suggest", "")
            ),
        )

    @page
    async def cmd(req: Request, ses: Session) -> Response:
        form = await req.form()
        name, arg = str(form.get("name", "")), str(form.get("arg", ""))
        res = await data.command(name, arg)
        if name.upper() in ("YES", "NO"):
            flash = ("Approval sent: " if res.get("ok") else "Approval refused: ") + str(
                res.get("reason")
            )
            return await html(
                req,
                ses,
                "Approvals",
                r.approvals_page(ses.csrf, data.pending_approvals(), data.approvals_today()),
                flash=flash,
            )
        return await html(
            req,
            ses,
            "Controls",
            r.controls_page(ses.csrf, await data.status(), data.audit(), result=res),
        )

    @page
    async def stories(req: Request, ses: Session) -> Response:
        rows = data.recent_proposals(7)
        t = r.table(
            rows,
            [
                ("ts", "Time"),
                ("ticker", "Ticker"),
                ("setup", "Setup"),
                ("result", "Result"),
                ("reason_code", "Reason"),
                ("qty", "Qty"),
                ("proposal_id", "Story"),
            ],
            "no decisions in 7 days",
            {
                "proposal_id": lambda v, row: r.link(f"/story/{v}", "story"),
                "reason_code": lambda v, row: r.e(f"{v} — {REASONS.get(v or '', '')}"),
            },
        )
        return await html(req, ses, "Trades and decisions (7 days)", t)

    @page
    async def story(req: Request, ses: Session) -> Response:
        pid = req.path_params["pid"][:64]
        d = data.story(pid)
        tick = (
            d["setup"][0]["ticker"]
            if d["setup"]
            else d["decisions"][0]["ticker"]
            if d["decisions"]
            else None
        )
        chart_html = ""
        if tick:
            b = await data.core.get("/bars", symbol=tick)
            bars = (b or {}).get("bars") or []
            chart_html = charts.candles(
                bars,
                vwap=_vwap_series(bars),
                title=f"{tick} story",
                markers=_fill_markers(data, tick, bars, pid),
            )
        return await html(
            req, ses, f"Trade story {tick or pid}", r.story_page(d, chart_html, REASONS, ses.csrf)
        )

    # -- UI-32 co-pilot ---------------------------------------------------------------------------
    @page
    async def copilot(req: Request, ses: Session) -> Response:
        cards = []
        for t in data.copilot_tickets():
            cards.append(
                f'<div class="card"><h3>{r.e(t["ticker"])} {r.e(t["setup"])} · '
                f"{r.ts(t['ts'], '%H:%M')}</h3><p>Plan: buy {r.e(t['qty'])} near "
                f"{r.num(t['entry'])} · stop {r.num(t['stop'])} · T1 {r.num(t['t1'])} · risk "
                f'{r.money(t["risk_usd"])}</p><form method="post" action="/copilot/fill" '
                f'class="inline">{r.csrf_field(ses.csrf)}<input type="hidden" name="ticker" '
                f'value="{r.e(t["ticker"])}"><label>Qty <input name="qty" size="5" '
                f'value="{r.e(t["qty"] or "")}"></label><label>Price <input name="price" '
                f'size="8" value="{r.e(t["entry"] or "")}"></label><button>I filled</button>'
                f"</form> {r.link('/story/' + t['proposal_id'], 'plan story')}</div>"
            )
        empty = '<p class="muted">no co-pilot plans today (mode.day_trades: copilot)</p>'
        body = (
            "".join(cards) or empty
        ) + '<p class="muted">"I filled" sends FILLED; exits then come as alerts (CP-2).</p>'
        return await html(req, ses, "Co-pilot tickets (UI-32)", body)

    @page
    async def copilot_fill(req: Request, ses: Session) -> Response:
        f = await req.form()
        tick, qty, price = (
            str(f.get("ticker", "")).upper(),
            str(f.get("qty", "")),
            str(f.get("price", "")),
        )
        if not (SYMBOL.match(tick) and qty.isdigit() and re.fullmatch(r"\d+(\.\d+)?", price)):
            return await html(req, ses, "Co-pilot", "", flash="ticker, qty and price needed")
        res = await data.command("FILLED", f"{tick} {qty} @ {price}")
        return await html(
            req,
            ses,
            "Co-pilot",
            f"<p>{r.e(res.get('reason'))}</p><p>{r.link('/copilot', 'back')}</p>",
        )

    # -- OP-17/OP-18 options ----------------------------------------------------------------------
    @page
    async def options(req: Request, ses: Session) -> Response:
        rows = data.suggestions(30)
        t = r.table(
            rows,
            [
                ("ts", "Time"),
                ("ticker", "Ticker"),
                ("direction", "View"),
                ("structure", "Structure"),
                ("expiry", "Expiry"),
                ("contracts", "Qty"),
                ("max_loss", "Max loss"),
                ("status", "Status"),
                ("mark", "P/L / contract"),
                ("pnl", "Result"),
                ("r_multiple", "R"),
                ("exit_reason", "Exit"),
                ("suggestion_id", "Payoff"),
            ],
            "no options suggestions in 30 days",
            {
                "suggestion_id": lambda v, row: r.link(f"/options/{v}", "payoff"),
                "max_loss": lambda v, row: r.money(v),
                "mark": lambda v, row: r.money(v),
                "pnl": lambda v, row: r.money(v),
            },
        )
        from trader.options import monitor

        rep = r.e(monitor.report(rows)) if rows else ""
        gate = monitor.promotion_status(rows)
        g = "".join(
            f"<li>{r.e(k[0])} {r.e(k[1])}: {r.passfail(ok)} {r.e(why)}</li>"
            for k, (ok, why) in sorted(gate.items())
        )
        return await html(
            req,
            ses,
            "Options suggestions (OP-18)",
            f"{t}<h2>Track record (A16-3)</h2><pre>{rep}</pre>"
            f'<h2>Promotion gate (A16-4)</h2><ul class="plain">{g or "<li>none</li>"}'
            f"</ul>",
        )

    @page
    async def option_detail(req: Request, ses: Session) -> Response:
        row = data.suggestion(req.path_params["sid"][:64])
        if row is None:
            return await html(req, ses, "Option", "", flash="not found")
        from trader.options.monitor import from_row

        sug = from_row(row)
        if sug is None:
            return await html(req, ses, "Option", f"<pre>{r.e(row['text'])}</pre>")
        body = f"<pre>{r.e(row['text'])}</pre>" + payoff_svg(sug, row)
        return await html(req, ses, f"Payoff {row['ticker']} {row['structure']} (OP-17)", body)

    # -- UI-14..UI-17 -----------------------------------------------------------------------------
    @page
    async def alerts(req: Request, ses: Session) -> Response:
        rows = data.alerts_today()
        rules, wl = data.bot_rules()
        body = (
            f"<p>{r.cmd_button(ses.csrf, 'ALERTS', 'ON', 'Signal alerts ON')} "
            f"{r.cmd_button(ses.csrf, 'ALERTS', 'OFF', 'Signal alerts OFF (mute)')}</p>"
            f"<h2>Today's alert log (trader-core, AL-1..AL-7)</h2>"
            + r.table(
                rows,
                [
                    ("ts", "Time"),
                    ("ticker", "Ticker"),
                    ("kind", "Signal"),
                    ("direction", "Dir"),
                    ("label", "Label"),
                    ("checklist_score", "Score"),
                    ("first_failed", "Failed"),
                    ("delivery", "Delivery"),
                    ("hit", "+1R first"),
                ],
                "no alerts today",
            )
            + f'<h2>Lightweight alerts bot rules (UI-14)</h2><p class="muted">{r.e(wl)} · '
            f"the bot is edited from Telegram (/alert, /watch, /mute) because it keeps its "
            f"own database; this view is read-only.</p>" + r.table(rules, empty="no rules")
        )
        return await html(req, ses, "Alerts", body)

    @page
    async def ideas(req: Request, ses: Session) -> Response:
        cur = data.ideas()
        hist = data.idea_history(20)
        cols = [
            ("plan_for", "For"),
            ("ticker", "Ticker"),
            ("status", "Status"),
            ("rank", "Rank"),
            ("score", "Score"),
            ("morning_setup", "Morning setup"),
            ("morning_trigger", "Trigger"),
            ("news_tone", "News"),
            ("close", "Close"),
            ("range_pos", "Range pos"),
        ]
        extra = [c for c in ("result", "grade", "triggered", "hit_t1") if hist and c in hist[0]]
        body = (
            f"<h2>Latest picks and screen (UI-15)</h2>{r.table(cur, cols, 'no ideas yet')}"
            f"<h2>Track record (20 days)</h2>"
            f"{r.table(hist, [*cols[:5], *[(c, c) for c in extra]], 'no history')}"
        )
        return await html(req, ses, "Next-day ideas", body)

    @page
    async def reports(req: Request, ses: Session) -> Response:
        name = req.query_params.get("name")
        if name:
            p = data.report_path(name)
            if p is None:
                return await html(req, ses, "Reports", "", flash="not found")
            if req.query_params.get("download"):
                return FileResponse(p, filename=p.name)
            return await html(
                req,
                ses,
                f"Report {p.name}",
                f"<p>{r.link(f'/reports?name={quote(name)}&download=1', 'download')}"
                f"</p><pre>{r.e(p.read_text(encoding='utf-8', errors='replace')[:400_000])}</pre>",
            )
        rows = data.reports()
        t = r.table(
            rows,
            [("name", "Report"), ("mtime", "Updated"), ("size", "Bytes")],
            "no reports yet (reports/ is written by trader-core)",
            {"name": lambda v, row: r.link(f"/reports?name={quote(str(v))}", v)},
        )
        return await html(req, ses, "Reports archive (UI-16)", t)

    @page
    async def compliance(req: Request, ses: Session) -> Response:
        c = data.compliance()
        lock_cols = [
            ("ticker", "Ticker"),
            ("lock_type", "Type"),
            ("start", "From"),
            ("until", "Unlocks"),
            ("reason", "Cause"),
        ]
        body = (
            f"<h2>Active locks</h2>{r.table(c['active'], lock_cols, 'no active locks')}"
            f"<h2>Recent locks (calendar)</h2>{r.table(c['recent'], lock_cols)}"
            f"<h2>Tax lots (adjusted basis)</h2>"
            + r.table(
                c["lots"],
                [
                    ("ticker", "Ticker"),
                    ("acquired_at", "Acquired"),
                    ("qty", "Qty"),
                    ("remaining_qty", "Open"),
                    ("cost", "Cost"),
                    ("adjusted_basis", "Adj. basis"),
                    ("realized_pnl", "Realized"),
                    ("disallowed_loss", "Disallowed"),
                    ("route", "Route"),
                ],
                "no lots",
            )
            + f"<h2>Wash sales (disallowed losses)</h2>{r.table(c['wash'], empty='none')}"
        )
        return await html(req, ses, "Compliance (UI-17)", body)

    # -- UI-29..UI-31 -----------------------------------------------------------------------------
    @page
    async def notes_get(req: Request, ses: Session) -> Response:
        notes = data.notes()
        tags: dict[str, int] = {}
        for n in notes:
            for t in n.get("tags") or []:
                tags[t] = tags.get(t, 0) + 1
        stats = ", ".join(f"{k}: {v}" for k, v in sorted(tags.items(), key=lambda kv: -kv[1]))
        closed = data.closed_positions(30)
        t = r.table(
            closed,
            [
                ("opened_at", "Date"),
                ("ticker", "Ticker"),
                ("setup", "Setup"),
                ("route", "Route"),
                ("r_multiple", "R"),
                ("exit_reason", "Exit"),
                ("position_id", "Note"),
            ],
            "no closed trades in 30 days",
            {"position_id": lambda v, row: r.link(f"/story/{v}", "story / add note")},
        )
        return await html(
            req,
            ses,
            "Owner journal (UI-29)",
            f"<p>Tag statistics (30 days): {r.e(stats or 'none yet')}</p>{t}"
            f"<h2>Notes</h2>{r.notes_list(notes)}",
        )

    @page
    async def notes_post(req: Request, ses: Session) -> Response:
        f = await req.form()
        shot = f.get("screenshot")
        b64 = None
        if shot is not None and hasattr(shot, "read"):
            raw = await shot.read()
            if raw:
                if len(raw) > MAX_SCREENSHOT or not raw[:8].startswith((b"\x89PNG", b"\xff\xd8")):
                    return await html(
                        req, ses, "Journal", "", flash="screenshot must be a PNG/JPEG under 2 MB"
                    )
                b64 = base64.b64encode(raw).decode()
        ref_id = str(f.get("ref_id", ""))[:64]
        data.add_note(
            str(f.get("ref_type", "proposal")),
            ref_id,
            [str(t) for t in f.getlist("tags") if str(t) in r.TAGS],
            str(f.get("note", "")),
            b64,
        )
        return RedirectResponse(f"/story/{quote(ref_id)}" if ref_id else "/notes", status_code=303)

    @page
    async def scanner(req: Request, ses: Session) -> Response:
        sc = await data.core.get("/scanner")
        labels: dict[str, str] = {}
        for a in reversed(data.alerts_today()):
            labels[a["ticker"]] = f"{a['label']} {a['checklist_score']}/10"
        rows = [{**x, "alert": labels.get(x["ticker"], "")} for x in (sc or {}).get("rows", [])]
        t = r.table(
            rows,
            [
                ("ticker", "Ticker"),
                ("last", "Last"),
                ("change_pct", "% chg"),
                ("rvol", "RVOL"),
                ("rs_qqq", "RS vs QQQ"),
                ("vwap_dist_pct", "% from VWAP"),
                ("alert", "Last alert (AL-2)"),
                ("in_play", "In play (UN-1)"),
            ],
            f"scanner unavailable: {data.core.last_error}",
            {
                "ticker": lambda v, row: r.link(f"/stock?symbol={quote(str(v))}", v),
                "change_pct": lambda v, row: r.num(v, 2, True),
                "rs_qqq": lambda v, row: r.num(v, 2, True),
                "vwap_dist_pct": lambda v, row: r.num(v, 2, True),
            },
        )
        return await html(req, ses, "Live scanner (UI-30)", t)

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
        body = (
            f"<p>{r.e((lat or {}).get('summary', data.core.last_error))}</p>"
            f"<p>Missed by reason: {r.e(by_reason or 'none')}</p>"
            + r.table(
                rows,
                [
                    ("trigger_ts", "Trigger"),
                    ("ticker", "Ticker"),
                    ("setup", "Setup"),
                    ("mode", "Mode"),
                    ("latency_s", "s"),
                    ("model_s", "model s"),
                    ("owner_s", "owner s"),
                    ("prearmed", "pre-armed"),
                    ("outcome", "Outcome"),
                    ("reason_code", "Reason"),
                    ("shadow", "Shadow (SH-1)"),
                ],
                "no triggers today",
            )
        )
        return await html(req, ses, "Latency and missed trades (UI-31)", body)

    # -- UI-18..UI-20 -----------------------------------------------------------------------------
    @page
    async def config(req: Request, ses: Session) -> Response:
        c = data.config_view()
        snap = c["snapshot"]
        import difflib

        snap_text = (snap or {}).get("yaml_text") or ""
        diff = (
            "".join(
                difflib.unified_diff(
                    snap_text.splitlines(True),
                    c["file"].splitlines(True),
                    "effective today",
                    c["file_name"],
                )
            )
            if snap_text
            else "(today's snapshot stores only the config hash)"
        )
        body = (
            f"<p>Effective config: {r.e((snap or {}).get('effective_date'))} · hash "
            f"{r.e(((snap or {}).get('yaml_hash') or '')[:12])}. Edits apply next session (SR-5); "
            f"edit config/config.yaml on the host, never here (DV-3).</p>"
            f"<h2>Pending edits (diff)</h2><pre>{r.e(diff)}</pre>"
            f"<h2>{r.e(c['file_name'])}</h2><pre>{r.e(c['file'])}</pre>"
        )
        return await html(req, ses, "Config (UI-18)", body)

    @page
    async def model(req: Request, ses: Session) -> Response:
        from trader.hardware import PROFILES

        llm = await data.ollama()
        rows = [
            {
                "model": p.tag,
                "weights_gb": p.weights_gb,
                "vram_needed_gb_8k": p.vram_needed_gb(),
                "tier": p.tier,
                "use": p.use,
            }
            for p in PROFILES
        ]
        body = (
            f"<p>Configured: <b>{r.e(llm.get('model'))}</b> · loaded "
            f"{r.e(', '.join(llm.get('loaded') or []) or 'no')} · VRAM "
            f"{r.num(llm.get('vram_gb'), 1)} GB · p95 {r.e(llm.get('p95_ms') or '—')} ms</p>"
            f'{r.table(rows)}<p class="muted">The reference machine is an RTX 4060 Laptop '
            f"8 GB (docs/HARDWARE.md). Change the model with ./run.sh model --apply.</p>"
        )
        return await html(req, ses, "Model and hardware (UI-19)", body)

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
                comp = r.table(
                    [
                        {"metric": k, a: ra["summary"].get(k), b: rb["summary"].get(k)}
                        for k in keys
                        if not isinstance(ra["summary"].get(k), dict | list)
                    ]
                )
        rows = [
            {
                "run": x["name"],
                **{k: v for k, v in x["summary"].items() if not isinstance(v, dict | list)},
            }
            for x in rs
        ]
        form = (
            '<form method="get" action="/runs" class="inline"><input name="a" placeholder='
            '"run A"><input name="b" placeholder="run B"><button>Compare</button></form>'
        )
        return await html(
            req,
            ses,
            "Replay and backtest runs (UI-20)",
            f"{form}{comp}{r.table(rows, empty='no runs yet (./run.sh replay)')}",
        )

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
        + '<p class="muted">Green: P/L per contract at expiry · blue: at the planned exit '
        f"({r.ts(sug.exit_at, '%b %d %H:%M')}), Black-Scholes at entry IV (estimate). Shaded: "
        "1x expected move.</p>"
    )
