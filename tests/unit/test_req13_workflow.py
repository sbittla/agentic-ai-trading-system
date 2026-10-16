"""REQUIREMENTS 1.3 owner workflow: AL, AP, CP, NF-1, UN, TX-1, WS-9, DQ-6/7, PT-4, OPS."""

from __future__ import annotations

import json
import struct
import zlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from tests.conftest import PLAIN_DAY, et
from tests.replay.helpers import Harness
from trader.agents import external_trades as ext
from trader.agents import ops
from trader.agents.a12_compliance import ComplianceGuard
from trader.agents.alerts_core import grading_report
from trader.calendar import MarketCalendar
from trader.config import default_config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import Bar, Command
from trader.models.reasons import Reason
from trader.notify.chart import approval_chart
from trader.notify.telegram import TelegramNotifier


def bar(sym: str, ts: datetime, o: float, h: float, lo: float, c: float, v: int = 1000) -> Bar:
    return Bar(symbol=sym, ts=ts.astimezone(UTC), o=o, h=h, l=lo, c=c, v=v)


def copilot_cfg():   # noqa: ANN201
    c = default_config()
    return c.model_copy(update={"mode": c.mode.model_copy(update={"day_trades": "copilot"})})


# --- AL -------------------------------------------------------------------------------------

def warm_engine(tmp_path: Path) -> Harness:
    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    eng = h.engine
    eng.day = PLAIN_DAY
    eng.core_alerts.start_day(et(PLAIN_DAY, "09:30"), {"NVDA": 100.0, "QQQ": 600.0})
    st = eng.md.state["NVDA"]
    for _ in range(20):
        st.atr5.update(100.5, 99.5, 100.0)
    return h


@pytest.mark.rule("AL-2")
@pytest.mark.rule("AL-3")
async def test_alert_has_label_score_and_plan(tmp_path: Path) -> None:
    h = warm_engine(tmp_path)
    ca = h.engine.core_alerts
    now = et(PLAIN_DAY, "09:31")
    out = ca.on_bar(bar("NVDA", et(PLAIN_DAY, "09:30"), 103.0, 103.5, 102.8, 103.2), now)
    assert out, "a 3% gap should alert"
    text = out[0]
    assert text.startswith("[raw ") and "/10" in text and "failed" in text   # AL-2
    assert "Plan: long trigger 103.20" in text and "stop" in text and "T1" in text   # AL-3
    row = h.journal.rows(s.alerts_log)[0]
    assert row["label"] == "raw" and row["delivery"] == "sent" and row["plan_json"]["t1"]


