"""A10 Supervisor (A10-1..A10-4, MONITORING.md §1).

Owns the schedule, the health score, the kill switch and the run modes. Health is graded
green/amber/red per MONITORING.md; any red on a required component blocks new entries while
exits and broker-side stops continue.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

import structlog

from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config
from trader.models.messages import HealthStatus
from trader.timeutil import et_datetime, to_et

log = structlog.get_logger(__name__)
Level = Literal["green", "amber", "red"]
REQUIRED = ("alpaca", "data", "ollama", "postgres", "clock", "reconcile")


def grade(value: float | None, amber_at: float, red_at: float) -> Level:
    if value is None:
        return "red"
    if value >= red_at:
        return "red"
    if value >= amber_at:
        return "amber"
    return "green"


# Thresholds from MONITORING.md §1 (value units in the names)
def grade_alpaca_latency_s(v: float | None, failures: int) -> Level:
    return "red" if failures >= 3 or v is None else grade(v, 1.0, 3.0)


def grade_data_age_s(v: float | None, in_window: bool) -> Level:
    if not in_window:
        return "green"
    return grade(v, 10, 60.0001)


def grade_llm(up: bool, p95_s: float) -> Level:
    return "red" if not up else grade(p95_s, 20, 45.0001)


def grade_pg_ms(v: float | None) -> Level:
    return grade(v, 50, 500)


def grade_heartbeat_min(age_min: float | None, amber: float, red: float) -> Level:
    return grade(age_min, amber, red + 1e-9)


def grade_clock_drift_s(v: float | None) -> Level:
    return grade(None if v is None else abs(v), 0.5, 2.0001)


@dataclass
class Job:
    name: str
    at: time
    fn: Callable[[datetime], Awaitable[bool | None] | bool | None]
    when: Literal["trading", "friday", "sunday", "daily", "month_end"] = "trading"
    until: time | None = None       # latest time a late (missed) run may still start
    retry_min: int = 0              # > 0: a run that returns False is retried after this delay
    last_fired: date | None = None
    next_try: datetime | None = None


DEFAULT_CATCH_UP = timedelta(minutes=30)


class Scheduler:
    """Deterministic calendar-aware job runner (same in live and replay).

    A job missed while the service was down runs late only until its ``until`` time (default
    30 minutes after ``at``); later it is skipped for the day, so a restart in the evening does
    not replay the morning plan, the no-trade logs and the reports on stale data."""

    def __init__(self, calendar: MarketCalendar) -> None:
        self.cal = calendar
        self.jobs: list[Job] = []

    def add(self, name: str, at: time,
            fn: Callable[[datetime], Awaitable[bool | None] | bool | None],
            when: Literal["trading", "friday", "sunday", "daily", "month_end"] = "trading",
            until: time | None = None, retry_min: int = 0) -> None:
        self.jobs.append(Job(name, at, fn, when, until, retry_min))
        self.jobs.sort(key=lambda j: j.at)

    @staticmethod
    def _deadline(j: Job, d: date) -> datetime:
        if j.until is not None:
            return et_datetime(d, j.until)
        return et_datetime(d, j.at) + DEFAULT_CATCH_UP

    def _due_today(self, j: Job, d: date) -> bool:
        if j.when == "daily":
            return True
        if j.when == "sunday":
            return d.weekday() == 6
        if not self.cal.is_trading_day(d):
            return False
        if j.when == "friday":   # last trading day of the week (Thursday if Friday is a holiday)
            return self.cal.next_trading_day(d).isocalendar()[:2] != d.isocalendar()[:2]
        if j.when == "month_end":
            return self.cal.next_trading_day(d).month != d.month
        return True

    async def fire_due(self, now: datetime) -> list[str]:
        local = to_et(now)
        d = local.date()
        fired = []
        for j in self.jobs:
            if j.last_fired == d or not self._due_today(j, d):
                continue
            if local < et_datetime(d, j.at) or (j.next_try is not None and now < j.next_try):
                continue
            j.last_fired = d
            if local > self._deadline(j, d):
                log.info("job_skipped_missed", job=j.name, day=d.isoformat())
                continue
            res = j.fn(now)
            if inspect.isawaitable(res):
                res = await res
            fired.append(j.name)
            if res is False and j.retry_min > 0:
                j.last_fired = None   # A10: e.g. the LLM was down for the plan; try again
                j.next_try = now + timedelta(minutes=j.retry_min)
                log.info("job_retry_scheduled", job=j.name, at=j.next_try.isoformat())
            else:
                j.next_try = None
        return fired


@dataclass
class Supervisor:
    cfg: Config
    calendar: MarketCalendar
    macro: MacroCalendar
    health: dict[str, HealthStatus] = field(default_factory=dict)
    kill_switch: bool = False
    kill_reason: str = ""
    paused: set[str] = field(default_factory=set)

    def set_health(self, component: str, level: Level, detail: str, now: datetime
                   ) -> HealthStatus | None:
        prev = self.health.get(component)
        hs = HealthStatus(component=component, level=level, detail=detail, ts=now)
        self.health[component] = hs
        if prev is None or prev.level != level:
            log.info("health_change", component=component, level=level, detail=detail)
            return hs
        return None

    def entries_blocked(self) -> tuple[bool, str]:
        reds = [f"{k}: {v.detail}" for k, v in self.health.items()
                if k in REQUIRED and v.level == "red"]
        return (bool(reds), "; ".join(reds))

    def news_ok(self) -> bool:
        h = self.health.get("news")
        return h is None or h.level != "red"

    def notifier_ok(self) -> bool:
        h = self.health.get("notifier")
        return h is None or h.level != "red"

    def overall(self) -> Level:
        levels = [v.level for v in self.health.values()]
        if any(lv == "red" for lv in levels):
            return "red"
        return "amber" if any(lv == "amber" for lv in levels) else "green"

    def preflight(self, d: date) -> tuple[bool, list[str]]:
        """7:00 pre-flight: green only if every check is green or amber (MONITORING §1)."""
        problems = [f"{k}: {v.detail}" for k, v in self.health.items() if v.level == "red"]
        if not self.calendar.is_trading_day(d):
            problems.append("not a trading day")
        if not self.macro.covers(d):
            problems.append("macro calendar has no CPI date on or after today (A10)")
        return (not problems, problems)

    def mode_tag(self) -> str:
        """SEC-7: every message starts with the environment."""
        return "[LIVE]" if self.cfg.mode.environment == "live" else "[PAPER]"
