"""Live-only behaviours: slow Qwen never delays the clock tick; Alpaca-style stop replaces."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.replay.helpers import Harness, et, orb_day
from trader.config import default_config
from trader.interfaces import LLMResult
from trader.llm.fakes import RulesLLM
from trader.models.messages import Order

DAY = date(2026, 10, 16)


class SlowLLM(RulesLLM):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def structured(self, **kw: Any) -> LLMResult:   # type: ignore[override]
        await self.gate.wait()
        return await super().structured(**kw)


async def test_slow_llm_does_not_block_tick(tmp_path: Path) -> None:
    llm = SlowLLM()
    h = Harness(default_config(), DAY, tmp_path, llm=llm)
    h.engine.sim_mode = False   # live behaviour
    await h.start()
    llm.gate.set()             # let the game plan through
    await h.premarket()
    await h.engine.settle()
    llm.gate.clear()           # now Qwen "hangs" on every call
    bars = orb_day(DAY)
    for b in [x for x in bars if x.ts < et(DAY, 9, 40).astimezone(x.ts.tzinfo)]:
        h.clock.advance_to(b.end)   # bars are received at their end time
        await h.engine.on_bar(b)
    await asyncio.wait_for(h.engine.tick(h.clock.now()), 2)   # returns while Qwen is "thinking"
    assert h.engine._tasks   # evaluation runs in the background
    llm.gate.set()
    await h.engine.settle()
    from trader.journal import schema as s
    reasons = [(d["reason_code"], d["detail"]) for d in h.journal.rows(s.decisions)]
    assert h.broker.submissions == 2, reasons


class ReplacingBroker:
    """Wraps SimBroker; replace_stop returns a new order id like Alpaca."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.replaced: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def replace_stop(self, order_id: str, new_stop: Decimal) -> Order:
        o = await self.inner.replace_stop(order_id, new_stop)
        self.replaced.append(order_id)
        sim = self.inner.orders.pop(order_id)
        sim.id = f"{order_id}-r{len(self.replaced)}"
        self.inner.orders[sim.id] = sim
        sib = self.inner.orders.get(sim.sibling_id or "")
        if sib is not None:
            sib.sibling_id = sim.id
        return o.model_copy(update={"id": sim.id})


async def test_stop_replace_tracks_new_order_id(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    rb = ReplacingBroker(h.broker)
    h.engine.exec.broker = rb   # type: ignore[assignment]
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(DAY))
    assert rb.replaced, "expected a break-even / trail move"
    p = h.journal.closed_positions()
    assert len(p) == 1 and p[0]["exit_reason"] == "EXIT_T2"
