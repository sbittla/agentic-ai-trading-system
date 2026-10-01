"""The full engine with its journal on a real Postgres (live/paper configuration, ADR-012)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from tests.replay.helpers import Harness, orb_day
from trader.config import default_config
from trader.journal import schema as s
from trader.journal.store import SqlJournal

URL = os.environ.get("TEST_PG_CORE_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="needs Postgres")]
DAY = date(2026, 10, 16)


@pytest.mark.rule("SR-4")
async def test_orb_day_journaled_in_postgres(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    j = SqlJournal(URL or "", create=False)
    with j.engine.begin() as c:   # clean slate for the engine's own tables
        for t in reversed(s.metadata.sorted_tables):
            if t.name != "outbox":
                c.execute(t.delete())
    h.journal = j
    eng = h.engine
    for agent in (eng, eng.universe, eng.strategy, eng.reviewer, eng.compliance, eng.exec,
                  eng.reporter, eng.bus, eng.auth):
        agent.journal = j   # type: ignore[attr-defined]
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(DAY))
    await h.at(16, 30)
    pos = j.closed_positions()
    assert len(pos) == 1 and pos[0]["exit_reason"] == "EXIT_T2"
    assert j.rows(s.tax_lots) and j.rows(s.decisions) and j.rows(s.daily_metrics)
    assert any(r["type"] == "PositionClosed" for r in j.rows(s.events))
