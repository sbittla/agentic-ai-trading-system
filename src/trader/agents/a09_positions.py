"""A9 Position monitor (A9-1..A9-4, XR-1..XR-6, DR-1, DR-2, DR-6).

Tracks every open position leg by leg, applies the exit rules on closed bars and at the
flatten times, and returns actions (exits, stop moves) for A8 to execute. Stops only ever move
toward profit (DR-2); a day trade is never converted into a swing trade (DR-6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

import structlog

from trader.agents.a08_execution import EntryTicket
from trader.calendar import MarketCalendar, TradingWindows
from trader.config import Config
from trader.models.messages import Bar, EventFlags, Levels
from trader.models.reasons import Reason
from trader.timeutil import cents_down, et_date, to_et

log = structlog.get_logger(__name__)
D0 = Decimal(0)


class StopWidenError(Exception):
    """DR-2: attempted to move a stop away from profit."""


@dataclass
class Leg:
    name: str
    entry_order_id: str
    qty: int = 0
    open_qty: int = 0
    tp_price: Decimal = D0
    tp_id: str | None = None
    sl_id: str | None = None


@dataclass
class ManagedPosition:
    position_id: str
    ticker: str
    style: Literal["day", "swing"]
    window: str
    setup: str
    planned: bool
    route: str
    override: bool
    trigger: Decimal
    breakout_level: float
    atr5: Decimal
    initial_stop: Decimal
    stop: Decimal
    t1: Decimal
    t2: Decimal
    opened_at: datetime
    opened_day: date
    legs: dict[str, Leg] = field(default_factory=dict)
    qty_filled: int = 0
    cost: Decimal = D0
    t1_hit: bool = False
    max_high: float = 0.0
    exit_qty: int = 0
    exit_value: Decimal = D0
    stop_kind: Literal["initial", "breakeven", "trail"] = "initial"
    pending_exit: str | None = None
    exit_reason: str | None = None
    days_held: int = 0
    reduced_size: bool = False

    @property
    def avg_entry(self) -> Decimal:
        return (self.cost / self.qty_filled).quantize(Decimal("0.0001")) if self.qty_filled \
            else D0

    @property
    def qty_open(self) -> int:
        return self.qty_filled - self.exit_qty

    @property
    def r_per_share(self) -> Decimal:
        return self.avg_entry - self.initial_stop

    @property
    def closed(self) -> bool:
        return self.qty_filled > 0 and self.qty_open <= 0

    def pnl(self) -> Decimal:
        return self.exit_value - self.avg_entry * self.exit_qty

    def r_multiple(self) -> float:
        risk = self.r_per_share * self.qty_filled
        return float(self.pnl() / risk) if risk > 0 else 0.0


@dataclass
class ExitAction:
    position_id: str
    reason: str
    qty: int | None = None


@dataclass
class StopMove:
    position_id: str
    new_stop: Decimal
    reason: str


Action = ExitAction | StopMove


class PositionMonitor:
    def __init__(self, cfg: Config, windows: TradingWindows, calendar: MarketCalendar) -> None:
        self.cfg = cfg
        self.windows = windows
        self.cal = calendar
        self.positions: dict[str, ManagedPosition] = {}
        self.by_order: dict[str, tuple[str, str, str]] = {}   # order id -> (pos, leg, role)
        self.news: dict[str, EventFlags] = {}

    # -- opening -------------------------------------------------------------------------------
    def on_entry_fill(self, ticket: EntryTicket, leg: str, order_id: str, qty: int,
                      price: Decimal, ts: datetime) -> tuple[ManagedPosition, bool]:
        """Returns (position, newly_opened)."""
        p = self.positions.get(ticket.proposal_id)
        new = p is None
        if p is None:
            p = ManagedPosition(
                position_id=ticket.proposal_id, ticker=ticket.ticker,
                style="swing" if ticket.style == "swing" else "day", window=ticket.window,
                setup=ticket.setup, planned=ticket.planned, route=ticket.route,
                override=ticket.override, trigger=ticket.trigger,
                breakout_level=ticket.breakout_level, atr5=ticket.atr5,
                initial_stop=ticket.stop, stop=ticket.stop, t1=ticket.t1, t2=ticket.t2,
                opened_at=ts, opened_day=et_date(ts), max_high=float(price))
            self.positions[p.position_id] = p
        lg = p.legs.get(leg)
        if lg is None:
            lg = Leg(name=leg, entry_order_id=order_id,
                     tp_price=ticket.t1 if leg == "A" else ticket.t2)
            p.legs[leg] = lg
        lg.qty += qty
        lg.open_qty += qty
        p.qty_filled += qty
        p.cost += price * qty
        self.by_order[order_id] = (p.position_id, leg, "entry")
        return p, new

    def register_child(self, parent_order_id: str, child_id: str, role: str) -> None:
        """Map a bracket child (take_profit / stop_loss) to its position leg."""
        key = self.by_order.get(parent_order_id)
        if key is None:
            return
        pid, leg, _ = key
        p = self.positions.get(pid)
        if p is None:
            return
        lg = p.legs[leg]
        if role == "take_profit":
            lg.tp_id = child_id
        else:
            lg.sl_id = child_id
        self.by_order[child_id] = (pid, leg, role)

    # -- fills on exits ----------------------------------------------------------------------
    def on_exit_fill(self, order_id: str, qty: int, price: Decimal, ts: datetime,
                     position_id: str | None = None) -> tuple[ManagedPosition | None, str | None,
                                                         list[Action]]:
        """A take-profit, stop or A9 exit fill. Returns (position, event, follow-up actions)."""
        key = self.by_order.get(order_id)
        if key is None and position_id is not None:
            key = (position_id, "", "exit")
        if key is None:
            return None, None, []
        pid, leg, role = key
        p = self.positions.get(pid)
        if p is None:
            return None, None, []
        p.exit_qty += qty
        p.exit_value += price * qty
        actions: list[Action] = []
        event: str | None = None
        if leg and leg in p.legs:
            p.legs[leg].open_qty = max(0, p.legs[leg].open_qty - qty)
        if role == "take_profit":
            if leg == "A" and not p.t1_hit:
                p.t1_hit = True
                event = Reason.EXIT_T1
                if p.style == "day" and p.qty_open > 0:   # XR-2: rest to break-even
                    be = cents_down(p.avg_entry)
                    if be > p.stop:
                        actions.append(StopMove(pid, be, "XR-2 break-even after T1"))
            else:
                event = Reason.EXIT_T2
        elif role == "stop_loss":
            event = {"initial": Reason.EXIT_STOP, "breakeven": Reason.EXIT_BREAKEVEN,
                     "trail": Reason.EXIT_TRAIL}[p.stop_kind]
        else:
            event = p.pending_exit or Reason.EXIT_FLATTEN_CMD
        if p.closed:
            p.exit_reason = event
        return p, event, actions

    # -- rules on closed bars ----------------------------------------------------------------
    def on_1m(self, bar: Bar, now: datetime) -> list[Action]:
        out: list[Action] = []
        for p in self._open_for(bar.symbol):
            p.max_high = max(p.max_high, bar.h)
            if p.style != "day" or p.pending_exit:
                continue
            dc = self.cfg.exits.day
            # XR-5: not at +0.5R within 15 minutes
            if (not p.t1_hit and now - p.opened_at >= timedelta(minutes=dc.time_stop_min)
                    and Decimal(str(p.max_high)) < p.avg_entry + Decimal(
                        str(dc.time_stop_min_progress_r)) * p.r_per_share):
                out.append(self._exit(p, Reason.EXIT_TIME_STOP_15MIN))
        return out

    def on_5m(self, bar: Bar, now: datetime, vwap: float | None, ema9: float | None,
              atr5: float | None, levels: Levels | None, last5: list[Bar]) -> list[Action]:
        out: list[Action] = []
        for p in self._open_for(bar.symbol):
            if p.pending_exit:
                continue
            if p.style == "day":
                if bar.end <= p.opened_at:
                    continue
                # XR-4: failed trade - 5-min close back below VWAP or the breakout level
                if (vwap is not None and bar.c < vwap) or bar.c < p.breakout_level:
                    out.append(self._exit(p, Reason.EXIT_FAILED_TRADE))
                    continue
                # XR-3: trail the remaining half after T1
                if p.t1_hit and atr5:
                    if self.cfg.exits.day.trail == "ema9_5m" and ema9 is not None:
                        cand = cents_down(ema9 - 0.05 * atr5)
                    else:
                        lows = [b.l for b in last5[-3:]]
                        cand = cents_down(min(lows) - 0.05 * atr5) if lows else p.stop
                    if cand > p.stop and cand < Decimal(str(bar.c)):
                        out.append(StopMove(p.position_id, cand, "XR-3 trail"))
            elif to_et(bar.end).time() == time(15, 50):
                out += self._swing_close_checks(p, bar, now, levels)
        return out

    def _swing_close_checks(self, p: ManagedPosition, bar: Bar, now: datetime,
                            levels: Levels | None) -> list[Action]:
        ex = self.cfg.exits.swing
        d = et_date(now)
        if levels is not None and levels.ema20d is not None and bar.c < levels.ema20d \
                and p.opened_day < d:
            return [self._exit(p, Reason.EXIT_FAILED_TRADE)]   # XR-4 swing
        if self.cal.trading_days_between(p.opened_day, d) >= ex.max_hold_days:
            return [self._exit(p, Reason.EXIT_SWING_MAX_HOLD)]  # XR-5 swing
        nf = self.news.get(self.cfg.canonical(p.ticker))
        if ex.exit_before_earnings and nf is not None and nf.earnings_date is not None and \
                nf.earnings_date <= self.cal.next_trading_day(d):
            return [self._exit(p, Reason.EXIT_EARNINGS)]       # XR-6
        return []

    def on_tick(self, now: datetime) -> list[Action]:
        """TW-2 / A9-2: morning day trades out at 10:30, all day trades by 15:55."""
        out: list[Action] = []
        d = et_date(now)
        if not self.cal.is_trading_day(d):
            return out
        for p in list(self.positions.values()):
            if p.closed or p.pending_exit or p.qty_open <= 0 or p.style != "day":
                continue
            if p.window == "morning" and now >= self.windows.flatten_time(d, "morning"):
                out.append(self._exit(p, Reason.EXIT_TIME_STOP_1030))
            elif now >= self.windows.flatten_time(d, "afternoon"):
                out.append(self._exit(p, Reason.EXIT_TIME_STOP_1555))
            elif p.opened_day < d:   # DR-6: never held overnight
                out.append(self._exit(p, Reason.EXIT_TIME_STOP_1555))
        # XR-6: swing positions with earnings today are closed at the first check
        for p in list(self.positions.values()):
            if p.style == "swing" and not p.closed and not p.pending_exit and p.qty_open > 0:
                nf = self.news.get(self.cfg.canonical(p.ticker))
                if nf is not None and "EARNINGS_TODAY" in nf.flags and \
                        self.windows.window_at(now) is not None:
                    out.append(self._exit(p, Reason.EXIT_EARNINGS))
        return out

    def start_day(self, d: date, levels: dict[str, Levels]) -> list[Action]:
        """Swing trail under the 10-day EMA (XR-3) before the open."""
        out: list[Action] = []
        for p in self.open_positions():
            if p.style != "swing":
                continue
            p.days_held = self.cal.trading_days_between(p.opened_day, d)
            lv = levels.get(p.ticker)
            if lv is not None and lv.ema10d is not None and (p.t1_hit or p.days_held > 0):
                cand = cents_down(lv.ema10d)
                if cand > p.stop and cand < Decimal(str(lv.prior_close)):
                    out.append(StopMove(p.position_id, cand, "XR-3 swing trail 10-day EMA"))
        return out

    def flatten_all(self, reason: str, style: str | None = None) -> list[Action]:
        return [self._exit(p, reason) for p in self.open_positions()
                if not p.pending_exit and (style is None or p.style == style)]

    # -- stop moves ---------------------------------------------------------------------------
    def apply_stop_move(self, mv: StopMove) -> ManagedPosition:
        """DR-2 guard: raises if the new stop is below the current one."""
        p = self.positions[mv.position_id]
        if mv.new_stop < p.stop:
            raise StopWidenError(f"{p.ticker}: stop {p.stop} -> {mv.new_stop} widens (DR-2)")
        p.stop = mv.new_stop
        p.stop_kind = "breakeven" if "break-even" in mv.reason else "trail"
        return p

    # -- helpers -----------------------------------------------------------------------------
    def _exit(self, p: ManagedPosition, reason: str) -> ExitAction:
        p.pending_exit = reason
        return ExitAction(p.position_id, reason, p.qty_open)

    def _open_for(self, symbol: str) -> list[ManagedPosition]:
        return [p for p in self.positions.values()
                if p.ticker == symbol and not p.closed and p.qty_open > 0]

    def open_positions(self) -> list[ManagedPosition]:
        return [p for p in self.positions.values() if not p.closed and p.qty_open > 0]

    def remove_closed(self) -> list[ManagedPosition]:
        done = [p for p in self.positions.values() if p.closed]
        for p in done:
            del self.positions[p.position_id]
        return done

    def convert_to_swing(self, position_id: str) -> None:
        """DR-6: not allowed, ever."""
        raise PermissionError("DR-6: a day trade can never be converted into a swing trade")