@pytest.mark.rule("AL-4")
@pytest.mark.rule("AL-5")
@pytest.mark.rule("AL-6")
def test_schedule_caps_and_digest(tmp_path: Path) -> None:
    h = warm_engine(tmp_path)
    ca = h.engine.core_alerts
    assert ca.window_of(et(PLAIN_DAY, "12:00")) is None             # midday: off by default
    assert ca.window_of(et(PLAIN_DAY, "08:00")) == "pre"
    from trader.alerts.engine import Alert

    t = et(PLAIN_DAY, "09:45")
    deliveries = [ca._delivery(Alert("NVDA", f"k{i}", "x", 100.0, t), "morning", t)
                  for i in range(5)]
    assert deliveries[:3] == ["sent"] * 3 and deliveries[3:] == ["capped"] * 2   # AL-5
    assert ca._delivery(Alert("NVDA", "k", "x", 1.0, et(PLAIN_DAY, "12:00")), None,
                        et(PLAIN_DAY, "12:00")) == "off"
    # AL-6: more than top_n in one 10-minute bucket go to a digest, best first
    (tmp_path / "b").mkdir()
    ca2 = warm_engine(tmp_path / "b").engine.core_alerts
    ca2.bucket = int(et(PLAIN_DAY, "09:45").timestamp() // 600)
    ca2.digest = [(10.0, "low one"), (90.0, "high one")]
    digest = ca2.flush(et(PLAIN_DAY, "09:51"))
    assert digest is not None and digest.index("high one") < digest.index("low one")


@pytest.mark.rule("AL-7")
def test_alert_grading_and_report(tmp_path: Path) -> None:
    h = warm_engine(tmp_path)
    ca = h.engine.core_alerts
    from trader.agents.alerts_core import Logged

    t = et(PLAIN_DAY, "09:40")
    st = h.engine.md.state["NVDA"]
    st.bars1 = [bar("NVDA", t + timedelta(minutes=1), 100, 100.4, 99.9, 100.3),
                bar("NVDA", t + timedelta(minutes=2), 100.3, 101.2, 100.2, 101.1)]
    rid = h.journal.insert(s.alerts_log, {"ts": t, "ticker": "NVDA", "kind": "orb_up"})
    ca.pending.append(Logged(rid, t, "NVDA", "orb_up", "long", Decimal("99.50"),
                             Decimal("101.00")))
    assert ca.grade(t + timedelta(minutes=31)) == 1
    assert h.journal.rows(s.alerts_log)[0]["hit"] is True
    rows = [{"kind": "volume", "hit": i < 15} for i in range(50)] + [
        {"kind": "orb_up", "hit": True} for _ in range(10)]
    text, flags = grading_report(rows, 50, 0.40)
    assert "volume: 30% of 50" in text and len(flags) == 1 and flags[0].startswith("volume")


@pytest.mark.rule("AL-1")
async def test_core_produces_alerts_from_its_own_stream(tmp_path: Path) -> None:
    h = warm_engine(tmp_path)
    h.clock.advance_to(et(PLAIN_DAY, "09:31").astimezone(UTC))
    await h.engine.on_bar(bar("NVDA", et(PLAIN_DAY, "09:30"), 103.0, 103.5, 102.8, 103.2))
    await h.engine.settle()
    assert any("gapped" in m for _, m in h.notifier.sent)


# --- AP -------------------------------------------------------------------------------------

def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, hgt = struct.unpack(">II", data[16:24])
    return w, hgt


@pytest.mark.rule("AP-1")
def test_approval_chart_is_a_valid_png() -> None:
    t = et(PLAIN_DAY, "09:30")
    bars = [bar("NVDA", t + timedelta(minutes=5 * i), 100 + i * 0.1, 100.3 + i * 0.1,
                99.8 + i * 0.1, 100.2 + i * 0.1) for i in range(12)]
    png = approval_chart(bars, [100.0 + i * 0.05 for i in range(12)], entry=101.3, stop=100.6,
                         t1=102.0, or_high=100.4, or_low=99.8)
    assert png_size(png) == (480, 280) and 1000 < len(png) < 60000
    # the IDAT decompresses to height rows of (1 filter byte + width * 3)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == 280 * (1 + 480 * 3)


class Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | bytes]] = []
        self.updates: list[dict[str, object]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            method = req.url.path.rsplit("/", 1)[-1]
            ctype = req.headers.get("content-type", "")
            body: dict[str, object] | bytes = json.loads(req.content or b"{}") \
                if "json" in ctype else req.content
            self.calls.append((method, body))
            if method == "getUpdates":
                return httpx.Response(200, json={"ok": True, "result": self.updates})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        return httpx.MockTransport(handler)


@pytest.mark.rule("AP-2")
@pytest.mark.rule("AP-3")
async def test_telegram_approval_buttons_callbacks_and_edits() -> None:
    bot = Bot()
    tg = TelegramNotifier("1:T", "42", transport=bot.transport())
    mid = await tg.send_approval("APPROVE? NVDA", "K7QM", b"\x89PNGfake")
    assert mid == 77
    method, body = bot.calls[-1]
    assert method == "sendPhoto" and isinstance(body, bytes)
    assert b"YES K7QM" in body and b"NO K7QM" in body and b"chart.png" in body
    assert await tg.edit_approval(77, "45s left", photo=True, code="K7QM")
    assert bot.calls[-1][0] == "editMessageCaption"
    bot.updates = [{"update_id": 5, "callback_query": {
        "id": "cb1", "data": "YES K7QM",
        "message": {"chat": {"id": 42}, "message_id": 77}}}]
    got = await tg.poll(wait_s=0)
    assert [(snd, txt) for snd, txt, _ in got] == [("telegram:42", "YES K7QM")]
    assert any(m == "answerCallbackQuery" for m, _ in bot.calls)
    upd = next(b for m, b in bot.calls if m == "getUpdates")
    assert isinstance(upd, dict) and "callback_query" in upd["allowed_updates"]  # type: ignore[operator]


@pytest.mark.rule("AP-3")
async def test_live_status_shows_distance_and_chase(tmp_path: Path) -> None:
    from tests.unit.test_req13_core import setup_at

    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    s_ = setup_at(et(PLAIN_DAY, "09:40"))
    h.engine.md.state["NVDA"].last_price = 100.10
    txt = h.engine.approval_status(s_, et(PLAIN_DAY, "09:42"), et(PLAIN_DAY, "09:41"))
    assert "60s left" in txt and "+0.20 ATR" in txt and "CHASE" not in txt
    h.engine.md.state["NVDA"].last_price = 100.40
    assert "WOULD NOW BE A CHASE" in h.engine.approval_status(s_, et(PLAIN_DAY, "09:42"),
                                                             et(PLAIN_DAY, "09:41"))


# --- CP -------------------------------------------------------------------------------------

def cmd(name: str, arg: str, now: datetime) -> Command:
    return Command(name=name, arg=arg, sender="telegram:42", received_at=now,
                   source="telegram")


@pytest.mark.rule("CP-1")
async def test_copilot_sends_a_plan_and_places_no_order(tmp_path: Path) -> None:
    from tests.replay.helpers import orb_day

    h = Harness(copilot_cfg(), PLAIN_DAY, tmp_path)
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(PLAIN_DAY), until=et(PLAIN_DAY, "09:50"))
    assert h.broker.submissions == 0
    msgs = [m for _, m in h.notifier.sent if "COPILOT PLAN NVDA" in m]
    assert msgs and "No order placed" in msgs[0] and "stop" in msgs[0]
    assert any(d["reason_code"] == Reason.COPILOT_PLAN for d in h.journal.rows(s.decisions))


