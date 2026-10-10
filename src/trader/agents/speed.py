"""Speed from trigger to order (REQUIREMENTS §5.5a, LAT-1..LAT-4, A5-5).

- ``LatencyTracker`` journals every trigger's outcome: an order with its trigger-to-order time,
  or a missed trade with the reason (LAT-1, LAT-4), and reports the day's p95.
- ``PreArmCache`` holds proposals and reviews written *before* a game-plan trigger's candle
  closes (LAT-2), so at the close only code checks run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog

from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import GamePlanItem, Review, SetupTriggered, TradeProposal
from trader.models.reasons import Reason
from trader.timeutil import et_date

log = structlog.get_logger(__name__)

# LAT-4: the missed-trade categories the owner sees; other codes are reported as they are.
MISSED_CATEGORY: dict[str, str] = {
    Reason.NT_EXPIRED: "NT_EXPIRED", Reason.NT_CHASE_AT_SEND: "NT_CHASE_AT_SEND",
    Reason.NT_LLM_INVALID: "NT_MODEL_TIMEOUT", Reason.NT_MODEL_TIMEOUT: "NT_MODEL_TIMEOUT",
    Reason.NT_APPROVAL_REJECTED: "NT_OWNER_NO", Reason.NT_APPROVAL_TIMEOUT: "NT_OWNER_NO",
    Reason.HALT_LL2: "NT_LIMIT", Reason.HALT_LL8: "NT_LIMIT", Reason.HALT_LL10: "NT_LIMIT",
}


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]


@dataclass
class LatencyTracker:
    journal: SqlJournal | None
    budget_s: float = 10.0
    today: date | None = None
    order_latencies: list[float] = field(default_factory=list)
    missed: dict[str, int] = field(default_factory=dict)

    def _roll(self, now: datetime) -> None:
        d = et_date(now)
        if d != self.today:
            self.today, self.order_latencies, self.missed = d, [], {}

    def order(self, s_: SetupTriggered, proposal_id: str, now: datetime, mode: str,
              model_s: float | None, owner_s: float | None, prearmed: bool) -> float:
        """An order was sent: trigger candle close -> now (LAT-1)."""
        self._roll(now)
        lat = max(0.0, (now - s_.ts).total_seconds())
        if mode == "approve" and owner_s is not None:
            lat_budgeted = max(0.0, lat - owner_s)   # LAT-1: owner time is not ours
        else:
            lat_budgeted = lat
        self.order_latencies.append(lat_budgeted)
        self._row(s_, proposal_id, now, mode, lat, model_s, owner_s, prearmed, "ORDER", None)
        return lat_budgeted

    def missed_trade(self, s_: SetupTriggered, proposal_id: str, now: datetime, mode: str,
                     reason: str, model_s: float | None = None, owner_s: float | None = None,
                     prearmed: bool = False) -> str:
        """A trigger that did not become an order (LAT-4). Returns the category."""
        self._roll(now)
        cat = MISSED_CATEGORY.get(reason, reason)
        self.missed[cat] = self.missed.get(cat, 0) + 1
        self._row(s_, proposal_id, now, mode, None, model_s, owner_s, prearmed, "MISSED", cat)
        return cat

    def _row(self, s_: SetupTriggered, pid: str, now: datetime, mode: str,
             lat: float | None, model_s: float | None, owner_s: float | None, prearmed: bool,
             outcome: str, reason: str | None) -> None:
        if self.journal is None:
            return
        try:
            self.journal.insert(s.latency, {
                "proposal_id": pid, "setup_id": s_.setup_id, "ticker": s_.ticker,
                "setup": s_.setup, "mode": mode, "trigger_ts": s_.ts,
                "order_ts": now if outcome == "ORDER" else None,
                "latency_s": round(lat, 3) if lat is not None else None,
                "model_s": round(model_s, 3) if model_s is not None else None,
                "owner_s": round(owner_s, 3) if owner_s is not None else None,
                "prearmed": prearmed, "outcome": outcome, "reason_code": reason})
        except Exception as exc:   # noqa: BLE001 - measurement never blocks trading
            log.warning("latency_journal_failed", error=str(exc))

    def p95_today(self) -> float | None:
        return p95(self.order_latencies)

    def health(self) -> tuple[str, str]:
        """(level, detail) for the health board: amber above budget (LAT-1)."""
        v = self.p95_today()
        if v is None:
            return "green", "no orders yet today"
        level = "amber" if v > self.budget_s else "green"
        return level, f"trigger-to-order p95 {v:.1f}s (budget {self.budget_s:.0f}s)"

    def summary(self) -> str:
        v = self.p95_today()
        parts = [f"orders {len(self.order_latencies)}"
                 + (f", p95 {v:.1f}s (budget {self.budget_s:.0f}s)" if v is not None else "")]
        if self.missed:
            parts.append("missed: " + ", ".join(f"{k} {n}" for k, n in sorted(
                self.missed.items(), key=lambda x: -x[1])))
        return "Speed (LAT-1/4): " + "; ".join(parts)


@dataclass
class PreArmed:
    item: GamePlanItem
    setup: SetupTriggered          # the expected setup (trigger = plan trigger)
    proposal: TradeProposal | None
    review: Review | None
    model_s: float
    created_at: datetime


@dataclass
class PreArmCache:
    """LAT-2: one pre-armed proposal per (day, window, ticker, setup)."""

    prearm_atr: float = 0.3
    tolerance_atr: float = 0.1
    entries: dict[tuple[date, str, str, str], PreArmed] = field(default_factory=dict)
    in_flight: set[tuple[date, str, str, str]] = field(default_factory=set)

    @staticmethod
    def key(d: date, window: str, ticker: str, setup: str) -> tuple[date, str, str, str]:
        return (d, window, ticker, setup.replace("_NO_PM", ""))

    def near(self, item: GamePlanItem, last: float | None, atr5: float | None) -> bool:
        """Within ``prearm_atr`` x ATR(5m) below the trigger (long setups)."""
        if last is None or not atr5 or atr5 <= 0:
            return False
        return item.trigger - self.prearm_atr * atr5 <= last < item.trigger

    def should_arm(self, k: tuple[date, str, str, str]) -> bool:
        return k not in self.entries and k not in self.in_flight

    def match(self, s_: SetupTriggered) -> PreArmed | None:
        """A pre-armed proposal is used only if trigger, stop and targets are within
        ``tolerance_atr`` x ATR(5m) of the expected ones (LAT-2)."""
        k = self.key(et_date(s_.ts), s_.window, s_.ticker, s_.setup)
        pa = self.entries.get(k)
        if pa is None or pa.proposal is None or pa.review is None:
            return None
        tol = Decimal(str(self.tolerance_atr * s_.atr5))
        exp = pa.setup
        close = all(abs(a - b) <= tol for a, b in (
            (s_.trigger, exp.trigger), (s_.stop, exp.stop), (s_.t1, exp.t1), (s_.t2, exp.t2)))
        if not close:
            log.info("prearm_mismatch", ticker=s_.ticker, setup=s_.setup)
            return None
        return pa

    def drop_day(self, d: date) -> None:
        self.entries = {k: v for k, v in self.entries.items() if k[0] == d}


def rebind(pa: PreArmed, s_: SetupTriggered, proposal_id: str, now: datetime,
           expires_at: datetime) -> tuple[TradeProposal, Review]:
    """Attach a pre-armed proposal and review to the actual setup (A5-5): code sets ids and
    times; every number comes from the actual setup, so A7 checks the real values."""
    if pa.proposal is None or pa.review is None:
        raise ValueError("pre-armed entry has no proposal or review")
    prop = pa.proposal.model_copy(update={
        "proposal_id": proposal_id, "setup_id": s_.setup_id, "trigger": float(s_.trigger),
        "entry": float(s_.entry), "stop": float(s_.stop), "target_1": float(s_.t1),
        "target_2": float(s_.t2), "created_at": now, "expires_at": expires_at})
    review = pa.review.model_copy(update={"proposal_id": proposal_id})
    return prop, review


def expected_setup(item: GamePlanItem, window: str, ticker_atr5: float, entry: Decimal,
                   now: datetime, setup_id: str, context: dict[str, Any] | None = None
                   ) -> SetupTriggered:
    """The setup a plan item will produce if it triggers at its plan levels."""
    from trader.timeutil import cents, cents_down

    stop = cents_down(item.stop)
    r = entry - stop
    t1 = cents(item.target_1) if item.target_1 > float(entry) else cents(entry + r)
    return SetupTriggered(
        setup_id=setup_id, ts=now, bar_closed=False, ticker=item.ticker,
        style="swing" if item.setup.startswith("SWING") else "day",
        window="morning" if window == "morning" else "afternoon", setup=item.setup,
        trigger=cents(item.trigger), entry=entry, stop=stop, t1=t1, t2=cents(entry + 2 * r),
        score=0, atr5=ticker_atr5, vwap=0.0, rvol=0.0, last_price=float(item.trigger),
        resistance=None, filters={}, context={"prearm": True, **(context or {})})
