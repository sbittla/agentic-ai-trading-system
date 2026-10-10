"""AlpacaBroker: the only module that uses Alpaca's Trading API (IF-6, SR-3, SR-6).

Synchronous alpaca-py calls run in a worker thread with a timeout and map to typed errors
(IF-4). ``submit_bracket`` is idempotent on ``client_order_id`` (IF-3). Bracket children are
mapped to their parent so position legs can be tracked. The removed PDT fields
``daytrade_count`` / ``daytrading_buying_power`` are never read (SR-1).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog

from trader.interfaces import BrokerRejected, BrokerUnavailable
from trader.models.messages import (
    Account,
    BracketRequest,
    BrokerPosition,
    Order,
    OrderUpdate,
)

log = structlog.get_logger(__name__)

_STATUS = {"new": "new", "accepted": "accepted", "pending_new": "pending_new",
           "partially_filled": "partially_filled", "filled": "filled", "canceled": "canceled",
           "expired": "expired", "rejected": "rejected", "replaced": "replaced", "held": "held",
           "done_for_day": "expired", "pending_cancel": "accepted",
           "pending_replace": "accepted", "stopped": "accepted", "suspended": "held",
           "calculated": "accepted", "accepted_for_bidding": "accepted"}
_EVENTS = {"new": "new", "fill": "fill", "partial_fill": "partial_fill", "canceled": "canceled",
           "expired": "expired", "rejected": "rejected", "replaced": "replaced",
           "done_for_day": "expired"}


def _v(x: Any) -> str:
    return str(getattr(x, "value", x))


def _dec(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


class AlpacaBroker:
    def __init__(self, key: str, secret: str, *, paper: bool = True, timeout_s: float = 10.0,
                 ) -> None:
        from alpaca.trading.client import TradingClient

        self.paper = paper
        self.timeout_s = timeout_s
        self._key, self._secret = key, secret
        self.client = TradingClient(key, secret, paper=paper)
        self.parent_of: dict[str, str] = {}
        self.leg_of: dict[str, str] = {}
        self.children: dict[str, list[Order]] = {}
        self.latency_s: float | None = None

    async def _call(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            res = await asyncio.wait_for(asyncio.to_thread(fn, *args, **kw), self.timeout_s)
        except TimeoutError as exc:
            raise BrokerUnavailable(f"timeout after {self.timeout_s}s") from exc
        except Exception as exc:   # alpaca APIError / requests errors
            status = getattr(exc, "status_code", None)
            msg = str(exc)
            if status is not None and 400 <= int(status) < 500:
                raise BrokerRejected(msg) from exc
            raise BrokerUnavailable(msg) from exc
        self.latency_s = loop.time() - t0
        return res

    # -- conversion ---------------------------------------------------------------------------
    def _order(self, o: Any, leg: str | None = None, parent: str | None = None) -> Order:
        oid = str(o.id)
        typ = _v(o.type or o.order_type)
        role = leg or self.leg_of.get(oid) or (
            "entry" if _v(o.side) == "buy" else
            "take_profit" if parent and typ == "limit" else
            "stop_loss" if parent and typ in ("stop", "stop_limit") else "exit")
        return Order(
            id=oid, client_order_id=o.client_order_id or oid, symbol=o.symbol,
            side="buy" if _v(o.side) == "buy" else "sell", qty=int(float(o.qty or 0)),
            filled_qty=int(float(o.filled_qty or 0)),
            type=typ if typ in ("limit", "market", "stop", "stop_limit")  # type: ignore[arg-type]
            else "limit",
            limit_price=_dec(o.limit_price), stop_price=_dec(o.stop_price),
            status=_STATUS.get(_v(o.status), "accepted"),   # type: ignore[arg-type]
            leg=role,   # type: ignore[arg-type]
            parent_id=parent or self.parent_of.get(oid),
            filled_avg_price=_dec(o.filled_avg_price),
            time_in_force="gtc" if _v(o.time_in_force) == "gtc" else "day",
            submitted_at=o.submitted_at)

    def _register_legs(self, parent: Any) -> list[Order]:
        out = []
        for lg in parent.legs or []:
            role = "take_profit" if _v(lg.type or lg.order_type) == "limit" else "stop_loss"
            self.parent_of[str(lg.id)] = str(parent.id)
            self.leg_of[str(lg.id)] = role
            out.append(self._order(lg, role, str(parent.id)))
        self.children[str(parent.id)] = out
        return out

    # -- Broker interface ---------------------------------------------------------------------
    async def account(self) -> Account:
        a = await self._call(self.client.get_account)
        # SR-1: daytrade_count / daytrading_buying_power are not used
        return Account(equity=Decimal(str(a.equity)), cash=Decimal(str(a.cash)),
                       buying_power=Decimal(str(a.buying_power)), status=_v(a.status))

    async def positions(self) -> list[BrokerPosition]:
        ps = await self._call(self.client.get_all_positions)
        return [BrokerPosition(symbol=p.symbol, qty=int(float(p.qty)),
                               avg_entry_price=Decimal(str(p.avg_entry_price)),
                               market_value=Decimal(str(p.market_value or 0)),
                               unrealized_pl=Decimal(str(p.unrealized_pl or 0))) for p in ps]

    async def open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = await self._call(self.client.get_orders, GetOrdersRequest(
            status=QueryOrderStatus.OPEN, nested=True, limit=500))
        out: list[Order] = []
        for o in orders:
            out.append(self._order(o))
            out += self._register_legs(o)
        return out

    async def submit_bracket(self, req: BracketRequest) -> Order:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        with contextlib.suppress(BrokerRejected, BrokerUnavailable):   # IF-3 idempotency
            existing = await self._call(self.client.get_order_by_client_id, req.client_order_id)
            if existing is not None:
                return self._order(existing, "entry")
        order = await self._call(self.client.submit_order, LimitOrderRequest(
            symbol=req.symbol, qty=req.qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC if req.time_in_force == "gtc" else TimeInForce.DAY,
            limit_price=float(req.limit_price), order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=float(req.take_profit)),
            stop_loss=StopLossRequest(stop_price=float(req.stop_price)),
            client_order_id=req.client_order_id))
        self.leg_of[str(order.id)] = "entry"
        self._register_legs(order)
        return self._order(order, "entry")

    async def replace_stop(self, order_id: str, new_stop: Decimal) -> Order:
        from alpaca.trading.requests import ReplaceOrderRequest

        o = await self._call(self.client.replace_order_by_id, order_id,
                             ReplaceOrderRequest(stop_price=float(new_stop)))
        new_id = str(o.id)
        parent = self.parent_of.get(order_id)
        if parent:   # a replace creates a new order id; keep the leg mapping
            self.parent_of[new_id] = parent
            self.leg_of[new_id] = "stop_loss"
        return self._order(o, "stop_loss", parent)

    async def cancel(self, order_id: str) -> None:
        await self._call(self.client.cancel_order_by_id, order_id)

    async def close_position(self, symbol: str, qty: int | None = None,
                             limit_price: Decimal | None = None) -> Order:
        """Marketable-limit sell for ``qty`` (TW-2). Callers cancel bracket legs first."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        if qty is None:
            pos = [p for p in await self.positions() if p.symbol == symbol]
            qty = pos[0].qty if pos else 0
        if qty <= 0:
            raise BrokerRejected(f"no position in {symbol}")
        if limit_price is not None:
            req: Any = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                         time_in_force=TimeInForce.DAY,
                                         limit_price=float(limit_price))
        else:
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                     time_in_force=TimeInForce.DAY)
        o = await self._call(self.client.submit_order, req)
        self.leg_of[str(o.id)] = "exit"
        return self._order(o, "exit")

    def children_of(self, order_id: str) -> list[Order]:
        return self.children.get(order_id, [])

    async def order_updates(self) -> AsyncIterator[OrderUpdate]:
        from alpaca.trading.stream import TradingStream

        queue: asyncio.Queue[OrderUpdate] = asyncio.Queue()
        stream = TradingStream(self._key, self._secret, paper=self.paper)

        async def handler(data: Any) -> None:
            o = data.order
            ev = _EVENTS.get(_v(data.event))
            if ev is None:
                return
            if _v(o.side) == "buy" and o.legs:
                self._register_legs(o)
            elif str(o.id) not in self.parent_of and _v(o.side) == "sell" and \
                    _v(o.order_class) == "bracket":
                with contextlib.suppress(BrokerRejected, BrokerUnavailable):
                    # child update before we saw the nested parent: refresh the mapping
                    await self.open_orders()
            await queue.put(OrderUpdate(
                event=ev,   # type: ignore[arg-type]
                order=self._order(o), ts=data.timestamp,
                fill_qty=int(float(getattr(data, "qty", 0) or 0)),
                fill_price=_dec(getattr(data, "price", None))))

        stream.subscribe_trade_updates(handler)
        task = asyncio.create_task(stream._run_forever())   # type: ignore[no-untyped-call]
        try:
            while True:
                yield await queue.get()
        finally:
            await stream.stop_ws()
            task.cancel()

    async def clock_time(self) -> datetime:
        c = await self._call(self.client.get_clock)
        ts: datetime = c.timestamp
        return ts

    # -- pre-flight helpers --------------------------------------------------------------------
    async def is_tradable(self, symbol: str) -> bool:
        try:
            a = await self._call(self.client.get_asset, symbol)
        except (BrokerRejected, BrokerUnavailable):
            return False
        return bool(a.tradable) and _v(a.status) == "active"

    async def calendar(self, start: date, end: date) -> list[tuple[date, str, str]]:
        from alpaca.trading.requests import GetCalendarRequest

        days = await self._call(self.client.get_calendar, GetCalendarRequest(start=start,
                                                                             end=end))
        return [(d.date, str(d.open), str(d.close)) for d in days]