@pytest.mark.rule("CP-2")
@pytest.mark.rule("A9-5")
async def test_owner_fill_tracked_exits_as_alerts_and_counted(tmp_path: Path) -> None:
    h = Harness(copilot_cfg(), PLAIN_DAY, tmp_path)
    await h.start()
    await h.at(9, 40)
    eng = h.engine
    for _ in range(15):
        eng.md.state["NVDA"].atr5.update(190.5, 189.5, 190.0)
    now = h.clock.now()
    reply = await eng.handle_command(cmd("FILLED", "NVDA 40 @ 190.00", now))
    assert "COPILOT tracking NVDA 40 @ 190.00" in reply
    p = next(x for x in eng.pm.open_positions() if x.ticker == "NVDA")
    assert p.route == "manual_ext" and eng.risk.state.trades_total_today == 1
    # T1 then stop at break-even arrive as instructions, never as orders
    t1 = float(p.t1)
    await eng.on_bar(bar("NVDA", et(PLAIN_DAY, "09:41"), 190.0, t1 + 0.05, 189.9, t1))
    await eng.settle()
    sent = "\n".join(m for _, m in h.notifier.sent)
    assert "T1" in sent and "sell 20" in sent and "move your stop" in sent
    assert h.broker.submissions == 0
    reply = await eng.handle_command(cmd("SOLD", "NVDA 40 @ 191.00", h.clock.now()))
    assert "closed" in reply
    closed = h.journal.closed_positions()
    assert closed and closed[0]["route"] == "manual_ext" and float(closed[0]["pnl"]) == 40.0
    assert (await eng.handle_command(cmd("FILLED", "bad", h.clock.now()))).startswith("usage")


