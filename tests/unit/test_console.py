"""Owner Console (UI_REQUIREMENTS.md): every page renders against a journal with a fake
trader-core API (UI-26), plus auth, CSRF, commands, Ask and the core internal API."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from tests.conftest import PLAIN_DAY, et
from trader.config import default_config
from trader.console.app import CSP, Auth, create_app
from trader.console.ask import AnswerOut, Asker, ToolChoice, keyword_route, numbers_ok
from trader.console.data import ConsoleData, CoreClient, Paths
from trader.interfaces import LLMResult
from trader.journal import schema as s
from trader.journal.store import SqlJournal

NOW = et(PLAIN_DAY, "11:00")          # outside the entry windows
PW = "correct horse battery"


def bars(n: int = 60, start: float = 100.0) -> list[dict[str, Any]]:
    t0 = et(PLAIN_DAY, "09:30")
    out = []
    for i in range(n):
        o = start + i * 0.05
        out.append({"t": (t0 + timedelta(minutes=i)).astimezone(UTC).isoformat(), "o": o,
                    "h": o + 0.2, "l": o - 0.15, "c": o + 0.1, "v": 1000 + i})
    return out


STATUS = {"environment": "paper", "health": {"alpaca": "green", "ollama": "amber"},
          "health_detail": {"alpaca": "ok", "ollama": "slow"}, "kill_switch": False,
          "equity": "25012.50", "day_start_equity": "25000", "day_pnl": "12.50",
          "trades_today": 1, "max_trades": 3, "loss_limit_pct": 1.5, "halted": None,
          "paused": [], "regime": "normal", "window": None,
          "positions": [{"position_id": "pid-1", "ticker": "NVDA", "qty": 40, "style": "day",
                         "entry": "190.00", "stop": "189.20", "t1": "191.20", "last": 190.4,
                         "unrealized": "16.00", "r": 0.5, "opened_at": NOW.isoformat()}],
          "locks": [], "options_open": 0}

CORE = {
    "/status": STATUS,
    "/quote": {"symbol": "NVDA", "source": "stream", "last": 190.4, "prior_close": 188.0,
               "change_pct": 1.28, "vwap": 189.9, "rvol": 1.8, "volume": 120000, "bid": 190.39,
               "ask": 190.41, "spread_pct": 0.01, "hod": 191.0, "lod": 188.5, "halted": False,
               "universe": True},
    "/bars": {"symbol": "NVDA", "tf": "1Min", "bars": bars(), "vwap": [], "or": [100.4, 99.9],
              "levels": {"prior_high": 103.0, "prior_low": 98.0, "prior_close": 99.5}},
    "/daily": {"symbol": "NVDA", "bars": bars(80, 90.0)},
    "/checklist": {"symbol": "NVDA", "tradable": True, "items": [
        {"id": "MF-1", "passed": True, "value": "QQQ above VWAP: True", "threshold": "x"},
        {"id": "MF-3", "passed": False, "value": 1.1, "threshold": "RVOL >= 1.5"}],
        "planned": [], "regime": "normal"},
    "/scanner": {"rows": [{"ticker": "NVDA", "last": 190.4, "change_pct": 1.28, "rvol": 1.8,
                           "rs_qqq": 0.6, "vwap_dist_pct": 0.26, "in_play": False}]},
    "/latency": {"summary": "LAT-1 p95 4.2 s (3 orders)", "health": "green", "detail": "",
                 "prearmed": 1},
    "/options/iv": {"symbol": "NVDA", "spot": 190.4, "atm_iv_30d": 0.42, "iv_rank": 55.0,
                    "iv_pct": 60.0, "history": "short history: 30 days", "term_front": 0.45,
                    "term_30d": 0.42, "skew": 0.03,
                    "expected_moves": {"2026-10-23": 6.1}, "text": "IV NVDA"},
    "/options/chain": {"symbol": "NVDA", "spot": 190.4, "expiry": "2026-10-23",
                       "expiries": ["2026-10-23"], "rows": [
                           {"symbol": "NVDA261023C00190000", "kind": "C", "strike": 190.0,
                            "bid": 4.1, "ask": 4.3, "mid": 4.2, "iv": 0.42, "delta": 0.52,
                            "oi": 5000, "volume": 900, "liquid": True, "why": ""},
                           {"symbol": "NVDA261023P00190000", "kind": "P", "strike": 190.0,
                            "bid": 3.8, "ask": 4.6, "mid": 4.2, "iv": 0.44, "delta": -0.48,
                            "oi": 100, "volume": 5, "liquid": False, "why": "OI 100 < 500"}]},
}


def core_transport(up: bool = True) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if not up:
            raise httpx.ConnectError("down")
        body = CORE.get(req.url.path)
        return httpx.Response(200, json=body) if body is not None else \
            httpx.Response(404, json={"error": "not found"})
    return httpx.MockTransport(handler)


def ollama_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"models": [
        {"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 6_000_000_000}]}))


def seed(j: SqlJournal) -> None:
    t = NOW - timedelta(minutes=60)
    j.insert(s.game_plans, {"date": PLAIN_DAY, "window": "morning", "ticker": "NVDA",
                            "setup": "ORB", "trigger": 190.0, "stop": 189.2, "target": 191.2,
                            "source": "llm"})
    j.insert(s.next_day_ideas, {"run_at": t, "plan_for": PLAIN_DAY + timedelta(days=3),
                                "session_date": PLAIN_DAY, "ticker": "AMD", "rank": 1,
                                "status": "pick", "score": 71, "morning_setup": "ORB",
                                "morning_trigger": 151.0})
    j.insert(s.incidents, {"ts": t, "type": "NOTIFIER_DOWN", "detail": "heartbeat 3 min"})
    j.insert(s.setups, {"id": "set-1", "ts": t, "ticker": "NVDA", "style": "day",
                        "window": "morning", "setup_name": "ORB", "trigger": Decimal("190"),
                        "entry": Decimal("190.05"), "stop": Decimal("189.2"),
                        "t1": Decimal("191.2"), "t2": Decimal("192"), "score": 80})
    j.insert(s.proposals, {"proposal_id": "pid-1", "setup_id": "set-1", "ts": t,
                           "ticker": "NVDA", "json": {"setup": "ORB", "entry": "190.05",
                                                      "stop": "189.20", "target_1": "191.20",
                                                      "reasoning": "clean ORB"},
                           "valid": True})
    j.insert(s.reviews, {"proposal_id": "pid-1", "ts": t, "verdict": "agree",
                         "main_risk": "resistance at 192", "rules_broken": []})
    j.insert(s.decisions, {"proposal_id": "pid-1", "ts": t, "ticker": "NVDA", "setup": "ORB",
                           "window": "morning", "style": "day", "result": "APPROVE",
                           "reason_code": "NT_CK6", "qty": 40, "risk_usd": Decimal("34"),
                           "checklist_json": [{"id": "CK-1", "passed": True, "detail": ""}]})
    j.insert(s.approvals, {"proposal_id": "pid-1", "code": "SECRET42", "sent_at": NOW
                           - timedelta(seconds=20)})
    j.insert(s.orders, {"client_order_id": "c1", "broker_id": "b1", "proposal_id": "pid-1",
                        "leg": "entry", "type": "limit", "side": "buy", "ticker": "NVDA",
                        "qty": 40, "status": "filled", "ts": t})
    j.insert(s.fills, {"broker_id": "b1", "client_order_id": "c1", "ticker": "NVDA",
                       "side": "buy", "ts": et(PLAIN_DAY, "09:45"), "qty": 40,
                       "price": Decimal("190.05")})
    j.insert(s.positions, {"position_id": "pid-0", "ticker": "AAPL", "style": "day",
                           "window": "morning", "setup": "ORB", "opened_at": t,
                           "closed_at": t + timedelta(minutes=20), "qty": 10,
                           "avg_entry": Decimal("100"), "pnl": Decimal("15"),
                           "r_multiple": 1.5, "exit_reason": "EXIT_T2", "route": "broker"})
    j.insert(s.locks, {"ticker": "MSFT", "lock_type": "WASH", "start": t - timedelta(days=1),
                       "until": t
                       + timedelta(days=30), "reason": "loss 120 on MSFT"})
    j.insert(s.alerts_log, {"ts": t, "ticker": "NVDA", "kind": "orb_up", "direction": "long",
                            "label": "qualified", "checklist_score": 10, "delivery": "sent",
                            "text": "orb"})
    j.insert(s.latency, {"proposal_id": "pid-1", "setup_id": "set-1", "ticker": "NVDA",
                         "setup": "ORB", "mode": "approve", "trigger_ts": t, "order_ts": t,
                         "latency_s": 4.2, "outcome": "ORDER"})
    j.insert(s.decisions, {"proposal_id": "pid-2", "ts": t, "ticker": "AMD", "setup": "ORB",
                           "window": "morning", "style": "day", "result": "PLAN",
                           "reason_code": "COPILOT_PLAN", "qty": 20, "risk_usd": Decimal("40"),
                           "checklist_json": []})
    j.insert(s.outbox, {"created_at": NOW - timedelta(seconds=30), "source": "core",
                        "channel": "heartbeat", "payload": {"service": "core"}})
    j.insert(s.status_snapshots, {"ts": NOW - timedelta(minutes=1), "json": STATUS})


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    seed(j)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "daily_2026-10-16.md").write_text("# Daily\nP&L +12.50\n")
    (tmp_path / "secret.txt").write_text("nope")
    runs = tmp_path / "runs"
    for name, exp in (("run-a", 0.2), ("run-b", 0.3)):
        (runs / name).mkdir(parents=True)
        (runs / name / "summary.json").write_text(json.dumps({"trades": 40,
                                                              "expectancy_r": exp}))
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text("mode:\n  environment: paper\n")
    bot = tmp_path / "alerts.sqlite"
    with sqlite3.connect(bot) as c:
        c.executescript("CREATE TABLE rules (id INTEGER PRIMARY KEY, symbol TEXT, kind TEXT, "
                        "value REAL); CREATE TABLE watchlist (symbol TEXT, position INTEGER);"
                        "INSERT INTO rules VALUES (1, 'TSLA', 'above', 250.0);"
                        "INSERT INTO watchlist VALUES ('TSLA', 0);")
    clock = [NOW]
    data = ConsoleData(j, CoreClient("http://core", core_transport()), default_config(),
                       Paths(tmp_path / "reports", runs, cfgdir, bot),
                       ollama_url="http://ollama", now=lambda: clock[0],
                       ollama_transport=ollama_transport())
    auth = Auth(PW, now=lambda: clock[0])
    return {"j": j, "data": data, "auth": auth, "clock": clock, "tmp": tmp_path}


def client(env: dict[str, Any], asker: Asker | None = None) -> TestClient:
    return TestClient(create_app(env["data"], env["auth"], asker, sse_interval_s=0.01))


def login(c: TestClient) -> str:
    r = c.post("/login", data={"password": PW}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/").text
    m = re.search(r'name="csrf" value="([^"]+)"', page)
    assert m
    return m.group(1)


# --- UI-21 security ---------------------------------------------------------------------------

@pytest.mark.rule("UI-21")
def test_login_session_csrf_headers_and_rate_limit(env: dict[str, Any]) -> None:
    c = client(env)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/home").status_code == 401
    assert c.post("/login", data={"password": "wrong"}).status_code == 401
    r = c.post("/login", data={"password": PW}, follow_redirects=False)
    ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck
    page = c.get("/")
    assert page.headers["content-security-policy"] == CSP
    assert page.headers["x-frame-options"] == "DENY" and "<script>" not in page.text
    assert ' style="' not in page.text                       # no inline styles (CSP)
    assert c.post("/cmd", data={"name": "STATUS"}).status_code == 403   # no CSRF token
    env["clock"][0] = NOW + timedelta(minutes=31)                       # idle timeout
    assert c.get("/", follow_redirects=False).status_code == 303
    c2 = client(env)
    for _ in range(5):
        c2.post("/login", data={"password": "bad"})
    assert c2.post("/login", data={"password": PW}).status_code == 429


# --- UI-1..UI-4b, UI-22, UI-27 ------------------------------------------------------------------

@pytest.mark.rule("UI-1")
@pytest.mark.rule("UI-2")
@pytest.mark.rule("UI-3")
@pytest.mark.rule("UI-4a")
@pytest.mark.rule("UI-4b")
def test_home_cockpit(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    t = c.get("/").text
    assert 'class="mode paper"' in t and "PAPER" in t
    assert "$25,012.50" in t and "$12.50" in t and "-$375.00" in t and "of\n3" not in t
    assert "alpaca: green" in t and "slow" in t and "TRADING OK" in t
    assert 'href="/story/pid-1"' in t and "+0.50" in t                      # UI-2
    assert "ORB" in t and "AMD" in t                                         # UI-3
    assert "core" in t and "qwen3:8b" in t and "6.0 GB" in t                 # UI-4a
    assert "NOTIFIER_DOWN" in t and "ACK alerts" in t                        # UI-4b
    assert "1 pending approval" in t


@pytest.mark.rule("UI-22")
def test_live_updates(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    d = c.get("/api/home").json()
    assert d["status"]["equity"] == "25012.50" and d["pending"] == 1
    with c.stream("GET", "/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        first = next(r.iter_lines())
        assert first.startswith("data: ") and "25012.50" in first
    js = Path("src/trader/console/static/app.js").read_text()
    assert "EventSource" in js and "visibilityState" in js and "5000" in js


@pytest.mark.rule("UI-27")
def test_core_down_still_loads(env: dict[str, Any]) -> None:
    env["data"].core = CoreClient("http://core", core_transport(up=False))
    c = client(env)
    login(c)
    t = c.get("/").text
    assert "showing the last snapshot" in t and "$25,012.50" in t
    s_ = c.get("/stock?symbol=NVDA")
    assert s_.status_code == 200 and "Quote unavailable" in s_.text
    assert c.get("/scanner").status_code == 200


# --- UI-4..UI-6a stock, OP-16 --------------------------------------------------------------------

@pytest.mark.rule("UI-4")
@pytest.mark.rule("UI-5")
@pytest.mark.rule("UI-6")
@pytest.mark.rule("UI-6a")
@pytest.mark.rule("OP-16")
@pytest.mark.rule("UI-28")
def test_stock_page(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    t = c.get("/stock?symbol=nvda").text
    assert 'data-symbol="NVDA"' in t and "190.40" in t and "+1.28%" in t
    assert t.count("<svg") >= 2 and "prior high 103.00" in t and "polygon" in t   # fill marker
    assert "RSI(14, 1m)" in t and "MACD" in t and "Bollinger" in t
    assert "Checklist dry-run now (UI-5)" in t and "PASS" in t and "FAIL" in t
    assert "Suggest bull (options)" in t and "Ask</a>" in t and "Grafana" in t   # UI-6
    assert "2–3% of volume" in t                                                  # UI-6a
    assert "IV rank 55" in t and "liquid" in t and "OI 100 &lt; 500" in t         # OP-16
    assert "Universe: <span class=\"badge ok\">YES" in t
    assert "not a valid symbol" in c.get("/stock?symbol=%3Cscript%3E").text


# --- UI-7..UI-8d ask, OP-19 ----------------------------------------------------------------------

class FakeLLM:
    def __init__(self, choice: ToolChoice, answer: str) -> None:
        self.choice, self.answer = choice, answer
        self.calls = 0

    async def structured(self, **kw: Any) -> LLMResult:
        self.calls += 1
        if kw["schema"] is ToolChoice:
            return LLMResult(ok=True, value=self.choice, model="m")
        return LLMResult(ok=True, value=AnswerOut(answer=self.answer), model="m")


@pytest.mark.rule("UI-7")
@pytest.mark.rule("UI-8")
@pytest.mark.rule("UI-8c")
async def test_ask_tools_and_number_check(env: dict[str, Any]) -> None:
    data = env["data"]
    llm = FakeLLM(ToolChoice(tool="metrics", days=30, group_by="setup"),
                  "ORB has 1 trade with expectancy 1.5R.")
    ans = await Asker(data, llm).ask("How did the ORB setup do in the last 30 days?")
    assert ans.tool == "metrics" and ans.model_used and "1.5" in ans.text
    assert ans.rows == [{"setup": "ORB", "trades": 1, "win_rate_pct": 100.0,
                         "expectancy_r": 1.5}]
    bad = FakeLLM(ToolChoice(tool="metrics"), "ORB made 999 dollars.")
    ans = await Asker(data, bad).ask("How did ORB do?")
    assert not ans.model_used and "UI-C6" in ans.note and ans.text.startswith("1 row(s)")
    assert numbers_ok("Expectancy +1.50R on 40 trades", [{"e": 1.5, "n": 40}])
    assert not numbers_ok("about 12% better", [{"e": 1.5}])
    ans = await Asker(data, None).ask("What does NVDA's checklist say?")
    assert ans.tool == "checklist" and any(r_["item"] == "MF-3" for r_ in ans.rows)
    ans = await Asker(data, None).ask("show the checklist")                 # needs a symbol
    assert "needs a stock symbol" in ans.text
    ans = await Asker(data, None).ask("why not buying?")                    # today's decisions
    assert ans.tool == "decisions" and ans.rows


@pytest.mark.rule("UI-8a")
@pytest.mark.rule("UI-8b")
@pytest.mark.rule("UI-8d")
def test_ask_page_windows_suggestions_and_history(env: dict[str, Any]) -> None:
    llm = FakeLLM(ToolChoice(tool="locks"), "MSFT is locked.")
    asker = Asker(env["data"], llm)
    env["auth"].idle = timedelta(hours=6)          # the test moves the clock around
    c = client(env, asker)
    csrf = login(c)
    env["clock"][0] = et(PLAIN_DAY, "09:50")                                # entry window
    t = c.post("/ask", data={"q": "Which tickers are wash-sale locked? pause day trading",
                             "csrf": csrf}).text
    assert llm.calls == 0 and "model paused during entry windows" in t
    assert "MSFT" in t and "/controls?suggest=PAUSE%20DAY" in t              # UI-8a
    env["clock"][0] = NOW
    c.post("/ask", data={"q": "Which tickers are locked?", "csrf": csrf})
    assert llm.calls == 2
    hist = c.get("/ask").text
    assert hist.count("Which tickers") >= 2
    assert "locked?" in c.get("/ask?search=wash").text
    c.post("/ask/clear", data={"csrf": csrf})
    assert env["data"].chats() == []


@pytest.mark.rule("OP-19")
def test_options_tools_routing() -> None:
    u = ("NVDA", "AAPL")
    assert keyword_route("How did put debit spreads do on high-IV days?", u).tool == \
        "options_metrics"
    assert keyword_route("show the NVDA chain", u).tool == "chain"
    assert keyword_route("What is NVDA IV now", u).tool == "iv"
    assert keyword_route("list my option ideas this week", u).tool == "suggestions"


# --- UI-9..UI-13 ---------------------------------------------------------------------------------

@pytest.mark.rule("UI-9")
def test_trade_story(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    t = c.get("/story/pid-1").text
    for part in ("A5 proposal", "clean ORB", "A6 review", "agree", "Checklist", "CK-1",
                 "A7 decision", "entry would chase the trigger", "Orders", "Fills",
                 "Latency", "Owner notes"):
        assert part in t, part
    assert "SECRET42" not in t                                     # codes never rendered
    assert "story" in c.get("/stories").text


@pytest.mark.rule("UI-10")
@pytest.mark.rule("UI-11")
def test_approvals_inbox(env: dict[str, Any]) -> None:
    c = client(env)
    csrf = login(c)
    t = c.get("/approvals").text
    assert "NVDA ORB" in t and "data-expires" in t and "SECRET42" not in t
    assert "Code from your phone" in t and "risk $34.00" in t
    env["data"].command = _fake_command                            # type: ignore[method-assign]
    t = c.post("/cmd", data={"name": "YES", "arg": "WRONG1", "csrf": csrf}).text
    assert "Approval refused: unknown or used approval code" in t


async def _fake_command(name: str, arg: str = "", wait_s: float = 4.0) -> dict[str, Any]:
    return {"ok": False, "reason": "unknown or used approval code (SEC-2)"}


@pytest.mark.rule("UI-12")
@pytest.mark.rule("UI-13")
async def test_commands_go_through_the_outbox_and_core_authority(env: dict[str, Any]) -> None:
    from trader.models.messages import Command
    from trader.notify.commands import CommandAuthority, command_source

    data, j = env["data"], env["j"]
    auth = CommandAuthority(now=data.now, journal=j)

    async def fake_core() -> None:                 # trader-core's _commands loop
        for _ in range(50):
            for m in j.rows(s.outbox, {"channel": "inbound_cmd"}):
                if m["consumed_at"] is None:
                    p = m["payload"]
                    auth.handle(Command(name=p["name"], arg=p["arg"], sender=p["sender"],
                                        received_at=datetime.fromisoformat(p["received_at"]),
                                        source=command_source(p["source"])))
                    j.update(s.outbox, {"id": m["id"]}, {"consumed_at": data.now()})
            await asyncio.sleep(0.05)

    task = asyncio.create_task(fake_core())
    res = await data.command("FLATTEN", "", wait_s=3)
    assert res["ok"] and res["need_confirm"] and "phone" in res["reason"]
    code = next(iter(auth.confirmations))
    assert code not in res["reason"]                                   # UI-C3
    res = await data.command("CONFIRM", code, wait_s=3)
    assert res["ok"] and "confirmed FLATTEN" in res["reason"]
    assert (await data.command("SHUTDOWN"))["ok"] is False             # allow-list
    task.cancel()
    row = j.rows(s.outbox, {"channel": "inbound_cmd"})[0]
    assert row["source"] == "console" and row["payload"]["source"] == "console"
    audit = data.audit()
    assert {a["text"] for a in audit} >= {"FLATTEN", f"CONFIRM {code}"}


def test_controls_page_previews_destructive(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    t = c.get("/controls?suggest=PAUSE%20DAY").text
    assert "Suggested by Ask: PAUSE DAY" in t
    assert "FLATTEN would close NVDA 40 sh at market" in t
    assert 'data-confirm="FLATTEN: close NVDA 40 sh at market?"' in t
    assert "KILL (no confirmation" in t


# --- UI-14..UI-20 ------------------------------------------------------------------------------

@pytest.mark.rule("UI-14")
@pytest.mark.rule("UI-15")
@pytest.mark.rule("UI-16")
@pytest.mark.rule("UI-17")
def test_alerts_ideas_reports_compliance(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    t = c.get("/alerts").text
    assert "orb_up" in t and "qualified" in t and "TSLA" in t and "watchlist: TSLA" in t
    assert "Signal alerts OFF" in t
    t = c.get("/ideas").text
    assert "AMD" in t and "pick" in t
    t = c.get("/reports").text
    assert "daily_2026-10-16.md" in t
    v = c.get("/reports?name=daily_2026-10-16.md")
    assert "P&amp;L +12.50" in v.text
    assert c.get("/reports?name=daily_2026-10-16.md&download=1").content.startswith(b"# Daily")
    assert "not found" in c.get("/reports?name=../secret.txt").text            # traversal
    t = c.get("/compliance").text
    assert "MSFT" in t and "loss 120 on MSFT" in t and "Tax lots" in t


@pytest.mark.rule("UI-18")
@pytest.mark.rule("UI-19")
@pytest.mark.rule("UI-20")
def test_config_model_runs(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    assert "environment: paper" in c.get("/config").text
    t = c.get("/model").text
    assert "qwen3" in t.lower() and "./run.sh model --apply" in t
    t = c.get("/runs?a=run-a&b=run-b").text
    assert "expectancy_r" in t and "0.2" in t and "0.3" in t


# --- UI-29..UI-32, OP-17, OP-18 ------------------------------------------------------------------

@pytest.mark.rule("UI-29")
def test_owner_journal_notes(env: dict[str, Any]) -> None:
    c = client(env)
    csrf = login(c)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = c.post("/notes", data={"csrf": csrf, "ref_type": "proposal", "ref_id": "pid-1",
                               "tags": ["FOMO", "not-a-tag"], "note": "chased it"},
               files={"screenshot": ("s.png", png, "image/png")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/story/pid-1"
    note = env["j"].rows(s.owner_notes)[0]
    assert note["tags"] == ["FOMO"] and note["screenshot_b64"]
    assert "FOMO: 1" in c.get("/notes").text
    bad = c.post("/notes", data={"csrf": csrf, "ref_id": "x", "note": "n"},
                 files={"screenshot": ("s.exe", b"MZ" + b"0" * 10, "image/png")})
    assert "PNG/JPEG" in bad.text


@pytest.mark.rule("UI-30")
@pytest.mark.rule("UI-31")
@pytest.mark.rule("UI-32")
def test_scanner_latency_copilot(env: dict[str, Any]) -> None:
    c = client(env)
    csrf = login(c)
    t = c.get("/scanner").text
    assert "NVDA" in t and "+1.28" in t and "qualified 10/10" in t
    t = c.get("/latency").text
    assert "LAT-1 p95 4.2 s" in t and "ORDER" in t
    t = c.get("/copilot").text
    assert "AMD ORB" in t and "I filled" in t
    sent: list[tuple[str, str]] = []

    async def cmd(name: str, arg: str = "", wait_s: float = 4.0) -> dict[str, Any]:
        sent.append((name, arg))
        return {"ok": True, "reason": "COPILOT tracking AMD"}
    env["data"].command = cmd                                       # type: ignore[method-assign]
    t = c.post("/copilot/fill", data={"csrf": csrf, "ticker": "amd", "qty": "20",
                                      "price": "151.10"}).text
    assert sent == [("FILLED", "AMD 20 @ 151.10")] and "COPILOT tracking" in t


@pytest.mark.rule("OP-17")
@pytest.mark.rule("OP-18")
async def test_options_list_and_payoff(env: dict[str, Any]) -> None:
    from tests.unit.test_options import NOW as ONOW
    from tests.unit.test_options import bear_view, chain
    from trader.config import OptionsCfg
    from trader.options.monitor import OptionsBook
    from trader.options.strategist import format_suggestion, suggest

    sug, _ = suggest(bear_view(t1=98.0), chain(100, ONOW), cfg=OptionsCfg(), now=ONOW,
                     equity=100000, risk_pct=0.5, iv_rank=45)
    OptionsBook(env["j"]).add(sug[0], format_suggestion(sug[0]))
    c = client(env)
    login(c)
    t = c.get("/options").text
    assert "put_debit" in t and "payoff" in t and "Promotion gate" in t
    p = c.get(f"/options/{sug[0].suggestion_id}").text
    assert "<svg" in p and "invalidation 101.00" in p and "at the planned exit" in p


# --- UI-23, UI-25, UI-26, compose -----------------------------------------------------------------

@pytest.mark.rule("UI-23")
@pytest.mark.rule("UI-C1")
def test_container_budget_and_localhost_only() -> None:
    import yaml

    dc = yaml.safe_load(Path("docker/docker-compose.yml").read_text())
    con = dc["services"]["console"]
    assert con["ports"] == ["127.0.0.1:8080:8080"]
    assert con["mem_limit"] in ("256m", "200m") and "internal" in con["networks"]
    assert "console_password" in con["secrets"]
    lock = Path("requirements/console.lock").read_text()
    pkgs = re.findall(r"^([a-z0-9-]+)==", lock, re.M)
    assert set(pkgs) <= {"anyio", "click", "h11", "idna", "python-multipart", "starlette",
                         "typing-extensions", "uvicorn"}


@pytest.mark.rule("UI-25")
def test_accessibility_theme_and_shortcuts(env: dict[str, Any]) -> None:
    css = Path("src/trader/console/static/app.css").read_text()
    js = Path("src/trader/console/static/app.js").read_text()
    assert "prefers-color-scheme: dark" in css and 'data-theme="dark"' in css
    assert 'ev.key === "/"' in js and 'ev.key === "h"' in js and "localStorage" in js
    c = client(env)
    login(c)
    t = c.get("/").text
    assert re.search(r'<span class="badge ok">alpaca: green</span>', t)   # text, not colour


@pytest.mark.rule("UI-26")
def test_every_page_renders(env: dict[str, Any]) -> None:
    c = client(env)
    login(c)
    for path in ("/", "/scanner", "/stock", "/stock?symbol=NVDA", "/ask", "/approvals",
                 "/controls", "/stories", "/story/pid-1", "/copilot", "/options", "/alerts",
                 "/ideas", "/latency", "/notes", "/compliance", "/reports", "/config",
                 "/model", "/runs", "/healthz", "/static/app.css", "/static/app.js"):
        r = c.get(path)
        assert r.status_code == 200, path


# --- trader-core internal API --------------------------------------------------------------------

@pytest.mark.rule("UI-5")
@pytest.mark.rule("UI-30")
async def test_core_internal_api(tmp_path: Path) -> None:
    from tests.replay.helpers import Harness
    from trader.services.core_api import CoreApi

    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    eng = h.engine
    h.clock.advance_to(et(PLAIN_DAY, "09:50").astimezone(UTC))
    eng.md.state["NVDA"].last_price = 190.0
    eng.md.state["NVDA"].prior_close = 188.0
    api = CoreApi(eng)
    assert (await api.handle("/status", {}))[1]["environment"] == "paper"
    code, q = await api.handle("/quote", {"symbol": "NVDA"})
    assert code == 200 and q["source"] == "stream" and q["change_pct"] == pytest.approx(1.0638,
                                                                                       abs=1e-3)
    code, ck = await api.handle("/checklist", {"symbol": "NVDA"})
    assert code == 200 and ck["tradable"] and {i["id"] for i in ck["items"]} >= {
        "window", "MF-1", "MF-3", "compliance", "limits"}
    assert (await api.handle("/checklist", {"symbol": "KO"}))[1]["tradable"] is False
    assert (await api.handle("/scanner", {}))[1]["rows"][0]["ticker"]
    assert "summary" in (await api.handle("/latency", {}))[1]
    assert (await api.handle("/quote", {"symbol": "bad sym"}))[0] == 400
    assert (await api.handle("/nope", {}))[0] == 404
    assert (await api.handle("/options/iv", {"symbol": "NVDA"}))[0] == 404   # options off
    assert (await api.handle("/quote", {"symbol": "TSLA"}))[0] == 404         # no REST client

    class Rest:
        async def snapshots(self, syms: list[str]) -> dict[str, Any]:
            return {"TSLA": {"latestTrade": {"p": 250.0}, "prevDailyBar": {"c": 245.0},
                             "latestQuote": {"bp": 249.9, "ap": 250.1},
                             "dailyBar": {"v": 1000, "h": 252, "l": 244, "vw": 248}}}
    api.rest = Rest()
    code, q = await api.handle("/quote", {"symbol": "TSLA"})
    assert code == 200 and q["source"] == "snapshot" and q["spread_pct"] == pytest.approx(
        0.08, abs=1e-3)
    # HTTP round trip on an ephemeral port: GET only, JSON
    server = await asyncio.start_server(api._client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, httpx.AsyncClient() as hc:
        r = await hc.get(f"http://127.0.0.1:{port}/status")
        assert r.status_code == 200 and r.json()["equity"]
        r = await hc.post(f"http://127.0.0.1:{port}/status")
        assert r.status_code == 405
