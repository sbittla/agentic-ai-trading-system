"""Full-engine replays: one trade end to end through all agents (ARCHITECTURE §3, §4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tests.replay.helpers import Harness, et, orb_day
from trader.config import Config, default_config
from trader.journal import schema as s
from trader.llm.fakes import ScriptedLLM

DAY = date(2026, 10, 16)


def approve_cfg() -> Config:
    c = default_config()
    return c.model_copy(update={"mode": c.mode.model_copy(update={"day_trades": "approve"})})


@pytest.mark.rule("SR-4")
@pytest.mark.rule("A8-1")
async def test_orb_trade_full_lifecycle(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(DAY))
    await h.at(16, 0)
    j = h.journal
    pos = j.closed_positions()
    assert len(pos) == 1, [d["reason_code"] for d in j.rows(s.decisions)]
    p = pos[0]
    assert p["ticker"] == "NVDA" and p["setup"] == "ORB" and p["window"] == "morning"
    assert p["exit_reason"] == "EXIT_T2" and float(p["r_multiple"]) == pytest.approx(1.5, abs=0.05)
    # the journal holds every step (SR-4)
    types = {r["type"] for r in j.rows(s.events)}
    assert {"SetupTriggered", "TradeProposal", "Review", "ComplianceResult", "Decision",
            "PositionOpened", "PositionClosed", "GamePlan"} <= types
    dec = [d for d in j.rows(s.decisions) if d["result"] == "APPROVE"]
    assert len(dec) == 1 and len(dec[0]["checklist_json"]) == 10
    orders = j.rows(s.orders)
    assert {o["leg"] for o in orders} == {"A", "B"}
    assert all(o["proposal_id"] == dec[0]["proposal_id"] for o in orders)
    assert j.rows(s.reviews) and j.rows(s.tax_lots)
    # stop was moved to break-even after T1 and never below the initial stop
    moves = [r["payload"] for r in j.rows(s.events) if r["type"] == "StopMoved"]
    assert moves and all(Decimal(m["new_stop"]) >= Decimal(m["old_stop"]) for m in moves)
    msgs = "\n".join(h.notifier.sent[i][1] for i in range(len(h.notifier.sent)))
    assert "[PAPER]" in msgs and "FILLED NVDA" in msgs and "CLOSED NVDA" in msgs


@pytest.mark.rule("TW-2")
@pytest.mark.rule("A9-2")
async def test_morning_trade_flattened_at_1030(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    # rally a little (no T1), hold above VWAP and the breakout level until 10:30
    after = [190.7, 190.8, 190.75, 190.85, 190.8] * 12 + [190.8] * 330
    await h.run_bars(orb_day(DAY, nvda_after=after))
    p = h.journal.closed_positions()
    assert len(p) == 1 and p[0]["exit_reason"] in ("EXIT_TIME_STOP_1030", "EXIT_TIME_STOP_15MIN")
    assert p[0]["closed_at"] <= et(DAY, 10, 32)


@pytest.mark.rule("XR-1")
async def test_stop_out_counts_as_loss_and_locks(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    after = [190.6, 190.4, 190.0, 189.5, 189.0] + [189.0] * 376
    await h.run_bars(orb_day(DAY, nvda_after=after))
    p = h.journal.closed_positions()
    assert len(p) == 1 and float(p[0]["pnl"]) < 0
    assert p[0]["exit_reason"] in ("EXIT_STOP", "EXIT_FAILED_TRADE")
    assert any(lk.ticker == "NVDA" and lk.lock_type == "WASH" for lk in h.engine.compliance.locks)


@pytest.mark.rule("A10-4")
@pytest.mark.rule("SEC-2")
async def test_approve_mode_yes(tmp_path: Path) -> None:
    h = Harness(approve_cfg(), DAY, tmp_path, approvals="yes")
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(DAY))
    assert len(h.notifier.requests) == 1
    assert "code" in next(t for k, t in h.notifier.sent if k == "approval")
    assert len(h.journal.closed_positions()) == 1
    ap = h.journal.rows(s.approvals)
    assert ap[0]["answer"] == "yes" and ap[0]["code"]


@pytest.mark.rule("A10-4")
@pytest.mark.parametrize("answer", ["no", "timeout"])
async def test_approve_mode_no_or_timeout_means_no_trade(tmp_path: Path, answer: str) -> None:
    h = Harness(approve_cfg(), DAY, tmp_path, approvals=answer)
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(DAY))
    assert h.journal.closed_positions() == [] and h.broker.submissions == 0
    codes = {d["reason_code"] for d in h.journal.rows(s.decisions)}
    assert ("NT_APPROVAL_REJECTED" if answer == "no" else "NT_APPROVAL_TIMEOUT") in codes


@pytest.mark.rule("A10-3")
async def test_kill_switch_cancels_entries_keeps_stops(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    bars = orb_day(DAY)
    await h.run_bars(bars, until=et(DAY, 9, 41))
    open_entries = [o for o in h.broker.orders.values() if o.role == "entry" and o.active]
    assert open_entries   # submitted at 9:40, not yet filled
    n = await h.engine.kill("test", by="local")
    assert n == len(open_entries)
    assert not [o for o in h.broker.orders.values() if o.role == "entry" and o.active]
    await h.run_bars(h.remaining(bars, et(DAY, 9, 41)))
    assert h.broker.submissions == len(open_entries)   # nothing new after the kill
    assert any(r["type"] == "KILL_SWITCH" for r in h.journal.rows(s.incidents))


@pytest.mark.rule("A10-3")
async def test_kill_switch_keeps_broker_stops_on_open_position(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    bars = orb_day(DAY)
    await h.run_bars(bars, until=et(DAY, 9, 43))
    assert h.engine.pm.open_positions()
    await h.engine.kill("test")
    stops = [o for o in h.broker.orders.values() if o.role == "stop_loss" and o.active]
    assert stops and sum(o.open_qty for o in stops) == h.broker.holdings["NVDA"]


@pytest.mark.rule("A10-2")
async def test_llm_down_blocks_entries(tmp_path: Path) -> None:
    llm = ScriptedLLM()
    llm.up = False
    h = Harness(default_config(), DAY, tmp_path, llm=llm)
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(DAY))
    assert h.broker.submissions == 0
    codes = {d["reason_code"] for d in h.journal.rows(s.decisions)}
    assert "NT_LLM_INVALID" in codes


@pytest.mark.rule("A10-2")
async def test_red_health_blocks_entries(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    h.engine.sup.set_health("ollama", "red", "container stopped", h.clock.now())
    await h.run_bars(orb_day(DAY))
    assert h.broker.submissions == 0
    assert "NT_HEALTH" in {d["reason_code"] for d in h.journal.rows(s.decisions)}


@pytest.mark.rule("SEC-14")
async def test_whatsapp_commands_two_step(tmp_path: Path) -> None:
    from trader.models.messages import Command

    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    now = h.clock.now()
    reply = await h.engine.handle_command(Command(name="FLATTEN", sender="whatsapp:+1",
                                                  received_at=now, source="whatsapp"))
    assert "within 60s" in reply
    code = reply.split()[1]
    await h.engine.handle_command(Command(name="CONFIRM", arg=code, sender="whatsapp:+1",
                                          received_at=now, source="whatsapp"))
    rows = h.journal.rows(s.commands)
    assert len(rows) == 2 and all(r["accepted"] for r in rows)
    await h.engine.handle_command(Command(name="KILL", sender="whatsapp:+1", received_at=now,
                                          source="whatsapp"))
    assert h.engine.risk.state.kill_switch