@pytest.mark.rule("CP-3")
def test_copilot_routes_are_reconciled_like_broker_positions(tmp_path: Path) -> None:
    from trader.agents.a09_positions import MANUAL_ROUTES

    assert "manual" in MANUAL_ROUTES and "manual_ext" in MANUAL_ROUTES
    src = (Path(__file__).resolve().parents[2] / "src/trader/engine.py").read_text(encoding="utf-8")
    assert 'p.route in ("broker", "manual")' in src   # Alpaca-held owner buys reconcile


# --- NF-1 -----------------------------------------------------------------------------------

@pytest.mark.rule("NF-1")
async def test_notifier_down_blocks_copilot_and_uses_fallback(tmp_path: Path) -> None:
    from tests.conftest import AGREE, make_ctx, make_setup
    from trader.models.messages import ComplianceResult

    h = Harness(copilot_cfg(), PLAIN_DAY, tmp_path)
    rm = h.engine.risk
    rm.start_day(PLAIN_DAY, Decimal("25000"))
    d = rm.evaluate(make_setup(), make_ctx(approvals_available=False), AGREE,
                    ComplianceResult(proposal_id="p1", ok=True), "p1")
    assert d.result == "REJECT" and d.reason_code == Reason.NT_NOTIFIER_DOWN

    class Fallback:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send(self, subject: str, body: str) -> None:
            self.sent.append((subject, body))

    fb = Fallback()
    h.engine.fallback = fb
    await h.engine.notifier_down("heartbeat 3 min")
    assert fb.sent and "NF-1" in fb.sent[0][1]
    assert any(r["type"] == "NOTIFIER_DOWN" for r in h.journal.rows(s.incidents))


# --- UN -------------------------------------------------------------------------------------

@pytest.mark.rule("UN-1")
def test_in_play_ranking() -> None:
    snaps = {"AMD": {"prevDailyBar": {"c": 100, "v": 1_000_000}, "latestTrade": {"p": 106},
                     "dailyBar": {"v": 900_000}},
             "KO": {"prevDailyBar": {"c": 60, "v": 1_000_000}, "latestTrade": {"p": 60.1},
                    "dailyBar": {"v": 10_000}},
             "NVDA": {"prevDailyBar": {"c": 190, "v": 1}, "latestTrade": {"p": 192},
                      "dailyBar": {"v": 1}}}
    rows = ops.rank_in_play(snaps, {"KO"}, 2, {"NVDA"})
    assert [r["ticker"] for r in rows][0] == "AMD" and rows[0]["rank"] == 1
    assert rows[0]["gap_pct"] == 6.0 and not rows[0]["on_list"] and len(rows) == 2


@pytest.mark.rule("UN-2")
def test_rotation_candidates() -> None:
    days = [date(2026, 10, d) for d in (12, 13, 14, 15, 16)]
    rows = [{"date": d, "ticker": "AMD"} for d in days[:3]] + [
        {"date": days[0], "ticker": "KO"}, {"date": days[1], "ticker": "NVDA"},
        {"date": days[2], "ticker": "NVDA"}, {"date": days[3], "ticker": "NVDA"}]
    assert ops.rotation_candidates(rows, {"NVDA"}, 3, days) == ["AMD"]


# --- TX-1 -----------------------------------------------------------------------------------

@pytest.mark.rule("TX-1")
def test_tax_set_aside() -> None:
    rows = [{"pnl": Decimal("1000"), "closed_at": datetime(2026, 3, 1, tzinfo=UTC),
             "route": "broker"},
            {"pnl": Decimal("-200"), "closed_at": datetime(2026, 5, 1, tzinfo=UTC),
             "route": "manual"},
            {"pnl": Decimal("500"), "closed_at": datetime(2025, 5, 1, tzinfo=UTC),
             "route": "broker"},
            {"pnl": Decimal("999"), "closed_at": datetime(2026, 5, 1, tzinfo=UTC),
             "route": "paper"}]
    text = ops.tax_set_aside(rows, 2026, 30)
    assert "$+800.00" in text and "~$240.00" in text and "not tax advice" in text


# --- WS-9 -----------------------------------------------------------------------------------

