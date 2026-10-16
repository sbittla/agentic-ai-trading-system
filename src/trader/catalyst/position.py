"""Position view from an outlook: stance, vehicle and plan numbers (NC-24..NC-26).

Advisory only (ADR-006): the plan is computed by code and sized by the same risk rules as
every other idea; nothing here can place an order. Long-only stock (ADR-008): a bearish
view is a put debit spread, never a short sale (ADR-015).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from trader.catalyst.outlook import HorizonView
from trader.timeutil import cents


@dataclass
class View:
    stance: str                    # BULLISH | BEARISH | NEUTRAL | NO_TRADE
    horizon: int | None
    vehicle: str                   # stock | call_debit_spread | put_debit_spread | wait | none
    why: str
    plan: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def best_horizon(views: Sequence[HorizonView]) -> HorizonView | None:
    """The horizon with the largest typical move for its uncertainty."""
    return max(views, key=lambda v: abs(v.median_pct) / max(v.sigma_pct, 1e-9), default=None)


def stance(views: Sequence[HorizonView], *, strength_label: str, stance_p: float,
           min_edge: float, earnings_in: int | None, is_earnings: bool
           ) -> tuple[str, HorizonView | None, str]:
    """NC-24."""
    v = best_horizon(views)
    if v is None:
        return "NO_TRADE", None, "no outlook"
    if strength_label == "WEAK":
        return "NO_TRADE", v, "weak catalyst"
    if v.confidence == "LOW":
        return "NO_TRADE", v, f"low confidence ({v.n} similar events)"
    if earnings_in is not None and earnings_in <= v.h and not is_earnings:
        return "NO_TRADE", v, f"earnings in {earnings_in} sessions, inside the {v.h}-session view"
    edge = abs(v.ex_median_pct) / max(v.sigma_pct, 1e-9)
    if v.p_up >= stance_p and v.ex_median_pct > 0 and edge >= min_edge:
        return "BULLISH", v, f"P(up) {v.p_up:.0%}, typical {v.median_pct:+.1f}% in {v.h}"
    if v.p_up <= 1 - stance_p and v.ex_median_pct < 0 and edge >= min_edge:
        return "BEARISH", v, f"P(up) {v.p_up:.0%}, typical {v.median_pct:+.1f}% in {v.h}"
    return "NEUTRAL", v, f"no clear edge (P(up) {v.p_up:.0%}, typical {v.median_pct:+.1f}%)"


def vehicle(st: str, iv_rank: float | None, reaction_agrees: bool | None) -> str:
    """NC-25."""
    if st == "BULLISH":
        if reaction_agrees is False:
            return "wait"            # the market disagrees with the news: let it confirm
        return "call_debit_spread" if iv_rank is not None and iv_rank < 60 else "stock"
    if st == "BEARISH":
        return "wait" if reaction_agrees is False else "put_debit_spread"
    return "none"


def plan(st: str, v: HorizonView | None, *, day0_high: float, day0_low: float, atr: float,
         last: float, sizer: Callable[[float, float], tuple[int, float, str]] | None = None
         ) -> dict[str, Any]:
    """NC-26: entry beyond the day-0 range, invalidation on the other side, T1/T2 at 1.5R/3R
    capped by the horizon's 90th-percentile move. Bearish levels describe the stock for the
    put spread; there is never a stock short."""
    if st not in ("BULLISH", "BEARISH") or v is None or atr <= 0:
        return {}
    if st == "BULLISH":
        trig = cents(max(day0_high, last) + 0.01)
        stop = cents(day0_low - 0.25 * atr)
        r = trig - stop
        cap = cents(last * (1 + max(v.p90_pct, 0.0) / 100))
        t1, t2 = cents(trig + r * Decimal("1.5")), cents(trig + r * 3)
        t2 = min(t2, max(cap, t1))
    else:
        trig = cents(min(day0_low, last) - 0.01)
        stop = cents(day0_high + 0.25 * atr)
        r = stop - trig
        cap = cents(last * (1 + min(v.p10_pct, 0.0) / 100))
        t1, t2 = cents(trig - r * Decimal("1.5")), cents(trig - r * 3)
        t2 = max(t2, min(cap, t1))
    out: dict[str, Any] = {"side": "long" if st == "BULLISH" else "bearish view (puts)",
                           "trigger": float(trig), "invalidation": float(stop),
                           "t1": float(t1), "t2": float(t2), "r": float(r),
                           "horizon": v.h}
    if sizer is not None and st == "BULLISH":
        qty, risk_usd, why = sizer(float(trig), float(stop))
        out.update({"qty": qty, "risk_usd": round(risk_usd, 2), "size_binding": why})
    return out
