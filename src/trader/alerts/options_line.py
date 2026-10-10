"""OP-14: one options line on the lightweight alerts bot's ORB, VWAP and HOD/LOD alerts.

Optional (``options.enabled`` in ``config/alerts.yaml``, default off). One REST snapshot
call per alert, at most one call every ``min_interval_s``; sized to a fixed dollar risk.
It reuses A14's pure functions (``trader.options.strategist.suggest``), so the bot and
trader-core give the same structure for the same inputs. The bot has no IV history (IV rank
reads "n/a", the middle column of OP-1) and makes no contracts call, so open interest is not
checked here; the line says so.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from trader.config import OptionsCfg
from trader.options.data import fill_greeks, quote_from_snapshot
from trader.options.strategist import LABEL, View, suggest
from trader.timeutil import to_et

log = structlog.get_logger(__name__)

BULL = {"orb_up": "bull", "vwap_up": "bull", "hod": "bull",
        "orb_down": "bear", "vwap_down": "bear", "lod": "bear"}


class OptionsLineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    fixed_risk_usd: float = Field(default=200, gt=0)
    min_interval_s: float = Field(default=10, ge=1)
    day_dte: tuple[int, int] = (5, 14)


def view_for(kind: str, symbol: str, price: float, st: Any) -> View | None:
    """Invalidation from the alert's own level: OR low/high, VWAP; T1 at 1.5R."""
    direction = BULL.get(kind)
    if direction is None or price <= 0:
        return None
    vwap = st.vwap if st is not None else None
    if kind == "orb_up" and st is not None and st.or_low is not None:
        inv = st.or_low
    elif kind == "orb_down" and st is not None and st.or_high is not None:
        inv = st.or_high
    elif vwap is not None and ((direction == "bull" and vwap < price)
                               or (direction == "bear" and vwap > price)):
        inv = vwap
    else:
        inv = price * (0.99 if direction == "bull" else 1.01)
    r = abs(price - inv)
    if r <= 0:
        return None
    sign = 1 if direction == "bull" else -1
    return View(symbol, direction, price, inv, price + sign * 1.5 * r, price + sign * 3 * r,
                "day", 0, "alert", kind, False, True)


class OptionsLine:
    def __init__(self, settings: OptionsLineSettings, client: Any,
                 now: Callable[[], datetime]) -> None:
        self.s = settings
        self.client = client
        self.now = now
        self.last_call = -1e9
        self.cfg = OptionsCfg(day_dte=settings.day_dte, min_open_interest=0, account_level=3)

    async def line(self, kind: str, symbol: str, price: float, st: Any) -> str | None:
        if not self.s.enabled:
            return None
        view = view_for(kind, symbol, price, st)
        if view is None:
            return None
        mono = _time.monotonic()
        if mono - self.last_call < self.s.min_interval_s:
            return None                                       # OP-14 rate limit
        self.last_call = mono
        now = self.now()
        today = to_et(now).date()
        try:
            snaps = await self.client.snapshots(
                symbol, today + timedelta(days=self.s.day_dte[0]),
                today + timedelta(days=self.s.day_dte[1]), price * 0.9, price * 1.1)
        except Exception as exc:   # noqa: BLE001 - the stock alert still goes out
            log.info("options_line_failed", symbol=symbol, error=str(exc))
            return None
        quotes = [fill_greeks(q, price, now) for sym, sn in snaps.items()
                  if (q := quote_from_snapshot(sym, sn)) is not None]
        # equity x 1% = the fixed risk; the premium cap (1%) is the same dollar amount
        sugs, notes = suggest(view, quotes, cfg=self.cfg, now=now,
                              equity=self.s.fixed_risk_usd * 100, risk_pct=1.0, iv_rank=None)
        if not sugs:
            return f"Options: no structure passed ({notes[0] if notes else 'no data'})"
        sg = sugs[0]
        strikes = "/".join(f"{lg.quote.strike:g}{lg.quote.kind}" for lg in sg.legs)
        price_txt = (f"credit ~{-sg.net_mid:.2f}" if sg.is_credit else f"debit ~{sg.net_mid:.2f}")
        return (f"Options: {LABEL[sg.structure]} {sg.expiry:%b %d} {strikes} · {price_txt} · "
                f"max loss ${sg.max_loss:,.0f} x{sg.contracts} · OI not checked · indicative")