ROBINHOOD = """Activity Date,Process Date,Settle Date,Instrument,Description,Trans Code,\
Quantity,Price,Amount
10/01/2026,10/01/2026,10/02/2026,AMD,Advanced Micro,Buy,10,$150.00,($1500.00)
10/05/2026,10/05/2026,10/06/2026,AMD,Advanced Micro,Sell,10,$140.00,$1400.00
10/06/2026,10/06/2026,10/07/2026,,Dividend,CDIV,,,
10/08/2026,10/08/2026,10/09/2026,AMD,Advanced Micro,Buy,5,$141.00,($705.00)
"""


@pytest.mark.rule("WS-9")
def test_import_other_broker_losses_create_locks(tmp_path: Path) -> None:
    trades, skipped = ext.parse_csv(ROBINHOOD, "robinhood")
    assert len(trades) == 3 and len(skipped) == 1
    losses = ext.realized_losses(trades)
    assert len(losses) == 1 and losses[0].loss == Decimal("100.00")
    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    now = datetime(2026, 10, 10, 15, tzinfo=UTC)
    assert ext.store(j, trades, "robinhood", "rh.csv", now) == 3
    assert ext.store(j, trades, "robinhood", "rh.csv", now) == 0      # idempotent
    cfg = default_config()
    guard = ComplianceGuard(cfg, MarketCalendar(), j)
    n, warnings = ext.apply_locks(guard, ext.load(j), now, 31, cfg.canonical)
    assert n == 1 and guard.active_locks(now, "AMD")
    assert warnings and "inside a lock window" in warnings[0]          # the 10/08 re-buy
    assert ext.apply_locks(guard, ext.load(j), now, 31, cfg.canonical)[0] == 0   # no dupes
    generic = "date,symbol,side,qty,price\n2026-10-02,MSFT,buy,3,500\n"
    assert ext.parse_csv(generic)[0][0].ticker == "MSFT"
    with pytest.raises(ValueError):
        ext.parse_csv(generic, "etrade")


# --- DQ-6 / DQ-7 ----------------------------------------------------------------------------

@pytest.mark.rule("DQ-6")
def test_divergence_row() -> None:
    t0 = et(PLAIN_DAY, "09:30")
    iex = [bar("NVDA", t0 + timedelta(minutes=i), 100, 100.50, 99.90, 100.2, 100)
           for i in range(15)]
    sip = [bar("NVDA", t0 + timedelta(minutes=i), 100, 100.52, 99.90, 100.2, 4000)
           for i in range(15)]
    row = ops.divergence_row(PLAIN_DAY, "NVDA", iex, sip,
                             [(t0 + timedelta(minutes=10), 100.51)], 0.05)
    assert row["or_diff_pct"] == pytest.approx(0.0199, abs=0.001)
    assert row["iex_volume_share_pct"] == 2.5 and row["setups_within"] == 1


@pytest.mark.rule("DQ-7")
def test_data_gate() -> None:
    rows = [{"date": date(2026, 9, 1) + timedelta(days=i), "or_diff_pct": 0.01,
             "setups_checked": 2, "setups_within": 2} for i in range(20)]
    ok, detail = ops.data_gate(rows, 20, 0.05, 95)
    assert ok and "100.0%" in detail
    assert not ops.data_gate(rows[:10], 20, 0.05, 95)[0]
    bad = [{**r, "setups_within": 1} for r in rows]
    assert not ops.data_gate(bad, 20, 0.05, 95)[0]
    cfg = default_config()
    live = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"environment": "live"})})
    from trader.config import validate

    probs = validate(live, gates_passed={"live"})
    assert any("DQ-7" in p for p in probs)
    assert not any("DQ-7" in p for p in validate(live, gates_passed={"live", "data_sip"}))


# --- PT-4 -----------------------------------------------------------------------------------

