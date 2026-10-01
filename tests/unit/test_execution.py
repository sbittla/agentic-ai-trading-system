"""Execution A8 (EX-1..EX-4, A8-1..A8-4, SEC-12, SEC-13) against SimBroker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.conftest import PLAIN_DAY, bar, et, make_setup
from trader.agents.a08_execution import ExecutionAgent, RateGuard, build_legs
from trader.brokers.sim import SimBroker
from trader.config import Config
from trader.models.messages import BracketRequest, Decision

D = Decimal


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def decision(pid: str = "11111111-2222-3333-4444-555555555555", qty: int = 32) -> Decision:
    return Decision(proposal_id=pid, result="APPROVE", reason_code="APPROVED", qty=qty,
                    entry=D("190.49"), stop=D("189.61"), t1=D("191.37"), t2=D("192.25"))


@pytest.fixture
def clock() -> Clock:
    return Clock(et(PLAIN_DAY, "09:45").astimezone(UTC))


@pytest.fixture
def broker(clock: Clock) -> SimBroker:
    return SimBroker(clock, equity=D("25000"))


@pytest.mark.rule("EX-1")
@pytest.mark.rule("EX-2")
@pytest.mark.rule("EX-4")
@pytest.mark.rule("A8-1")
async def test_two_marketable_limit_brackets(cfg: Config, broker: SimBroker,
                                             clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    code, t = await ex.execute(decision(), make_setup(), clock(), True)
    assert code == "EXEC_SUBMITTED" and t is not None
    orders = await broker.open_orders()
    assert len(orders) == 2
    assert {o.client_order_id[-2:] for o in orders} == {"-A", "-B"}
    assert all(o.type == "limit" and o.limit_price == D("190.49") for o in orders)
    sim = list(broker.orders.values())
    assert {o.tp_price for o in sim} == {D("191.37"), D("192.25")}
    assert {o.sl_price for o in sim} == {D("189.61")}
    assert sorted(o.qty for o in sim) == [16, 16]


@pytest.mark.rule("EX-4")
def test_one_share_uses_leg_b_only() -> None:
    legs = build_legs("p", "META", 1, D("700"), D("699"), D("701"), D("702"), "day")
    assert len(legs) == 1 and legs[0].client_order_id == "p-B" and legs[0].take_profit == D("702")
    legs = build_legs("p", "META", 5, D("700"), D("699"), D("701"), D("702"), "gtc")
    assert [x.qty for x in legs] == [2, 3] and legs[0].time_in_force == "gtc"


@pytest.mark.rule("A8-2")
async def test_same_proposal_never_sent_twice(cfg: Config, broker: SimBroker,
                                              clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    await ex.execute(decision(), make_setup(), clock(), True)
    code, _ = await ex.execute(decision(), make_setup(), clock(), True)
    assert code == "EXEC_DUPLICATE" and broker.submissions == 2


@pytest.mark.rule("A8-2")
async def test_broker_idempotent_on_client_order_id(broker: SimBroker) -> None:
    req = BracketRequest(client_order_id="abc-A", symbol="NVDA", qty=1, limit_price=D("10"),
                         take_profit=D("11"), stop_price=D("9"), time_in_force="day")
    o1 = await broker.submit_bracket(req)
    o2 = await broker.submit_bracket(req)
    assert o1.id == o2.id and broker.submissions == 1


@pytest.mark.rule("EX-3")
@pytest.mark.rule("A8-3")
async def test_unfilled_entry_cancelled_after_3_minutes(cfg: Config, broker: SimBroker,
                                                        clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    await ex.execute(decision(), make_setup(), clock(), True)
    assert await ex.check_cancels(clock() + timedelta(minutes=2, seconds=59), {}) == []
    out = await ex.check_cancels(clock() + timedelta(minutes=3), {})
    assert [r for _, r in out] == ["EXEC_CANCEL_TIMEOUT", "EXEC_CANCEL_TIMEOUT"]
    assert await broker.open_orders() == []


@pytest.mark.rule("EX-3")
async def test_runaway_cancel(cfg: Config, broker: SimBroker, clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    await ex.execute(decision(), make_setup(atr5=0.85), clock(), True)  # runaway uses ATR5 0.85
    # trigger 190.40 + 0.5 x 0.85 = 190.825
    assert await ex.check_cancels(clock() + timedelta(seconds=30), {"NVDA": 190.82}) == []
    out = await ex.check_cancels(clock() + timedelta(seconds=40), {"NVDA": 190.83})
    assert {r for _, r in out} == {"EXEC_CANCEL_RUNAWAY"}


@pytest.mark.rule("A8-3")
async def test_partial_fill_keeps_filled_part(cfg: Config, broker: SimBroker,
                                              clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    broker.volume_cap = 0.001   # 10 shares per bar of 10,000
    await ex.execute(decision(qty=40), make_setup(), clock(), True)
    broker.match(bar("NVDA", et(PLAIN_DAY, "09:45"), 190.6, 190.7, 190.3, 190.5, v=10000))
    for u in broker.drain_updates():
        if u.event in ("fill", "partial_fill") and u.order.leg == "entry":
            ex.on_entry_fill(u.order.id, u.fill_qty)
    await ex.check_cancels(clock() + timedelta(minutes=3), {})
    assert broker.holdings["NVDA"] == 20
    # bracket legs cover exactly the filled shares
    kids = [o for o in broker.orders.values() if o.role == "stop_loss"]
    assert sum(o.qty for o in kids) == 20


@pytest.mark.rule("A8-4")
@pytest.mark.rule("SEC-13")
async def test_reconcile_flags_mismatch_and_foreign_orders(cfg: Config, broker: SimBroker,
                                                           clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    assert await ex.reconcile({}) == []
    broker.holdings["AAPL"] = 5
    broker.cost["AAPL"] = D("1000")
    req = BracketRequest(client_order_id="web-order", symbol="TSLA", qty=1, limit_price=D("10"),
                         take_profit=D("11"), stop_price=D("9"), time_in_force="day")
    await broker.submit_bracket(req)
    problems = await ex.reconcile({})
    assert any("AAPL" in p for p in problems)
    assert any("not placed by the system" in p for p in problems)


@pytest.mark.rule("SEC-12")
def test_order_rate_guard() -> None:
    g = RateGuard()
    t = datetime(2026, 10, 16, 14, tzinfo=UTC)
    assert all(g.allow(t + timedelta(seconds=i)) for i in range(10))
    assert not g.allow(t + timedelta(seconds=11))
    assert g.allow(t + timedelta(seconds=75))
    g2 = RateGuard()
    assert all(g2.allow(t + timedelta(minutes=i * 2)) for i in range(30))
    assert not g2.allow(t + timedelta(minutes=70))


@pytest.mark.rule("SEC-12")
async def test_rate_guard_blocks_submission(cfg: Config, broker: SimBroker,
                                            clock: Clock) -> None:
    ex = ExecutionAgent(cfg, broker, None)
    for i in range(5):
        code, _ = await ex.execute(decision(pid=f"00000000-0000-0000-0000-00000000000{i}",
                                            qty=2), make_setup(), clock(), True)
        assert code == "EXEC_SUBMITTED"
    code, _ = await ex.execute(decision(pid="00000000-0000-0000-0000-000000000009", qty=2),
                               make_setup(), clock(), True)
    assert code == "EXEC_RATE_LIMIT"
