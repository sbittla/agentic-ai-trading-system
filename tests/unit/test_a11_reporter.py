"""A11 Reporter (A11-1..A11-3, SEC-15, WS-6)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import PLAIN_DAY, et
from trader.agents.a11_reporter import Reporter, allowed_numbers, numbers_ok
from trader.agents.a12_compliance import ComplianceGuard
from trader.calendar import MarketCalendar
from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.fakes import RulesLLM, ScriptedLLM


def seed(journal: SqlJournal) -> None:
    rows = [("p1", "NVDA", "ORB", "morning", "09:46", "10:05", "36.80", 1.3, "EXIT_TRAIL", True),
            ("p2", "AMZN", "VWAP_RECLAIM", "morning", "09:50", "10:02", "-28.00", -1.0,
             "EXIT_STOP", False)]
    for pid, t, setup, w, o, c, pnl, r, why, planned in rows:
        journal.insert(s.positions, {"position_id": pid, "ticker": t, "style": "day",
                                     "window": w, "setup": setup, "planned": planned,
                                     "opened_at": et(PLAIN_DAY, o), "closed_at": et(PLAIN_DAY, c),
                                     "qty": 32, "avg_entry": Decimal("190.47"),
                                     "avg_exit": Decimal("191.6"), "pnl": Decimal(pnl),
                                     "r_multiple": r, "exit_reason": why, "route": "broker",
                                     "state_json": {"atr5": "0.85", "trigger": "190.40"}})
    journal.insert(s.decisions, {"proposal_id": "x", "ts": et(PLAIN_DAY, "09:55"),
                                 "result": "REJECT", "reason_code": "NT_FILTER_MF1"})
    journal.insert(s.commands, {"ts": et(PLAIN_DAY, "09:47"), "sender": "whatsapp:+1",
                                "text": "YES K7Q2", "accepted": True, "reason": "ok",
                                "source": "whatsapp"})
    journal.insert(s.commands, {"ts": et(PLAIN_DAY, "09:48"), "sender": "whatsapp:+999",
                                "text": "KILL", "accepted": False, "reason": "CMD_REJECTED",
                                "source": "whatsapp"})


@pytest.fixture
def rep(cfg: Config, cal: MarketCalendar, journal: SqlJournal, tmp_path: Path) -> Reporter:
    seed(journal)
    g = ComplianceGuard(cfg, cal, journal)
    g.add_lock("AMZN", "WASH", et(PLAIN_DAY, "10:02"), et(PLAIN_DAY, "10:02") + timedelta(
        days=31), "loss")
    return Reporter(cfg, journal, RulesLLM(), g, tmp_path / "reports",
                    lambda: et(PLAIN_DAY, "16:30"))


@pytest.mark.rule("A11-1")
@pytest.mark.rule("A11-3")
@pytest.mark.rule("SEC-15")
@pytest.mark.rule("WS-6")
async def test_daily_review_figures_from_journal(rep: Reporter, journal: SqlJournal) -> None:
    data = await rep.daily(PLAIN_DAY)
    m = data["metrics"]
    assert m["trades"] == 2 and m["pnl"] == pytest.approx(8.80) and m["total_r"] == 0.3
    grades = {t["ticker"]: t["grade"] for t in data["trades"]}
    assert grades == {"NVDA": "A", "AMZN": "C"}            # unplanned -> C (code, not model)
    assert data["no_trade_log"] == {"NT_FILTER_MF1": 1}
    assert data["rejected_senders"] == ["whatsapp:+999"]
    text = data["text"]
    assert text.startswith("[PAPER] Daily review") and "IRA" in text and "AMZN" in text
    assert "+$8.80" in text
    assert journal.rows(s.daily_metrics)[0]["trades"] == 2
    assert (rep.out_dir / f"{PLAIN_DAY}_daily.html").exists()


@pytest.mark.rule("A11-3")
def test_narrative_numbers_must_come_from_metrics() -> None:
    allowed = allowed_numbers({"pnl": 8.8, "trades": 2, "x": "chase 0.2 ATR"})
    assert numbers_ok("2 trades, P&L $8.80, chase 0.2", allowed)
    assert not numbers_ok("We made $500 today", allowed)


@pytest.mark.rule("A11-3")
async def test_hallucinated_narrative_omitted(cfg: Config, cal: MarketCalendar,
                                              journal: SqlJournal, tmp_path: Path) -> None:
    seed(journal)
    bad = json.dumps({"what_went_well": "Profit was $9,999.", "what_to_fix": "Nothing.",
                      "tomorrow_focus": "More."})
    r = Reporter(cfg, journal, ScriptedLLM({"a11": [bad]}), ComplianceGuard(cfg, cal),
                 tmp_path, lambda: et(PLAIN_DAY, "16:30"))
    data = await r.daily(PLAIN_DAY)
    assert data["narrative"] is None and "9,999" not in data["text"]


@pytest.mark.rule("A11-2")
async def test_weekly_and_monthly_with_standards(rep: Reporter) -> None:
    w = await rep.weekly(PLAIN_DAY)
    assert w["results"]["trades"] == 2 and "standards" in w
    assert set(w["standards"]["checks"]) == {"expectancy_positive", "profit_factor_ge_1_3",
                                             "win_loss_ge_1_5", "zero_breaches",
                                             "drawdown_inside_limit"}
    m = await rep.monthly(2026, 10)
    assert m["results"]["trades"] == 2 and "wash_sale_report" in m
    assert (rep.out_dir / "2026-10_wash_sales.json").exists()


@pytest.mark.rule("A11-1")
async def test_morning_summary_covers_until_1030(rep: Reporter) -> None:
    data = await rep.daily(PLAIN_DAY, "morning")
    assert data["metrics"]["trades"] == 2 and data["text"].startswith("[PAPER] Morning summary")


@pytest.mark.rule("PA-3")
async def test_reports_carry_risk_adjusted_benchmark_and_statistics(
        rep: Reporter, journal: SqlJournal) -> None:
    for k, eq in enumerate([25000, 25100, 24950, 25200, 25300]):
        journal.upsert(s.daily_metrics, {"date": PLAIN_DAY - timedelta(days=4 - k)},
                       {"trades": 0, "equity": str(eq)})
    for k, px in enumerate([500.0, 505.0]):
        journal.insert(s.levels, {"date": PLAIN_DAY - timedelta(days=3 - 3 * k),
                                  "ticker": "SPY", "prior_close": px})
    w = await rep.weekly(PLAIN_DAY)
    assert {"risk_adjusted", "benchmark", "statistical"} <= set(w)
    assert w["benchmark"]["spy_return_pct"] == 1.0
    assert w["statistical"]["checks"]["trades_ge_100"] is False    # 2 trades: no evidence
    m = await rep.monthly(2026, 10)
    assert "risk_adjusted" in m and "statistical" in m["text"]
