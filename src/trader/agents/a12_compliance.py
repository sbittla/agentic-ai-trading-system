"""A12 Compliance guard (A12-1..A12-4, WS-1..WS-9, RC-4).

Before every entry: wash-sale locks, look-back for realized losses, year-end rule,
earnings and loss-streak locks, owner's list. Before every loss exit: the look-forward check.
Maintains tax lots with adjusted basis and disallowed losses, and produces the monthly
wash-sale report and the year-end export.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

import structlog

from trader.calendar import MarketCalendar
from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import ComplianceResult, EventFlags, LockCreated
from trader.timeutil import ET, et_date, et_datetime

log = structlog.get_logger(__name__)

LockType = Literal["WASH", "EARNINGS", "LOSS_STREAK", "REVENGE"]
WASH_WINDOW = timedelta(days=30)


@dataclass
class Lot:
    lot_id: str
    group: str                    # canonical ticker (GOOG -> GOOGL, WS-4)
    position_id: str
    acquired_at: datetime
    qty: int
    remaining: int
    cost_per_share: Decimal
    adjusted_basis: Decimal       # total basis incl. disallowed losses added (WS-7)
    disallowed_added: Decimal = Decimal(0)
    wash_link_lot_id: str | None = None
    route: str = "broker"
    disposed_at: datetime | None = None
    proceeds: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)


@dataclass
class LossSale:
    group: str
    lot_id: str
    ts: datetime
    qty: int
    loss: Decimal                 # positive number
    r_multiple: float
    washed_qty: int = 0
    disallowed: Decimal = Decimal(0)


@dataclass
class Lock:
    ticker: str
    lock_type: LockType
    start: datetime
    until: datetime
    reason: str


@dataclass
class ComplianceGuard:
    cfg: Config
    calendar: MarketCalendar
    journal: SqlJournal | None = None
    lots: list[Lot] = field(default_factory=list)
    loss_sales: list[LossSale] = field(default_factory=list)
    locks: list[Lock] = field(default_factory=list)
    news: dict[str, EventFlags] = field(default_factory=dict)
    ticker_results: dict[str, list[float]] = field(default_factory=dict)
    wash_sales: int = 0
    _lot_seq: int = 0

    # -- helpers ---------------------------------------------------------------------------
    def group(self, ticker: str) -> str:
        return self.cfg.canonical(ticker)

    def _year_end_active(self, now: datetime) -> bool:
        mm, dd = (int(x) for x in self.cfg.wash_sale.year_end_strict_from.split("-"))
        d = et_date(now)
        return d >= date(d.year, mm, dd)

    def _realized_losses(self, group: str, now: datetime, min_loss_r: float | None = None
                         ) -> list[LossSale]:
        out = []
        for ls in self.loss_sales:
            if ls.group == group and now - WASH_WINDOW <= ls.ts <= now:
                if min_loss_r is None or ls.r_multiple <= -min_loss_r:
                    out.append(ls)
        return out

    def add_lock(self, ticker: str, lock_type: LockType, start: datetime, until: datetime,
                 reason: str) -> LockCreated:
        lk = Lock(self.group(ticker), lock_type, start, until, reason)
        self.locks.append(lk)
        if self.journal is not None:
            self.journal.insert(s.locks, {"ticker": lk.ticker, "lock_type": lock_type,
                                          "start": start, "until": until, "reason": reason})
        return LockCreated(ticker=lk.ticker, lock_type=lock_type, start=start, until=until,
                           reason=reason)

    def active_locks(self, now: datetime, ticker: str | None = None) -> list[Lock]:
        g = self.group(ticker) if ticker else None
        return [lk for lk in self.locks if lk.start <= now < lk.until and (g is None
                                                                          or lk.ticker == g)]

    def open_lots(self, group: str | None = None) -> list[Lot]:
        return [lt for lt in self.lots if lt.remaining > 0 and (group is None
                                                                or lt.group == group)]

    # -- A12-1: before every entry -----------------------------------------------------------
    def check_entry(self, ticker: str, now: datetime, style: str, proposal_id: str = ""
                    ) -> ComplianceResult:
        found: list[str] = []
        t = ticker.upper()
        if t not in self.cfg.universe.tickers:   # UR-1, UR-2 (GOOG is never traded)
            return self._result(proposal_id, False, ["UNIVERSE"], "NT_UNIVERSE", now, t)
        g = self.group(t)
        policy = self.cfg.wash_sale.policy
        year_end = self._year_end_active(now)
        for lk in self.active_locks(now, g):
            found.append(f"{lk.lock_type}:{lk.until.astimezone(ET):%Y-%m-%d %H:%M}")
        # WS-2 look-back (policy-dependent); WS-5 year-end look-back always applies (WS-8)
        if policy == "strict" or (policy == "year_end_only" and year_end):
            losses = self._realized_losses(g, now)
        elif policy == "balanced":
            losses = self._realized_losses(g, now, self.cfg.wash_sale.balanced_min_loss_r)
        else:
            losses = []
        if year_end:
            losses = losses or self._realized_losses(g, now)
        if losses:
            found.append(f"WASH_LOOKBACK:{len(losses)} loss(es) in 30 days")
        # strict: a second lot while one is open could wash on a later loss exit (WS-3)
        if policy == "strict" and self.open_lots(g):
            found.append("WASH_OPEN_LOT")
        # earnings locks
        nf = self.news.get(g)
        if nf is not None:
            if "EARNINGS_TODAY" in nf.flags:
                found.append("EARNINGS_TODAY")
            if style == "swing" and "EARNINGS_3D" in nf.flags:
                found.append("EARNINGS_3D")
        ok = not found
        code = None
        if not ok:
            code = ("NT_WASH_LOCK" if any(f.startswith("WASH") for f in found) else
                    "NT_EARNINGS_LOCK" if any(f.startswith("EARNINGS") for f in found) else
                    "NT_LOSS_STREAK_LOCK" if any(f.startswith("LOSS_STREAK") for f in found)
                    else "NT_DR3")
        return self._result(proposal_id, ok, found, code, now, t)

    def _result(self, proposal_id: str, ok: bool, found: list[str], code: str | None,
                now: datetime, ticker: str) -> ComplianceResult:
        res = ComplianceResult(proposal_id=proposal_id, ok=ok, locks_found=tuple(found),
                               reason=code)
        if self.journal is not None and proposal_id:
            self.journal.insert(s.compliance_checks, {
                "proposal_id": proposal_id, "ts": now, "kind": "entry",
                "result": "OK" if ok else "BLOCKED", "locks_found": list(found),
                "detail": f"{ticker} {code or ''}"})
        return res

    # -- tax lots (A12-3, WS-7) --------------------------------------------------------------
    def on_buy_fill(self, position_id: str, ticker: str, qty: int, price: Decimal,
                    ts: datetime, route: str = "broker") -> Lot:
        g = self.group(ticker)
        self._lot_seq += 1
        lot = Lot(lot_id=f"{position_id}-L{self._lot_seq}", group=g, position_id=position_id,
                  acquired_at=ts, qty=qty, remaining=qty, cost_per_share=price,
                  adjusted_basis=price * qty, route=route)
        # replacement purchase within 30 days after a loss sale: disallow and add to basis
        for ls in self.loss_sales:
            if ls.group != g or not (ls.ts <= ts <= ls.ts + WASH_WINDOW):
                continue
            free = ls.qty - ls.washed_qty
            if free <= 0:
                continue
            n = min(free, lot.remaining)
            self._wash(ls, lot, n)
        self.lots.append(lot)
        self._persist_lot(lot)
        return lot

    def _wash(self, ls: LossSale, replacement: Lot, n: int) -> None:
        part = (ls.loss * n / ls.qty).quantize(Decimal("0.01"))
        ls.washed_qty += n
        ls.disallowed += part
        replacement.adjusted_basis += part
        replacement.disallowed_added += part
        replacement.wash_link_lot_id = ls.lot_id
        self.wash_sales += 1
        log.warning("wash_sale", group=ls.group, lot=ls.lot_id, replacement=replacement.lot_id,
                    disallowed=str(part))
        self._persist_lot(replacement)
        if self.journal is not None:
            self.journal.update(s.tax_lots, {"lot_id": ls.lot_id},
                                {"disallowed_loss": ls.disallowed,
                                 "wash_link_lot_id": replacement.lot_id})

    def check_loss_exit(self, position_id: str, ticker: str, now: datetime) -> tuple[bool, str]:
        """A12-2 / WS-3: would closing this position at a loss create a wash sale?"""
        g = self.group(ticker)
        others = [lt for lt in self.lots if lt.group == g and lt.position_id != position_id
                  and lt.remaining > 0 and now - WASH_WINDOW <= lt.acquired_at <= now]
        detail = ("no replacement lots" if not others else
                  f"WASH EXPECTED: {sum(o.remaining for o in others)} sh bought within 30 days "
                  f"({', '.join(o.lot_id for o in others)})")
        if self.journal is not None:
            self.journal.insert(s.compliance_checks, {
                "proposal_id": position_id, "ts": now, "kind": "loss_exit",
                "result": "WASH_EXPECTED" if others else "OK", "locks_found": [],
                "detail": detail})
        return bool(others), detail

    def on_sell_fill(self, position_id: str, ticker: str, qty: int, price: Decimal,
                     ts: datetime, r_multiple: float = 0.0) -> Decimal:
        """Dispose lots of this position FIFO. Returns realized P&L (before wash adjustment)."""
        g = self.group(ticker)
        remaining = qty
        realized = Decimal(0)
        for lot in [lt for lt in self.lots if lt.position_id == position_id and lt.remaining]:
            if remaining <= 0:
                break
            n = min(lot.remaining, remaining)
            basis_per = lot.adjusted_basis / lot.qty
            pnl = (price - basis_per) * n
            lot.remaining -= n
            lot.proceeds += price * n
            lot.realized += pnl
            remaining -= n
            realized += pnl
            if lot.remaining == 0:
                lot.disposed_at = ts
            if pnl < 0:
                ls = LossSale(group=g, lot_id=lot.lot_id, ts=ts, qty=n, loss=-pnl,
                              r_multiple=r_multiple)
                self.loss_sales.append(ls)
                # replacement shares bought within 30 days before and still held (WS-3)
                for other in self.lots:
                    if (other.group == g and other.position_id != position_id
                            and other.remaining > 0 and ts - WASH_WINDOW <= other.acquired_at
                            <= ts and ls.washed_qty < ls.qty):
                        self._wash(ls, other, min(ls.qty - ls.washed_qty, other.remaining))
            self._persist_lot(lot)
        return realized

    def _persist_lot(self, lot: Lot) -> None:
        if self.journal is None:
            return
        self.journal.upsert(s.tax_lots, {"lot_id": lot.lot_id}, {
            "ticker": lot.group, "position_id": lot.position_id,
            "acquired_at": lot.acquired_at, "qty": lot.qty, "remaining_qty": lot.remaining,
            "cost": lot.cost_per_share * lot.qty, "adjusted_basis": lot.adjusted_basis,
            "disposed_at": lot.disposed_at, "proceeds": lot.proceeds,
            "realized_pnl": lot.realized, "disallowed_loss": lot.disallowed_added,
            "wash_link_lot_id": lot.wash_link_lot_id, "route": lot.route})

    # -- after a position closes: locks (WS-1, RC-4) -----------------------------------------
    def on_position_closed(self, ticker: str, pnl: Decimal, r_multiple: float, now: datetime
                           ) -> list[LockCreated]:
        g = self.group(ticker)
        out: list[LockCreated] = []
        res = self.ticker_results.setdefault(g, [])
        res.append(float(pnl))
        if pnl < 0:
            policy = self.cfg.wash_sale.policy
            lock = (policy == "strict"
                    or (policy == "balanced" and r_multiple <= -self.cfg.wash_sale.
                        balanced_min_loss_r)
                    or self._year_end_active(now))
            if lock:
                until = now + timedelta(days=self.cfg.wash_sale.lock_days)
                out.append(self.add_lock(g, "WASH", now, until,
                                         f"loss {pnl} ({r_multiple:+.2f}R), WS-1 {policy}"))
            n = self.cfg.risk.ticker_loss_streak_lock.losses
            if len(res) >= n and all(x < 0 for x in res[-n:]):
                d = self.calendar.add_trading_days(et_date(now),
                                                   self.cfg.risk.ticker_loss_streak_lock.days)
                out.append(self.add_lock(g, "LOSS_STREAK", now, et_datetime(d, time(0, 0)),
                                         f"{n} losing trades in a row (RC-4)"))
        return out

    # -- WS-5 second half: no deferred loss carried into the new year --------------------------
    def year_end_must_close(self, now: datetime) -> list[str]:
        if not self._year_end_active(now):
            return []
        return sorted({lt.group for lt in self.open_lots() if lt.disallowed_added > 0})

    # -- A12-4 / WS-6 ------------------------------------------------------------------------
    def locked_list(self, now: datetime) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for lk in self.active_locks(now):
            cur = out.get(lk.ticker)
            if cur is None or lk.until > cur["until"]:
                out[lk.ticker] = {"ticker": lk.ticker, "type": lk.lock_type,
                                  "until": lk.until, "reason": lk.reason}
        for t, nf in self.news.items():
            if "EARNINGS_TODAY" in nf.flags and t not in out:
                out[t] = {"ticker": t, "type": "EARNINGS", "until": now, "reason": "earnings"}
        return sorted(out.values(), key=lambda x: x["ticker"])

    def ira_warning(self, now: datetime) -> str:
        wash = [x for x in self.locked_list(now) if x["type"] == "WASH"]
        if not wash:
            return ""
        names = ", ".join(f"{x['ticker']} until {x['until'].astimezone(ET):%b %d}"
                          for x in wash)
        return (f"Wash-sale locks: {names}. Do not buy these in other accounts, especially an "
                "IRA (WS-6, Rev. Rul. 2008-5).")

    # -- reports (A12-3) ---------------------------------------------------------------------
    def monthly_report(self, year: int, month: int) -> dict[str, Any]:
        def in_month(ts: datetime | None) -> bool:
            if ts is None:
                return False
            d = et_date(ts)
            return d.year == year and d.month == month
        disposed = [lt for lt in self.lots if in_month(lt.disposed_at)]
        sales = [ls for ls in self.loss_sales if in_month(ls.ts)]
        return {
            "year": year, "month": month,
            "lots_disposed": len(disposed),
            "realized_pnl": str(sum((lt.realized for lt in disposed), Decimal(0))),
            "loss_sales": len(sales),
            "wash_sales": sum(1 for ls in sales if ls.washed_qty > 0),
            "disallowed_total": str(sum((ls.disallowed for ls in sales), Decimal(0))),
            "open_lots_with_deferred_loss": [lt.lot_id for lt in self.open_lots()
                                             if lt.disallowed_added > 0],
        }

    def year_end_export(self, year: int) -> str:
        """CSV for tax software (Form 8949-style columns)."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["description", "date_acquired", "date_sold", "proceeds", "cost_basis",
                    "wash_sale_loss_disallowed", "gain_or_loss", "route"])
        for lt in self.lots:
            if lt.disposed_at is None or et_date(lt.disposed_at).year != year:
                continue
            ls = [x for x in self.loss_sales if x.lot_id == lt.lot_id]
            disallowed = sum((x.disallowed for x in ls), Decimal(0))
            w.writerow([f"{lt.qty} sh {lt.group}", f"{et_date(lt.acquired_at)}",
                        f"{et_date(lt.disposed_at)}", f"{lt.proceeds:.2f}",
                        f"{lt.adjusted_basis:.2f}", f"{disallowed:.2f}",
                        f"{lt.realized + disallowed:.2f}", lt.route])
        return buf.getvalue()

    def reconcile_lots(self, broker_qty: dict[str, int]) -> list[str]:
        """WS-7: open lots must match the broker's positions (route=broker only)."""
        ours: dict[str, int] = {}
        for lt in self.open_lots():
            if lt.route == "broker":
                ours[lt.group] = ours.get(lt.group, 0) + lt.remaining
        problems = []
        for sym in sorted(set(ours) | {self.group(k) for k in broker_qty}):
            b = sum(q for k, q in broker_qty.items() if self.group(k) == sym)
            if ours.get(sym, 0) != b:
                problems.append(f"{sym}: lots {ours.get(sym, 0)} vs broker {b}")
        return problems

    # -- restart -----------------------------------------------------------------------------
    def load_from_journal(self, now: datetime) -> None:
        if self.journal is None:
            return
        self.locks = [Lock(r["ticker"], r["lock_type"], r["start"], r["until"], r["reason"] or "")
                      for r in self.journal.rows(s.locks)]
        self.lots = []
        for r in self.journal.rows(s.tax_lots):
            self.lots.append(Lot(
                lot_id=r["lot_id"], group=r["ticker"], position_id=r["position_id"] or "",
                acquired_at=r["acquired_at"], qty=r["qty"], remaining=r["remaining_qty"],
                cost_per_share=(r["cost"] or Decimal(0)) / max(r["qty"], 1),
                adjusted_basis=r["adjusted_basis"] or Decimal(0),
                disallowed_added=Decimal(0), wash_link_lot_id=r["wash_link_lot_id"],
                route=r["route"] or "broker", disposed_at=r["disposed_at"],
                proceeds=r["proceeds"] or Decimal(0), realized=r["realized_pnl"] or Decimal(0)))
            if r["realized_pnl"] is not None and r["realized_pnl"] < 0 and r["disposed_at"]:
                self.loss_sales.append(LossSale(group=r["ticker"], lot_id=r["lot_id"],
                                                ts=r["disposed_at"], qty=r["qty"],
                                                loss=-r["realized_pnl"], r_multiple=-1.0))
        self._lot_seq = len(self.lots)
        for r in self.journal.closed_positions():
            self.ticker_results.setdefault(self.group(r["ticker"]), []).append(
                float(r["pnl"] or 0))

