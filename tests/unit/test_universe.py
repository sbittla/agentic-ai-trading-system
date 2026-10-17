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
    assert ua.check("GOOGL") == (True, "")
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
    assert not ua.check("SPCX")[0] and not ua.check("TSM")[0] and ua.check("NVDA")[0]
    # the engine's daily check passes no asset list: history only (tradability is the
    # live pre-flight's job)
    assert set(ua.check_tradable(None, hist)) == {"TSM"}


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


@pytest.mark.rule("A1-2")
def test_weekly_compare_cross_checks_shares_with_edgar(cfg: Config) -> None:
    import asyncio

    import httpx

    from trader.news.sources import EdgarFilings

    ua = UniverseAgent(cfg, None, _now)
    caps = {t: 1e12 * (20 - i) for i, t in enumerate(cfg.universe.tickers)}
    vendor = {"NVDA": 24.3e9, "AAPL": 15.0e9, "TSM": 5.2e9}
    sec = {"NVDA": 24.4e9, "AAPL": 13.0e9}           # AAPL disagrees by 15%; TSM files 20-F
    rep = ua.weekly_compare(caps, PLAIN_DAY, vendor, sec)
    assert rep["share_mismatch"] == ["AAPL"] and set(rep["share_check"]) == {"NVDA", "AAPL"}
    assert ua.weekly_compare(caps, PLAIN_DAY)["share_check"] == {}   # no counts: no check

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"ticker": "NVDA", "cik_str": 1045810}})
        assert req.url.path.endswith("CIK0001045810.json")
        return httpx.Response(200, json={"facts": {"dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"val": 24.0e9}, {"val": 24.4e9}]}}}}})

    edgar = EdgarFilings("test test@example.com", transport=httpx.MockTransport(handler))
    assert asyncio.run(edgar.shares_outstanding("NVDA")) == 24.4e9
    assert asyncio.run(edgar.shares_outstanding("ZZZZ")) is None
