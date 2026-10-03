"""A10 Supervisor (A10-1..A10-4, MONITORING §1)."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import PLAIN_DAY, et
from trader.agents import a10_supervisor as sv
from trader.agents.a10_supervisor import Scheduler, Supervisor
from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config


@pytest.mark.rule("A10-2")
def test_health_grades_match_monitoring_table() -> None:
    assert sv.grade_alpaca_latency_s(0.5, 0) == "green"
    assert sv.grade_alpaca_latency_s(2.0, 0) == "amber"
    assert sv.grade_alpaca_latency_s(0.5, 3) == "red"
    assert sv.grade_data_age_s(5, True) == "green" and sv.grade_data_age_s(30, True) == "amber"
    assert sv.grade_data_age_s(61, True) == "red" and sv.grade_data_age_s(600, False) == "green"
    assert sv.grade_llm(True, 9.8) == "green" and sv.grade_llm(True, 30) == "amber"
    assert sv.grade_llm(True, 50) == "red" and sv.grade_llm(False, 1) == "red"
    assert sv.grade_pg_ms(10) == "green" and sv.grade_pg_ms(100) == "amber"
    assert sv.grade_pg_ms(None) == "red"
    assert sv.grade_heartbeat_min(1, 2, 5) == "green" and sv.grade_heartbeat_min(3, 2, 5) == "amber"
    assert sv.grade_heartbeat_min(6, 2, 5) == "red"


@pytest.mark.rule("A10-2")
def test_required_red_blocks_entries(cfg: Config, cal: MarketCalendar,
                                     macro: MacroCalendar) -> None:
    sup = Supervisor(cfg, cal, macro)
    now = et(PLAIN_DAY, "09:40")
    sup.set_health("ollama", "green", "ok", now)
    assert not sup.entries_blocked()[0]
    assert sup.set_health("ollama", "red", "container stopped", now) is not None  # alert once
    assert sup.set_health("ollama", "red", "container stopped", now) is None
    blocked, why = sup.entries_blocked()
    assert blocked and "ollama" in why
    sup.set_health("ollama", "green", "ok", now)
    sup.set_health("news", "red", "stale", now)          # news red does not block everything
    assert not sup.entries_blocked()[0] and not sup.news_ok()


@pytest.mark.rule("A10-1")
def test_preflight(cfg: Config, cal: MarketCalendar, macro: MacroCalendar) -> None:
    sup = Supervisor(cfg, cal, macro)
    ok, problems = sup.preflight(PLAIN_DAY)
    assert ok, problems
    ok, problems = sup.preflight(date(2026, 11, 26))
    assert not ok and "not a trading day" in problems
    ok, problems = sup.preflight(date(2027, 1, 5))   # past the last listed CPI date
    assert not ok and any("macro calendar" in p for p in problems)


@pytest.mark.rule("A10-1")
async def test_scheduler_runs_jobs_once_on_trading_days(cal: MarketCalendar) -> None:
    sch = Scheduler(cal)
    fired: list[str] = []
    sch.add("plan", et(PLAIN_DAY, "09:00").time(), lambda now: fired.append("plan"))
    sch.add("weekly", et(PLAIN_DAY, "17:00").time(), lambda now: fired.append("weekly"),
            when="friday")
    await sch.fire_due(et(PLAIN_DAY, "08:59"))
    assert fired == []
    await sch.fire_due(et(PLAIN_DAY, "09:00"))
    await sch.fire_due(et(PLAIN_DAY, "09:30"))
    assert fired == ["plan"]
    await sch.fire_due(et(PLAIN_DAY, "17:00"))       # Oct 16 2026 is a Friday
    assert fired == ["plan", "weekly"]
    await sch.fire_due(et(date(2026, 10, 17), "09:30"))   # Saturday
    assert fired == ["plan", "weekly"]


@pytest.mark.rule("A10-1")
async def test_weekly_job_moves_to_thursday_on_friday_holiday(cal: MarketCalendar) -> None:
    sch = Scheduler(cal)
    fired: list[date] = []
    sch.add("weekly", et(PLAIN_DAY, "17:00").time(), lambda now: fired.append(now.date()),
            when="friday")
    good_friday_week_thu = date(2027, 3, 25)   # Good Friday 2027-03-26 is a holiday
    await sch.fire_due(et(good_friday_week_thu, "17:00"))
    assert fired == [good_friday_week_thu]


@pytest.mark.rule("A10-1")
async def test_restart_after_deadline_skips_missed_jobs(cal: MarketCalendar) -> None:
    """A restart at 22:16 must not replay the morning plan or the no-trade log."""
    sch = Scheduler(cal)
    fired: list[str] = []
    sch.add("plan", et(PLAIN_DAY, "09:00").time(), lambda now: fired.append("plan"),
            until=et(PLAIN_DAY, "10:15").time())
    sch.add("no_trade_log", et(PLAIN_DAY, "10:16").time(), lambda now: fired.append("ntl"))
    await sch.fire_due(et(PLAIN_DAY, "22:16"))
    assert fired == []
    await sch.fire_due(et(PLAIN_DAY, "22:17"))           # skipped for the day, not retried
    assert fired == []
    late = Scheduler(cal)                                # restart inside the window
    late.add("plan", et(PLAIN_DAY, "09:00").time(), lambda now: fired.append("plan"),
             until=et(PLAIN_DAY, "10:15").time())
    late.add("no_trade_log", et(PLAIN_DAY, "10:16").time(), lambda now: fired.append("ntl"))
    await late.fire_due(et(PLAIN_DAY, "10:07"))
    assert fired == ["plan"]


@pytest.mark.rule("A10-1")
async def test_failed_job_is_retried_until_its_deadline(cal: MarketCalendar) -> None:
    """The 9:00 plan fell back because the LLM was down: retry every 5 min until 10:15."""
    sch = Scheduler(cal)
    results = [False, False, True]
    calls: list[str] = []

    def plan(now):  # type: ignore[no-untyped-def]
        calls.append(f"{now:%H:%M}")
        return results.pop(0)

    sch.add("plan", et(PLAIN_DAY, "09:00").time(), plan, until=et(PLAIN_DAY, "10:15").time(),
            retry_min=5)
    for hhmm in ("09:00", "09:02", "09:05", "09:10", "09:15", "09:20"):
        await sch.fire_due(et(PLAIN_DAY, hhmm))
    assert calls == ["09:00", "09:05", "09:10"]           # succeeded at 09:10; no more runs
    never = Scheduler(cal)
    never.add("plan", et(PLAIN_DAY, "09:00").time(), lambda now: False,
              until=et(PLAIN_DAY, "09:07").time(), retry_min=5)
    assert await never.fire_due(et(PLAIN_DAY, "09:00")) == ["plan"]
    assert await never.fire_due(et(PLAIN_DAY, "09:05")) == ["plan"]
    assert await never.fire_due(et(PLAIN_DAY, "09:10")) == []   # past the deadline


@pytest.mark.rule("A10-3")
@pytest.mark.rule("A10-4")
def test_modes_and_kill_state(cfg: Config, cal: MarketCalendar, macro: MacroCalendar) -> None:
    sup = Supervisor(cfg, cal, macro)
    assert sup.mode_tag() == "[PAPER]"
    live = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"environment": "live"})})
    assert Supervisor(live, cal, macro).mode_tag() == "[LIVE]"
    assert cfg.style_mode("day") == "auto" and cfg.style_mode("swing") == "auto"
    sup.kill_switch = True
    assert sup.kill_switch
