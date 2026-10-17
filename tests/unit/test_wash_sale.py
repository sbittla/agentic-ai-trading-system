"""Wash-sale prevention WS-1..WS-8 and the compliance guard A12-1..A12-4."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import PLAIN_DAY, et
from trader.agents.a12_compliance import ComplianceGuard
from trader.calendar import MarketCalendar
from trader.config import Config
from trader.journal.store import SqlJournal
from trader.models.messages import EventFlags

D = Decimal


def policy(cfg: Config, name: str) -> Config:
    return cfg.model_copy(update={"wash_sale": cfg.wash_sale.model_copy(update={"policy": name})})


def round_trip(g: ComplianceGuard, pid: str, ticker: str, buy: str, sell: str, day: date,
               qty: int = 10, r: float = -1.0) -> Decimal:
    g.on_buy_fill(pid, ticker, qty, D(buy), et(day, "09:45"))
    pnl = g.on_sell_fill(pid, ticker, qty, D(sell), et(day, "10:10"), r)
    g.on_position_closed(ticker, pnl, r, et(day, "10:10"))
    return pnl


@pytest.mark.rule("WS-1")
@pytest.mark.rule("A12-1")
def test_loss_locks_ticker_for_31_days(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(cfg, cal)
    round_trip(g, "p1", "AAPL", "100", "99", PLAIN_DAY)
    for days in (1, 15, 30):
        res = g.check_entry("AAPL", et(PLAIN_DAY + timedelta(days=days), "09:40"), "day")
        assert not res.ok and res.reason == "NT_WASH_LOCK"
    assert g.check_entry("AAPL", et(PLAIN_DAY + timedelta(days=31), "10:11"), "day").ok


@pytest.mark.rule("WS-1")
def test_profit_creates_no_lock(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(cfg, cal)
    round_trip(g, "p1", "AAPL", "100", "101", PLAIN_DAY, r=1.4)
    assert g.check_entry("AAPL", et(PLAIN_DAY, "15:10"), "day").ok


@pytest.mark.rule("WS-2")
def test_look_back_blocks_entry(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(cfg, cal)
    g.on_buy_fill("p1", "MSFT", 5, D("500"), et(PLAIN_DAY, "09:45"))
    g.on_sell_fill("p1", "MSFT", 5, D("495"), et(PLAIN_DAY, "10:00"), -1.0)
    g.locks.clear()   # even without a lock row, the look-back finds the loss
    res = g.check_entry("MSFT", et(PLAIN_DAY + timedelta(days=10), "09:40"), "day")
    assert not res.ok and any(x.startswith("WASH_LOOKBACK") for x in res.locks_found)


@pytest.mark.rule("WS-3")
@pytest.mark.rule("A12-2")
def test_look_forward_flags_other_open_lots(cfg: Config, cal: MarketCalendar,
                                            journal: SqlJournal) -> None:
    g = ComplianceGuard(policy(cfg, "year_end_only"), cal, journal)
    g.on_buy_fill("swing1", "NVDA", 10, D("180"), et(PLAIN_DAY, "15:45"))
    g.on_buy_fill("day1", "NVDA", 10, D("185"), et(PLAIN_DAY + timedelta(days=3), "09:45"))
    flagged, detail = g.check_loss_exit("day1", "NVDA", et(PLAIN_DAY + timedelta(days=3),
                                                            "10:00"))
    assert flagged and "WASH EXPECTED" in detail and "swing1" in detail
    alone, _ = g.check_loss_exit("swing1", "NVDA", et(PLAIN_DAY + timedelta(days=3), "10:00"))
    assert alone   # day1 lot was bought within 30 days too
    # selling day1 at a loss with swing1 still held is a wash: basis moves to swing1
    g.on_sell_fill("day1", "NVDA", 10, D("183"), et(PLAIN_DAY + timedelta(days=3), "10:05"))
    lot = next(x for x in g.lots if x.position_id == "swing1")
    assert lot.disallowed_added == D("20.00") and lot.adjusted_basis == D("1820.00")


@pytest.mark.rule("WS-4")
def test_goog_and_googl_are_one_ticker(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(cfg, cal)
    round_trip(g, "p1", "GOOGL", "240", "238", PLAIN_DAY)
    assert g.group("GOOG") == "GOOGL"
    assert g.active_locks(et(PLAIN_DAY, "15:00"), "GOOG")
    assert not g.check_entry("GOOG", et(PLAIN_DAY, "15:10"), "day").ok


@pytest.mark.rule("WS-5")
@pytest.mark.rule("WS-8")
def test_year_end_rule_applies_in_every_policy(cfg: Config, cal: MarketCalendar) -> None:
    for name in ("strict", "balanced", "year_end_only"):
        g = ComplianceGuard(policy(cfg, name), cal)
        d = date(2026, 12, 2)
        g.on_buy_fill("p1", "AMZN", 10, D("230"), et(d, "09:45"))
        g.on_sell_fill("p1", "AMZN", 10, D("229.9"), et(d, "10:00"), -0.1)   # tiny loss
        g.on_position_closed("AMZN", D("-1"), -0.1, et(d, "10:00"))
        assert not g.check_entry("AMZN", et(d + timedelta(days=5), "09:40"), "day").ok, name


@pytest.mark.rule("WS-5")
def test_no_deferred_loss_into_new_year(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(policy(cfg, "balanced"), cal)
    d = date(2026, 11, 20)
    g.on_buy_fill("p1", "TSLA", 10, D("440"), et(d, "09:45"))
    g.on_sell_fill("p1", "TSLA", 10, D("439.5"), et(d, "10:00"), -0.2)   # small loss: no lock
    g.on_buy_fill("p2", "TSLA", 10, D("441"), et(d + timedelta(days=5), "09:45"))   # washes
    assert g.year_end_must_close(et(date(2026, 11, 30), "15:45")) == []
    assert g.year_end_must_close(et(date(2026, 12, 31), "15:45")) == ["TSLA"]


@pytest.mark.rule("WS-6")
@pytest.mark.rule("A12-4")
def test_locked_list_and_ira_warning(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(cfg, cal)
    round_trip(g, "p1", "META", "760", "755", PLAIN_DAY)
    g.news["TSLA"] = EventFlags(ticker="TSLA", updated_at=et(PLAIN_DAY, "07:15"),
                                flags=("EARNINGS_TODAY",))
    locked = g.locked_list(et(PLAIN_DAY, "15:00"))
    assert [x["ticker"] for x in locked] == ["META", "TSLA"]
    assert "IRA" in g.ira_warning(et(PLAIN_DAY, "15:00"))


@pytest.mark.rule("WS-7")
@pytest.mark.rule("A12-3")
def test_lots_reports_and_export(cfg: Config, cal: MarketCalendar,
                                 journal: SqlJournal) -> None:
    g = ComplianceGuard(policy(cfg, "balanced"), cal, journal)
    round_trip(g, "p1", "AAPL", "250", "249.9", date(2026, 10, 5), r=-0.1)
    g.on_buy_fill("p2", "AAPL", 10, D("251"), et(date(2026, 10, 9), "09:45"))   # washes p1
    g.on_sell_fill("p2", "AAPL", 10, D("253"), et(date(2026, 10, 9), "10:00"), 1.0)
    rep = g.monthly_report(2026, 10)
    assert rep["wash_sales"] == 1 and rep["disallowed_total"] == "1.00"
    csv = g.year_end_export(2026)
    assert "wash_sale_loss_disallowed" in csv and "1.00" in csv
    assert g.reconcile_lots({}) == []
    g.on_buy_fill("p3", "AAPL", 3, D("250"), et(date(2026, 10, 12), "09:45"))
    assert g.reconcile_lots({"AAPL": 2}) == ["AAPL: lots 3 vs broker 2"]
    rows = journal.rows(__import__("trader.journal.schema", fromlist=["tax_lots"]).tax_lots)
    assert len(rows) == 3


@pytest.mark.rule("WS-7")
def test_balanced_policy_small_loss_washes_into_basis(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(policy(cfg, "balanced"), cal)
    round_trip(g, "p1", "AVGO", "340", "339", PLAIN_DAY, r=-0.3)
    assert g.check_entry("AVGO", et(PLAIN_DAY + timedelta(days=2), "09:40"), "day").ok
    lot = g.on_buy_fill("p2", "AVGO", 10, D("341"), et(PLAIN_DAY + timedelta(days=2), "09:45"))
    assert lot.adjusted_basis == D("3420.00")   # 3410 + 10 deferred


@pytest.mark.rule("WS-1")
@pytest.mark.rule("A12-1")
def test_strict_policy_one_year_zero_wash_sales(cfg: Config, cal: MarketCalendar) -> None:
    """A12 done-when: a year of simulated trades under the strict policy -> zero wash sales."""
    g = ComplianceGuard(cfg, cal)
    rng = random.Random(3)
    d = date(2026, 1, 5)
    n = 0
    while d.year == 2026:
        if cal.is_trading_day(d):
            for _ in range(3):
                t = rng.choice(list(cfg.universe.tickers))
                if not g.check_entry(t, et(d, "09:40"), "day").ok:
                    continue
                pid = f"p{n}"
                n += 1
                px = D(str(round(rng.uniform(100, 500), 2)))
                g.on_buy_fill(pid, t, 10, px, et(d, "09:45"))
                exit_px = px + D(str(round(rng.uniform(-3, 3), 2)))
                pnl = g.on_sell_fill(pid, t, 10, exit_px, et(d, "10:20"),
                                     -1.0 if exit_px < px else 1.0)
                g.on_position_closed(t, pnl, -1.0 if pnl < 0 else 1.0, et(d, "10:20"))
        d += timedelta(days=1)
    assert n > 100 and g.wash_sales == 0


@pytest.mark.rule("A12-1")
def test_earnings_locks(cfg: Config, cal: MarketCalendar) -> None:
    g = ComplianceGuard(cfg, cal)
    g.news["NVDA"] = EventFlags(ticker="NVDA", updated_at=et(PLAIN_DAY, "07:15"),
                                flags=("EARNINGS_3D",))
    assert g.check_entry("NVDA", et(PLAIN_DAY, "09:40"), "day").ok
    res = g.check_entry("NVDA", et(PLAIN_DAY, "15:35"), "swing")
    assert not res.ok and res.reason == "NT_EARNINGS_LOCK"


@pytest.mark.rule("A12-4")
def test_locks_survive_restart(cfg: Config, cal: MarketCalendar, journal: SqlJournal) -> None:
    g = ComplianceGuard(cfg, cal, journal)
    round_trip(g, "p1", "AAPL", "100", "99", PLAIN_DAY)
    g2 = ComplianceGuard(cfg, cal, journal)
    g2.load_from_journal(et(PLAIN_DAY, "16:00"))
    assert not g2.check_entry("AAPL", et(PLAIN_DAY + timedelta(days=3), "09:40"), "day").ok


@pytest.mark.rule("WS-7")
def test_tax_export_command_writes_the_years_lots(tmp_path, monkeypatch, capsys,  # type: ignore[no-untyped-def]
                                                 cfg: Config, cal: MarketCalendar) -> None:
    from trader.cli import main

    db = tmp_path / "j.db"
    url = f"sqlite:///{db.as_posix()}"
    g = ComplianceGuard(cfg, cal, SqlJournal(url))
    round_trip(g, "p1", "AAPL", "250", "251", date(2026, 10, 5), r=0.5)
    monkeypatch.setenv("DATABASE_URL", url)
    cfgp = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"
    out = tmp_path / "2026.csv"
    assert main(["--config", str(cfgp), "tax-export", "2026", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("description,date_acquired")
    assert "AAPL" in text and "wrote" in capsys.readouterr().out
    assert main(["--config", str(cfgp), "tax-export", "2025"]) == 0
    assert "AAPL" not in capsys.readouterr().out          # nothing sold in 2025
