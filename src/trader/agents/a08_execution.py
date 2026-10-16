"""A8 Execution agent (A8-1..A8-4, EX-1..EX-4, SEC-12, SEC-13).

The only agent that calls the broker. Entries are two marketable-limit bracket orders with
idempotent client order IDs; unfilled entries are cancelled after 3 minutes or when price runs
0.5 ATR(5-min) past the trigger; an order-rate guard trips the kill switch; reconcile runs
every minute and after any reconnect.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

import structlog

from trader.config import Config
from trader.interfaces import Broker, BrokerRejected, BrokerUnavailable
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import BracketRequest, Decision, Order, SetupTriggered
from trader.models.reasons import Reason
from trader.timeutil import cents, et_date

log = structlog.get_logger(__name__)


@dataclass
class EntryTicket:
    proposal_id: str
    ticker: str
    style: str
    window: str
    setup: str
    planned: bool
    trigger: Decimal
    atr5: Decimal
    entry: Decimal
    stop: Decimal
    t1: Decimal
    t2: Decimal
    qty: int
    route: Literal["broker", "paper"]
    submitted_at: datetime
    breakout_level: float
    override: bool = False
    orders: dict[str, Order] = field(default_factory=dict)   # leg A/B -> entry order
    filled: dict[str, int] = field(default_factory=dict)
    done: bool = False


@dataclass
class RateGuard:
    """SEC-12: at most 10 order submissions per minute and 30 per day."""

    per_min: int = 10
    per_day: int = 30
    _recent: deque[datetime] = field(default_factory=deque)
    _day: object = None
    _today: int = 0

    def allow(self, now: datetime, n: int = 1) -> bool:
        d = et_date(now)
        if d != self._day:
            self._day, self._today = d, 0
        while self._recent and now - self._recent[0] > timedelta(minutes=1):
            self._recent.popleft()
        if len(self._recent) + n > self.per_min or self._today + n > self.per_day:
            return False
        for _ in range(n):
            self._recent.append(now)
        self._today += n
        return True


def build_legs(proposal_id: str, symbol: str, qty: int, entry: Decimal, stop: Decimal,
               t1: Decimal, t2: Decimal, tif: Literal["day", "gtc"]) -> list[BracketRequest]:
    """EX-4 / STRATEGY_SPEC §2.4: leg A = half (rounded down) with TP at T1, leg B = rest with
    TP at T2, same stop; a 1-share position uses leg B only."""
    a = qty // 2
    b = qty - a
    legs: list[BracketRequest] = []
    if a >= 1:
        legs.append(BracketRequest(client_order_id=f"{proposal_id}-A", symbol=symbol, qty=a,
                                   limit_price=entry, take_profit=t1, stop_price=stop,
                                   time_in_force=tif))
    legs.append(BracketRequest(client_order_id=f"{proposal_id}-B", symbol=symbol, qty=b,
                               limit_price=entry, take_profit=t2, stop_price=stop,
                               time_in_force=tif))
    return legs


def order_sanity(cfg: Config, symbol: str, qty: int, entry: Decimal, stop: Decimal,
                 t1: Decimal, t2: Decimal, last_price: Decimal | None, adv: float | None
                 ) -> str | None:
    """OC-1..OC-4: the last independent check before an order leaves (fat-finger guard, in
    the spirit of SEC Rule 15c3-5 pre-trade controls). Returns why it failed, or None."""
    ex = cfg.execution
    if qty < 1 or entry <= 0 or stop <= 0:
        return f"OC-3 bad order: qty {qty}, entry {entry}, stop {stop}"
    if not stop < entry < t1 <= t2:   # long-only stock (ADR-008)
        return f"OC-3 prices out of order: stop {stop} < entry {entry} < T1 {t1} <= T2 {t2}"
    if last_price is not None and last_price > 0:
        dev = abs(entry - last_price) / last_price * 100
        if dev > Decimal(str(ex.max_price_deviation_pct)):
            return (f"OC-1 limit {entry} is {dev:.2f}% from the last trade {last_price} "
                    f"(max {ex.max_price_deviation_pct}%)")
    if adv is not None and adv > 0 and qty > adv * ex.max_adv_pct / 100:
        return (f"OC-2 {qty} shares is {qty / adv * 100:.2f}% of {symbol}'s 20-day average "
                f"volume (max {ex.max_adv_pct}%)")
    if ex.max_order_notional > 0 and entry * qty > Decimal(str(ex.max_order_notional)):
        return f"OC-4 order value ${entry * qty:,.2f} > cap ${ex.max_order_notional:,.0f}"
    return None


class ExecutionAgent:
    def __init__(self, cfg: Config, broker: Broker, journal: SqlJournal | None,
                 paper_broker: Broker | None = None) -> None:
        self.cfg = cfg
        self.broker = broker
        self.paper_broker = paper_broker
        self.journal = journal
        self.tickets: dict[str, EntryTicket] = {}
        self.order_index: dict[str, tuple[str, str]] = {}   # order id -> (proposal, leg)
        self.rate = RateGuard()
        self.failures = 0
        self.adv: dict[str, float] = {}   # OC-2, set each morning from the daily bars
        self.last_sanity = ""

    def broker_for(self, route: str) -> Broker:
        if route == "paper" and self.paper_broker is not None:
            return self.paper_broker
        return self.broker

    def seen(self, proposal_id: str) -> bool:
        if proposal_id in self.tickets:
            return True
        if self.journal is not None:
            return bool(self.journal.rows(s.orders, {"proposal_id": proposal_id}, limit=1))
        return False

    async def execute(self, d: Decision, setup: SetupTriggered, now: datetime, planned: bool,
                      last_price: Decimal | None = None) -> tuple[str, EntryTicket | None]:
        """Submit the bracket legs for an approved decision. Returns (reason code, ticket)."""
        if d.result != "APPROVE":
            return Reason.EXEC_REJECTED, None
        if self.seen(d.proposal_id):   # A8-2: never send the same proposal twice
            log.warning("duplicate_proposal", proposal_id=d.proposal_id)
            return Reason.EXEC_DUPLICATE, None
        why = order_sanity(self.cfg, setup.ticker, d.qty, d.entry, d.stop, d.t1, d.t2,
                           last_price, self.adv.get(self.cfg.canonical(setup.ticker)))
        if why is not None:
            log.error("order_sanity_failed", proposal_id=d.proposal_id, why=why)
            self.last_sanity = why
            return Reason.EXEC_SANITY, None
        tif: Literal["day", "gtc"] = "gtc" if setup.style == "swing" else "day"
        legs = build_legs(d.proposal_id, setup.ticker, d.qty, d.entry, d.stop, d.t1, d.t2, tif)
        if not self.rate.allow(now, len(legs)):
            return Reason.EXEC_RATE_LIMIT, None
        ticket = EntryTicket(
            proposal_id=d.proposal_id, ticker=setup.ticker, style=setup.style,
            window=setup.window, setup=setup.setup, planned=planned, trigger=setup.trigger,
            atr5=Decimal(str(setup.atr5)), entry=d.entry, stop=d.stop, t1=d.t1, t2=d.t2,
            qty=d.qty, route=d.route, submitted_at=now,
            breakout_level=float(setup.context.get("breakout_level") or setup.trigger),
            override=d.override)
        self.tickets[d.proposal_id] = ticket
        broker = self.broker_for(d.route)
        for req in legs:
            leg = req.client_order_id[-1]
            try:
                order = await broker.submit_bracket(req)
            except (BrokerUnavailable, BrokerRejected) as exc:
                self.failures += 1
                log.error("submit_failed", cid=req.client_order_id, error=str(exc))
                self._journal_order(req, None, d.proposal_id, leg, "rejected", now, d.route)
                continue
            self.failures = 0
            ticket.orders[leg] = order
            ticket.filled[leg] = 0
            self.order_index[order.id] = (d.proposal_id, leg)
            self._journal_order(req, order, d.proposal_id, leg, order.status, now, d.route)
        if not ticket.orders:
            ticket.done = True
            return Reason.EXEC_REJECTED, ticket
        return Reason.EXEC_SUBMITTED, ticket

    def _journal_order(self, req: BracketRequest, order: Order | None, proposal_id: str,
                       leg: str, status: str, now: datetime, route: str) -> None:
        if self.journal is None:
            return
        self.journal.insert(s.orders, {
            "client_order_id": req.client_order_id, "broker_id": order.id if order else None,
            "proposal_id": proposal_id, "leg": leg, "type": "bracket", "side": "buy",
            "ticker": req.symbol, "qty": req.qty, "limit": req.limit_price,
            "stop": req.stop_price, "take_profit": req.take_profit, "status": status,
            "ts": now, "route": route})

    def on_entry_fill(self, order_id: str, qty: int) -> EntryTicket | None:
        key = self.order_index.get(order_id)
        if key is None:
            return None
        t = self.tickets[key[0]]
        t.filled[key[1]] = t.filled.get(key[1], 0) + qty
        return t

    def open_entry_orders(self) -> list[tuple[EntryTicket, str, Order]]:
        out = []
        for t in self.tickets.values():
            if t.done:
                continue
            for leg, o in t.orders.items():
                if t.filled.get(leg, 0) < o.qty:
                    out.append((t, leg, o))
        return out

    async def check_cancels(self, now: datetime, last_prices: dict[str, float],
                            swing_cutoff: datetime | None = None) -> list[tuple[str, str]]:
        """EX-3 / A8-3: cancel unfilled entries after 3 minutes or on a run-away."""
        out: list[tuple[str, str]] = []
        e = self.cfg.entries
        for t, leg, o in self.open_entry_orders():
            reason = None
            if t.style == "day":
                if now - t.submitted_at >= timedelta(minutes=e.cancel_unfilled_after_min):
                    reason = Reason.EXEC_CANCEL_TIMEOUT
                else:
                    last = last_prices.get(t.ticker)
                    runaway = t.trigger + Decimal(str(e.cancel_if_runs_atr5)) * t.atr5
                    if last is not None and Decimal(str(last)) >= runaway:
                        reason = Reason.EXEC_CANCEL_RUNAWAY
            elif swing_cutoff is not None and now >= swing_cutoff:
                reason = Reason.EXEC_CANCEL_WINDOW
            if reason is not None:
                await self._cancel(t, leg, o, reason, now)
                out.append((t.proposal_id, reason))
        return out

    async def cancel_all_entries(self, reason: str, now: datetime) -> int:
        """Kill switch (A10-3): cancel unfilled entries; broker-side stops stay."""
        n = 0
        for t, leg, o in self.open_entry_orders():
            await self._cancel(t, leg, o, reason, now)
            n += 1
        return n

    async def _cancel(self, t: EntryTicket, leg: str, o: Order, reason: str, now: datetime
                      ) -> None:
        try:
            await self.broker_for(t.route).cancel(o.id)
        except (BrokerUnavailable, BrokerRejected) as exc:
            log.error("cancel_failed", order=o.id, error=str(exc))
            return
        filled = t.filled.get(leg, 0)
        t.orders[leg] = o.model_copy(update={"qty": filled})   # rest cancelled (partial fill)
        if all(t.filled.get(k, 0) >= v.qty for k, v in t.orders.items()):
            t.done = True
        if self.journal is not None:
            self.journal.update(s.orders, {"broker_id": o.id}, {"status": f"canceled:{reason}"})

    # -- reconcile (A8-4, SEC-13) ------------------------------------------------------------
    async def reconcile(self, expected_qty: dict[str, int], known_ids: set[str] | None = None
                        ) -> list[str]:
        """Compare broker positions with ours; flag orders the system did not place (SEC-13).
        After a restart, orders are recognised from the journal and the restored legs."""
        problems: list[str] = []
        try:
            positions = await self.broker.positions()
            orders = await self.broker.open_orders()
        except BrokerUnavailable as exc:
            return [f"broker unavailable: {exc}"]
        have = {p.symbol: p.qty for p in positions}
        for sym in sorted(set(have) | set(expected_qty)):
            if have.get(sym, 0) != expected_qty.get(sym, 0):
                problems.append(f"position {sym}: broker {have.get(sym, 0)} vs journal "
                                f"{expected_qty.get(sym, 0)}")
        known = set(self.order_index) | (known_ids or set())
        pids = set(self.tickets)
        if self.journal is not None:
            for r in self.journal.rows(s.orders):
                if r["broker_id"]:
                    known.add(r["broker_id"])
                if r["proposal_id"]:
                    pids.add(r["proposal_id"])
        for o in orders:
            ours = (o.id in known or (o.parent_id is not None and o.parent_id in known)
                    or any(o.client_order_id.startswith(pid) for pid in pids))
            if not ours:
                problems.append(f"unknown order {o.id} {o.symbol} {o.side} {o.qty} "
                                f"(not placed by the system, SEC-13)")
        return problems


def marketable_exit_price(bid: float | None, last: float, offset_pct: float = 0.1) -> Decimal:
    """Marketable limit for exits (TW-2): a little under the bid/last."""
    ref = bid if bid else last
    return cents(ref * (1 - offset_pct / 100))
