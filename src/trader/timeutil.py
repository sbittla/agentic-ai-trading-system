"""Time and money helpers (IF-1, IF-2, DV-1).

Times are timezone-aware everywhere: stored in UTC, compared in US Eastern.
Money is ``Decimal`` rounded to cents. Indicators work in float (statistics);
every price that reaches a proposal, decision, order or P&L is converted here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CENT = Decimal("0.01")


def parse_hhmm(text: str) -> time:
    hours, minutes = text.strip().split(":")
    return time(int(hours), int(minutes))


def require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed (IF-2): {dt!r}")
    return dt


def to_et(dt: datetime) -> datetime:
    return require_aware(dt).astimezone(ET)


def to_utc(dt: datetime) -> datetime:
    return require_aware(dt).astimezone(UTC)


def et_datetime(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET)


def et_date(dt: datetime) -> date:
    return to_et(dt).date()


def et_time(dt: datetime) -> time:
    return to_et(dt).time()


def minutes_since_open(dt: datetime) -> int:
    """Whole minutes since 9:30 ET on the same day (negative before the open)."""
    local = to_et(dt)
    return (local.hour * 60 + local.minute) - (9 * 60 + 30)


def dec(value: float | int | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def cents(value: float | int | str | Decimal) -> Decimal:
    """Round to the nearest cent (entries and targets)."""
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def cents_down(value: float | int | str | Decimal) -> Decimal:
    """Round down to the cent (stops are always rounded down, STRATEGY_SPEC §1)."""
    return dec(value).quantize(CENT, rounding=ROUND_FLOOR)


def add_trading_minutes(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)
