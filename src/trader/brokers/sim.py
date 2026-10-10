"""SimBroker: the fake Broker for tests, replay and backtests (BACKTEST_ENGINE.md §3).

Fill model v1 on 1-min bars:
- limit buy fills only if price trades through (low <= limit - $0.01), at the limit;
- stop sell triggers if low <= stop and fills at min(stop, open) - slippage;
- take-profit sell fills if high >= limit + $0.01, at the limit (at the open if it gapped over);
- stop and target in the same bar: stop first, unless the bar opened beyond the target;
- marketable-limit exit: this bar's open - slippage, never below the limit;
- at most 10% of the bar's volume fills per bar for limit orders; no fills while halted;
- 0.02% slippage per side on stops and exits, SEC + FINRA fees on sells.
Orders are only matched against bars that start at or after their submission time.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from trader.interfaces import BrokerRejected
from trader.models.messages import (
    Account,
    Bar,
    BracketRequest,
    BrokerPosition,
    Order,
    OrderUpdate,
)

FILL_MODEL_VERSION = "1.0"
CENT = Decimal("0.01")
SEC_FEE_RATE = Decimal("0.0000278")       # per $ of sell proceeds
FINRA_TAF_PER_SHARE = Decimal("0.000166")
FINRA_TAF_MAX = Decimal("8.30")

Role = Literal["entry", "take_profit", "stop_loss", "exit"]


@dataclass
class SimOrder:
    id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int
    role: Role
    tif: Literal["day", "gtc"]
    submitted_at: datetime
    limit: Decimal | None = None
    stop: Decimal | None = None
    filled: int = 0
    fill_value: Decimal = Decimal(0)
    status: str = "new"
    parent_id: str | None = None
    sibling_id: str | None = None
    tp_price: Decimal | None = None
    sl_price: Decimal | None = None
    children: list[str] = field(default_factory=list)

    @property
    def open_qty(self) -> int:
        return self.qty - self.filled

    @property
    def active(self) -> bool:
        return self.status in ("new", "accepted", "partially_filled") and self.open_qty > 0


class SimBroker:
    def __init__(self, now: Callable[[], datetime], equity: Decimal = Decimal("100000"),
                 slippage_pct: float = 0.02, volume_cap: float = 0.10) -> None:
        self.now = now
        self.cash = equity
        self.slip = Decimal(str(slippage_pct)) / 100
        self.volume_cap = volume_cap
        self.orders: dict[str, SimOrder] = {}
        self.by_cid: dict[str, str] = {}
        self.holdings: dict[str, int] = {}
        self.cost: dict[str, Decimal] = {}
        self.last: dict[str, Decimal] = {}
        self.halted: set[str] = set()
        self.fees = Decimal(0)
        self._ids = itertools.count(1)
        self._updates: asyncio.Queue[OrderUpdate] = asyncio.Queue()
        self.pending_updates: list[OrderUpdate] = []
        self._streaming = False     # True once a live-style consumer iterates order_updates()
        self.submissions = 0
        self.fail_next = 0          # chaos: number of upcoming calls that raise

    # -- Broker interface -------------------------------------------------------------------
    async def account(self) -> Account:
        eq = self.equity()
        return Account(equity=eq, cash=self.cash, buying_power=max(Decimal(0), self.cash))

    def equity(self) -> Decimal:
        mv = sum((self.last.get(s, Decimal(0)) * q for s, q in self.holdings.items()),
                 Decimal(0))
        return (self.cash + mv).quantize(CENT, rounding=ROUND_HALF_UP)

    async def positions(self) -> list[BrokerPosition]:
        out = []
        for s, q in self.holdings.items():
            if q:
                avg = (self.cost[s] / q).quantize(Decimal("0.0001"))
                last = self.last.get(s, avg)
                out.append(BrokerPosition(symbol=s, qty=q, avg_entry_price=avg,
                                          market_value=last * q,
                                          unrealized_pl=(last - avg) * q))
        return out

    async def open_orders(self) -> list[Order]:
        return [self._snap(o) for o in self.orders.values() if o.active]

    async def submit_bracket(self, req: BracketRequest) -> Order:
        self._maybe_fail()
        if req.client_order_id in self.by_cid:   # IF-3 idempotent
            return self._snap(self.orders[self.by_cid[req.client_order_id]])
        if not req.stop_price < req.limit_price < req.take_profit:
            raise BrokerRejected(f"bracket prices out of order: {req}")
        cost = req.limit_price * req.qty
        if cost > self.cash + Decimal("0.01"):
            raise BrokerRejected("insufficient buying power (1x, no margin)")
        o = SimOrder(id=f"sim-{next(self._ids)}", client_order_id=req.client_order_id,
                     symbol=req.symbol, side="buy", qty=req.qty, role="entry",
                     tif=req.time_in_force, submitted_at=self.now(), limit=req.limit_price,
                     tp_price=req.take_profit, sl_price=req.stop_price, status="accepted")
        self.orders[o.id] = o
        self.by_cid[o.client_order_id] = o.id
        self.submissions += 1
        self._emit("new", o)
        return self._snap(o)

    async def replace_stop(self, order_id: str, new_stop: Decimal) -> Order:
        self._maybe_fail()
        o = self.orders.get(order_id)
        if o is None or o.role != "stop_loss" or not o.active:
            raise BrokerRejected(f"no active stop order {order_id}")
        o.stop = new_stop
        self._emit("replaced", o)
        return self._snap(o)

    async def cancel(self, order_id: str) -> None:
        self._maybe_fail()
        o = self.orders.get(order_id)
        if o is None or not o.active:
            return
        o.status = "canceled" if o.filled == 0 else "partially_filled_canceled"
        o.qty = o.filled if o.role == "entry" else o.qty
        self._emit("canceled", o)

    async def close_position(self, symbol: str, qty: int | None = None,
                             limit_price: Decimal | None = None) -> Order:
        self._maybe_fail()
        held = self.holdings.get(symbol, 0)
        reserved = sum(o.open_qty for o in self.orders.values()
                       if o.active and o.side == "sell" and o.symbol == symbol)
        n = held if qty is None else qty
        if n <= 0 or n > held - reserved:
            raise BrokerRejected(f"insufficient qty available for {symbol}: held {held}, "
                                 f"reserved {reserved}, requested {n}")
        o = SimOrder(id=f"sim-{next(self._ids)}", client_order_id=f"exit-{next(self._ids)}",
                     symbol=symbol, side="sell", qty=n, role="exit", tif="day",
                     submitted_at=self.now(), limit=limit_price, status="accepted")
        self.orders[o.id] = o
        self.submissions += 1
        self._emit("new", o)
        return self._snap(o)

    async def order_updates(self) -> AsyncIterator[OrderUpdate]:
        self._streaming = True
        while True:
            yield await self._updates.get()

    async def clock_time(self) -> datetime:
        return self.now()

    # -- simulation ---------------------------------------------------------------------------
    def drain_updates(self) -> list[OrderUpdate]:
        out, self.pending_updates = self.pending_updates, []
        return out

    def match(self, bar: Bar) -> None:
        """Match open orders on ``bar.symbol`` against a closed 1-min bar."""
        self.last[bar.symbol] = Decimal(str(bar.c))
        if bar.symbol in self.halted:
            return
        o_, h, lo = Decimal(str(bar.o)), Decimal(str(bar.h)), Decimal(str(bar.l))
        cap = max(1, int(bar.v * self.volume_cap))
        for o in sorted([x for x in self.orders.values()
                         if x.active and x.symbol == bar.symbol and bar.ts >= x.submitted_at],
                        key=lambda x: (x.role != "stop_loss", x.id)):
            if not o.active:
                continue
            if o.role == "entry":
                if o.limit is not None and lo <= o.limit - CENT:
                    n = min(o.open_qty, cap)
                    self._fill(o, n, o.limit)
            elif o.role == "stop_loss":
                sib = self.orders.get(o.sibling_id or "")
                gapped_over_tp = sib is not None and sib.limit is not None and o_ >= sib.limit
                if o.stop is not None and lo <= o.stop and not gapped_over_tp:
                    px = min(o.stop, o_) * (1 - self.slip)
                    self._fill(o, o.open_qty, px.quantize(CENT))
            elif o.role == "take_profit":
                if o.limit is not None and (h >= o.limit + CENT or o_ >= o.limit):
                    px = max(o.limit, o_) if o_ >= o.limit else o.limit
                    self._fill(o, min(o.open_qty, cap), px)
            elif o.role == "exit":
                px = (o_ * (1 - self.slip)).quantize(CENT)
                if o.limit is None or px >= o.limit:
                    self._fill(o, o.open_qty, px)
                elif h >= o.limit:
                    self._fill(o, o.open_qty, o.limit)

    def end_of_day(self) -> None:
        """Day orders expire at the close."""
        for o in self.orders.values():
            if o.active and o.tif == "day":
                o.status = "expired"
                self._emit("expired", o)

    # -- internals ---------------------------------------------------------------------------
    def _fill(self, o: SimOrder, n: int, price: Decimal) -> None:
        if n <= 0:
            return
        o.filled += n
        o.fill_value += price * n
        o.status = "filled" if o.open_qty == 0 else "partially_filled"
        if o.side == "buy":
            self.cash -= price * n
            self.holdings[o.symbol] = self.holdings.get(o.symbol, 0) + n
            self.cost[o.symbol] = self.cost.get(o.symbol, Decimal(0)) + price * n
            self._add_children(o, n)
        else:
            held = self.holdings.get(o.symbol, 0)
            avg = self.cost[o.symbol] / held if held else Decimal(0)
            proceeds = price * n
            fee = (proceeds * SEC_FEE_RATE).quantize(CENT, rounding=ROUND_HALF_UP) + min(
                FINRA_TAF_MAX, (FINRA_TAF_PER_SHARE * n).quantize(CENT, rounding=ROUND_HALF_UP))
            self.fees += fee
            self.cash += proceeds - fee
            self.holdings[o.symbol] = held - n
            self.cost[o.symbol] = self.cost.get(o.symbol, Decimal(0)) - avg * n
            if self.holdings[o.symbol] == 0:
                self.cost[o.symbol] = Decimal(0)
            sib = self.orders.get(o.sibling_id or "")
            if sib is not None and sib.active:   # OCO: reduce the sibling by what filled
                sib.qty -= n
                if sib.open_qty <= 0:
                    sib.status = "canceled"
                    self._emit("canceled", sib)
        self._emit("fill" if o.open_qty == 0 else "partial_fill", o, n, price)

    def _add_children(self, parent: SimOrder, n: int) -> None:
        """Bracket legs cover the filled quantity of the parent."""
        if not parent.children:
            tp = SimOrder(id=f"sim-{next(self._ids)}", client_order_id=f"{parent.client_order_id}"
                          "-tp", symbol=parent.symbol, side="sell", qty=0, role="take_profit",
                          tif=parent.tif, submitted_at=self.now(), limit=parent.tp_price,
                          parent_id=parent.id, status="accepted")
            sl = SimOrder(id=f"sim-{next(self._ids)}", client_order_id=f"{parent.client_order_id}"
                          "-sl", symbol=parent.symbol, side="sell", qty=0, role="stop_loss",
                          tif=parent.tif, submitted_at=self.now(), stop=parent.sl_price,
                          parent_id=parent.id, status="accepted")
            tp.sibling_id, sl.sibling_id = sl.id, tp.id
            self.orders[tp.id], self.orders[sl.id] = tp, sl
            parent.children = [tp.id, sl.id]
        for cid in parent.children:
            self.orders[cid].qty += n
            if self.orders[cid].status == "canceled" and self.orders[cid].open_qty > 0:
                self.orders[cid].status = "accepted"

    def children_of(self, order_id: str) -> list[Order]:
        o = self.orders.get(order_id)
        return [self._snap(self.orders[c]) for c in (o.children if o else [])]

    def _snap(self, o: SimOrder) -> Order:
        leg: Literal["entry", "take_profit", "stop_loss", "exit"] = o.role
        status = o.status if o.status in ("new", "accepted", "partially_filled", "filled",
                                          "canceled", "expired", "rejected", "replaced") \
            else "canceled"
        return Order(id=o.id, client_order_id=o.client_order_id, symbol=o.symbol, side=o.side,
                     qty=o.qty, filled_qty=o.filled,
                     type=("stop" if o.role == "stop_loss" else "limit" if o.limit is not None
                           else "market"),
                     limit_price=o.limit, stop_price=o.stop, status=status,  # type: ignore[arg-type]
                     leg=leg, parent_id=o.parent_id,
                     filled_avg_price=(o.fill_value / o.filled).quantize(Decimal("0.0001"))
                     if o.filled else None, time_in_force=o.tif, submitted_at=o.submitted_at)

    def _emit(self, event: str, o: SimOrder, n: int = 0, price: Decimal | None = None) -> None:
        u = OrderUpdate(event=event, order=self._snap(o), ts=self.now(),  # type: ignore[arg-type]
                        fill_qty=n, fill_price=price)
        if self._streaming:
            self._updates.put_nowait(u)
        else:
            self.pending_updates.append(u)

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            from trader.interfaces import BrokerUnavailable
            raise BrokerUnavailable("simulated 500")