@pytest.mark.rule("PT-4")
def test_strict_paper_fills() -> None:
    t = et(PLAIN_DAY, "09:40")
    bars = {"NVDA": [bar("NVDA", t - timedelta(minutes=1), 100.1, 100.2, 100.05, 100.1),
                     bar("NVDA", t, 100.1, 100.2, 100.05, 100.1),
                     bar("NVDA", t + timedelta(minutes=20), 99.0, 99.1, 98.8, 98.9)],
            "AAPL": [bar("AAPL", t, 100.1, 100.2, 99.9, 100.1),
                     bar("AAPL", t + timedelta(minutes=20), 99.3, 99.4, 99.2, 99.3)]}
    positions = [
        {"position_id": "a", "ticker": "NVDA", "qty": 10, "avg_entry": "100.05",
         "avg_exit": "101", "pnl": "9.5", "opened_at": t, "closed_at": t + timedelta(minutes=5),
         "exit_reason": "EXIT_T2"},
        {"position_id": "b", "ticker": "AAPL", "qty": 10, "avg_entry": "100.00",
         "avg_exit": "99.50", "pnl": "-5", "opened_at": t,
         "closed_at": t + timedelta(minutes=20), "exit_reason": "EXIT_STOP"}]
    res = ops.strict_paper(positions, {"a": Decimal("100.05"), "b": Decimal("100.00")}, bars)
    assert res["would_not_fill"] == 1               # NVDA never traded below its limit
    assert res["strict_pnl"] == Decimal("-7.00")    # AAPL stop filled at the 99.30 open
    assert "PT-4" in res["text"]


# --- OPS ------------------------------------------------------------------------------------

@pytest.mark.rule("OPS-1")
def test_restore_comparison() -> None:
    live = {"events": 100, "positions": 5}
    last = datetime(2026, 10, 10, 20, tzinfo=UTC)
    ok, probs = ops.compare_restore({"counts": {"events": 95, "positions": 5},
                                     "last_event": "2026-10-10T02:00:00+00:00"}, live, last)
    assert ok and not probs
    ok, probs = ops.compare_restore({"counts": {"events": 95},
                                     "last_event": "2026-10-01T02:00:00+00:00"}, live, last)
    assert not ok and any("positions" in p for p in probs) and any("behind" in p for p in probs)


@pytest.mark.rule("OPS-2")
def test_drill_record_and_overdue(tmp_path: Path) -> None:
    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    now = datetime(2026, 10, 10, 20, tzinfo=UTC)
    assert len(ops.ops_overdue(j, now)) == 2
    ops.record_check(j, "drill", True, {"kill_switch": {"ok": True}}, now - timedelta(days=3))
    ops.record_check(j, "restore_test", True, {}, now - timedelta(days=40))
    due = ops.ops_overdue(j, now)
    assert len(due) == 1 and due[0].startswith("restore test")
    assert [k for k, _ in ops.DRILL_STEPS] == ["kill_switch", "flatten", "network"]


@pytest.mark.rule("BT-6")
def test_bt6_breakdown_and_history_loader(tmp_path: Path) -> None:
    from trader.cli import _bt6_breakdown

    j = SqlJournal(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    d = PLAIN_DAY
    j.insert(s.regime_daily, {"date": d, "regime": "high-vol"})
    for pid, setup, r in (("a", "ORB", 1.5), ("b", "ORB", -1.0), ("c", "GAP_AND_GO", 0.5)):
        j.insert(s.positions, {"position_id": pid, "ticker": "NVDA", "style": "day",
                               "window": "morning", "setup": setup,
                               "opened_at": et(d, "09:40"), "closed_at": et(d, "10:00"),
                               "pnl": Decimal("1"), "r_multiple": r, "route": "broker"})
    out = _bt6_breakdown(tmp_path / "r.db")
    assert out["setup"]["ORB"] == {"trades": 2, "expectancy_r": 0.25}
    assert out["regime"]["high-vol"]["trades"] == 3 and out["window"]["morning"]["trades"] == 3


@pytest.mark.rule("WS-9")
def test_import_trades_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:
    from trader.cli import main

    db = tmp_path / "j.db"
    SqlJournal(f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    f = tmp_path / "rh.csv"
    f.write_text(ROBINHOOD, encoding="utf-8")
    cfgp = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"
    assert main(["--config", str(cfgp), "import-trades", str(f), "--broker", "robinhood"]) == 0
    out = capsys.readouterr().out
    assert "3 trades read (3 new" in out and "lock" in out
