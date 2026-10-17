"""trader-core's momentum jobs (MOMENTUM_REQUIREMENTS MO-15, MO-20)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.replay.helpers import Harness, et
from trader.config import default_config
from trader.momentum.agent import MomentumAgent
from trader.momentum.setups import SETUPS
from trader.scanner.techniques import Series

DAY = date(2026, 10, 16)


def series(sym: str, end: date, n: int, g: float, price: float = 40.0) -> Series:
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rows = []
    c = price
    for k, day in enumerate(days):
        o = c
        c = price * (1 + g) ** k * (1 + 0.004 * ((-1) ** k))
        rows.append((day, o, max(o, c) * 1.004, min(o, c) * 0.996, c, 3e6))
    return Series.from_rows(sym, rows)


def loader(store: dict[str, Series]) -> Any:
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        out = {}
        for s in symbols:
            if s in store:
                v = store[s]
                n = sum(1 for d in v.dates if d <= until)
                if n:
                    out[s] = v.upto(n)
        return out
    return load


def harness(tmp_path: Path, end: date) -> Harness:
    cfg = default_config()
    syms = tuple(f"S{k:02d}" for k in range(11)) + ("LEAD",)
    mo = cfg.momentum.model_copy(update={
        "universe": syms, "eval_file": str(tmp_path / "eval.json"),
        "latest_file": str(tmp_path / "momentum.json")})
    cfg = cfg.model_copy(update={"momentum": mo})
    setups = {st.name: {"default_policy": st.policy, "gate": {"pass": st.name == "DONCHIAN_55",
                                                              "checks": {}},
                        "policies": {st.policy: {"all": {"trades": 99, "win_rate": 0.5,
                                                         "avg_r": 0.15, "profit_factor": 1.4}}}}
              for st in SETUPS}
    Path(mo.eval_file).write_text(json.dumps({"generated": "x", "setups": setups,
                                              "gate_pass": ["DONCHIAN_55"]}), encoding="utf-8")
    h = Harness(cfg, DAY, tmp_path)
    store = {f"S{k:02d}": series(f"S{k:02d}", end, 330, 0.0002 * k) for k in range(11)}
    store["LEAD"] = series("LEAD", end, 330, 0.003)
    store["SPY"] = series("SPY", end, 330, 0.0005, 400.0)
    h.engine._momentum = MomentumAgent(cfg, h.engine.journal, loader(store), h.engine.now,
                                       cal=h.engine.cal)
    from trader.journal import schema as js

    h.engine.journal.insert(js.fundamentals_daily, {          # a fresh calendar (MO-18)
        "date": end, "ticker": "ZZZ", "kind": "earnings_calendar", "json": {"date": "2099-01-02"}})
    return h


@pytest.mark.rule("MO-15")
@pytest.mark.rule("MO-20")
async def test_close_job_arms_and_sends_buy_stops(tmp_path: Path) -> None:
    h = harness(tmp_path, DAY)
    h.clock.set(et(DAY, 16, 50))
    assert await h.engine.job_momentum(et(DAY, 16, 50)) is True
    sent = [text for kind, text in h.notifier.sent if kind == "momentum"]
    assert len(sent) == 1 and "BUY-STOPS" in sent[0] and "LEAD DONCHIAN_55" in sent[0]
    assert Path(h.cfg.momentum.latest_file).exists()


@pytest.mark.rule("MO-15")
async def test_close_job_waits_for_the_final_bar(tmp_path: Path) -> None:
    h = harness(tmp_path, DAY - timedelta(days=1))
    h.clock.set(et(DAY, 16, 50))
    assert await h.engine.job_momentum(et(DAY, 16, 50)) is False          # retried later
    assert not [t for k, t in h.notifier.sent if k == "momentum"]


@pytest.mark.rule("MO-15")
async def test_intraday_job_stops_after_the_session(tmp_path: Path) -> None:
    h = harness(tmp_path, DAY)
    assert await h.engine.job_momentum_intraday(et(DAY, 15, 51)) is True


@pytest.mark.rule("MO-20")
def test_jobs_are_scheduled_live(tmp_path: Path) -> None:
    h = harness(tmp_path, DAY)
    h.engine.sim_mode = False
    h.engine.scheduler.jobs = []
    h.engine._setup_jobs()
    names = {j.name for j in h.engine.scheduler.jobs}
    assert {"momentum_close", "momentum_intraday", "momentum_eval"} <= names
    intraday = next(j for j in h.engine.scheduler.jobs if j.name == "momentum_intraday")
    assert intraday.retry_min == h.cfg.momentum.intraday_every_min
