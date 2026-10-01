"""A1 Universe (UR-1..UR-5, A1-1, A1-2)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.conftest import PLAIN_DAY, make_ctx, make_setup
from trader.agents.a01_universe import UniverseAgent
from trader.agents.a07_risk import RiskManager
from trader.config import Config
from trader.journal.store import SqlJournal


def _now() -> datetime:
    return datetime(2026, 10, 16, 14, tzinfo=UTC)


@pytest.mark.rule("UR-1")
@pytest.mark.rule("A1-1")
def test_off_list_ticker_rejected_and_logged(cfg: Config, risk: RiskManager) -> None:
    ua = UniverseAgent(cfg, None, _now)
    ok, why = ua.check("AMD")
    assert not ok and "UR-1" in why
    d = risk.evaluate(make_setup("AMD"), make_ctx(), None, None, "p-amd")
    assert d.result == "REJECT" and d.reason_code == "NT_UNIVERSE"


@pytest.mark.rule("UR-2")
def test_goog_is_never_traded(cfg: Config) -> None:
    ua = UniverseAgent(cfg, None, _now)
    ok, why = ua.check("GOOG")
    assert not ok and "UR-2" in why
    assert ua.is_allowed("GOOGL")
    assert cfg.canonical("GOOG") == "GOOGL"


@pytest.mark.rule("UR-3")
def test_tradability_and_history_check(cfg: Config) -> None:
    ua = UniverseAgent(cfg, None, _now)
    assets = {t: True for t in cfg.universe.tickers}
    assets["SPCX"] = False
    hist = {t: 500 for t in cfg.universe.tickers}
    hist["TSM"] = 30
    blocked = ua.check_tradable(assets, hist)
    assert set(blocked) == {"SPCX", "TSM"}
    assert not ua.is_allowed("SPCX") and not ua.is_allowed("TSM") and ua.is_allowed("NVDA")


@pytest.mark.rule("UR-5")
def test_list_stored_with_effective_date(cfg: Config, journal: SqlJournal) -> None:
    ua = UniverseAgent(cfg, journal, _now)
    ua.record(date(2026, 10, 1))
    assert ua.list_for(date(2026, 9, 30)) == []
    assert sorted(ua.list_for(PLAIN_DAY)) == sorted(cfg.universe.tickers)
    ua.record(date(2026, 10, 2))   # unchanged list: no duplicate rows
    assert len(ua.list_for(PLAIN_DAY)) == 10


@pytest.mark.rule("A1-2")
def test_weekly_compare_never_changes_list(cfg: Config, journal: SqlJournal) -> None:
    ua = UniverseAgent(cfg, journal, _now)
    caps = {t: 1e12 * (20 - i) for i, t in enumerate(cfg.universe.tickers)}
    caps["LLY"] = 1.5e13          # a new entrant in the top 10
    rep = ua.weekly_compare(caps, PLAIN_DAY)
    assert "LLY" in rep["top10_not_on_list"]
    assert rep["not_in_top10"] == ["TSLA"]
    assert list(cfg.universe.tickers)[-1] == "TSLA"    # list unchanged
