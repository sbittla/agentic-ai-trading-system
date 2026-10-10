"""Trades from the owner's other brokers for wash-sale locks (REQUIREMENTS WS-9).

The owner exports a CSV from Robinhood, Webull, Fidelity or Schwab (or writes the generic
format) and runs ``./run.sh import-trades FILE --broker NAME``. Rows are stored once
(``row_hash``), losses are found FIFO per ticker, and each loss creates the same 31-day lock a
loss here would. Buys there inside a lock window are reported. Imports never change the
Alpaca-side tax lots.

Generic format (header row, any column order): ``date,symbol,side,qty,price[,fees]``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.timeutil import ET

BROKERS = ("generic", "robinhood", "webull", "fidelity", "schwab")
# column names per broker (lower-case): date, symbol, side, qty, price, fees
PRESETS: dict[str, dict[str, tuple[str, ...]]] = {
    "generic": {"date": ("date",), "symbol": ("symbol",), "side": ("side",), "qty": ("qty",),
                "price": ("price",), "fees": ("fees",)},
    "robinhood": {"date": ("activity date",), "symbol": ("instrument",),
                  "side": ("trans code",), "qty": ("quantity",), "price": ("price",),
                  "fees": ()},
    "webull": {"date": ("filled time", "placed time"), "symbol": ("symbol",),
               "side": ("side",), "qty": ("filled", "total qty"), "price": ("avg price",
                                                                           "price"),
               "fees": ()},
    "fidelity": {"date": ("run date",), "symbol": ("symbol",), "side": ("action",),
                 "qty": ("quantity",), "price": ("price ($)", "price"),
                 "fees": ("commission ($)", "fees ($)")},
    "schwab": {"date": ("date",), "symbol": ("symbol",), "side": ("action",),
               "qty": ("quantity",), "price": ("price",), "fees": ("fees & comm",)},
}
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m/%d/%y")


@dataclass(frozen=True)
class ExtTrade:
    ts: datetime
    ticker: str
    side: str          # buy | sell
    qty: Decimal
    price: Decimal
    fees: Decimal = Decimal(0)

    def row_hash(self, broker: str) -> str:
        key = f"{broker}|{self.ts.isoformat()}|{self.ticker}|{self.side}|{self.qty}|{self.price}"
        return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class ExtLoss:
    ticker: str
    ts: datetime
    loss: Decimal


def _num(v: str | None) -> Decimal | None:
    if v is None:
        return None
    t = v.strip().replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _date(v: str) -> datetime | None:
    t = re.sub(r"\s+(EDT|EST|ET)$", "", v.strip())
    t = t.split(" as of ")[0]
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
        except ValueError:
            continue
        if dt.time() == time(0, 0) and len(t) <= 10:
            dt = dt.replace(hour=12)       # no time in the export: midday ET
        return dt.replace(tzinfo=ET).astimezone(UTC)
    return None


def _side(v: str) -> str | None:
    t = v.strip().lower()
    if t in ("buy", "b", "bot") or "bought" in t or t.startswith("buy"):
        return "buy"
    if t in ("sell", "s", "sld") or "sold" in t or t.startswith("sell"):
        return "sell"
    return None


def parse_csv(text: str, broker: str = "generic") -> tuple[list[ExtTrade], list[str]]:
    """Returns (trades, skipped-row notes). Non-trade rows (dividends, transfers, options,
    cancelled orders) are skipped and listed."""
    if broker not in PRESETS:
        raise ValueError(f"unknown broker {broker!r}; one of {', '.join(BROKERS)}")
    preset = PRESETS[broker]
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out: list[ExtTrade] = []
    skipped: list[str] = []

    def col(r: dict[str, Any], key: str) -> str | None:
        low = {(k or "").strip().lower(): v for k, v in r.items()}
        for name in preset[key]:
            if name in low and low[name] not in (None, ""):
                return str(low[name])
        return None

    for i, r in enumerate(rows, start=2):
        status = str({(k or "").lower(): v for k, v in r.items()}.get("status") or "")
        if broker == "webull" and status and status.lower() != "filled":
            skipped.append(f"line {i}: status {status}")
            continue
        d, sym, side = col(r, "date"), col(r, "symbol"), col(r, "side")
        qty, price = _num(col(r, "qty")), _num(col(r, "price"))
        fees = sum((_num(r.get(c)) or Decimal(0) for c in r
                    if (c or "").strip().lower() in preset["fees"]), Decimal(0))
        ts = _date(d) if d else None
        sd = _side(side) if side else None
        if not (ts and sym and sd and qty and price) or not re.fullmatch(
                r"[A-Z][A-Z.]{0,5}", sym.strip().upper()):
            skipped.append(f"line {i}: not a stock trade")
            continue
        out.append(ExtTrade(ts, sym.strip().upper(), sd, abs(qty), abs(price), abs(fees)))
    return sorted(out, key=lambda t: t.ts), skipped


def realized_losses(trades: list[ExtTrade]) -> list[ExtLoss]:
    """FIFO per ticker: every sell that closes shares below their cost is a loss."""
    lots: dict[str, deque[list[Decimal]]] = defaultdict(deque)   # [qty, cost/share]
    out: list[ExtLoss] = []
    for t in sorted(trades, key=lambda x: x.ts):
        if t.side == "buy":
            lots[t.ticker].append([t.qty, (t.price * t.qty + t.fees) / t.qty])
            continue
        left, pnl = t.qty, Decimal(0)
        proceeds = (t.price * t.qty - t.fees) / t.qty
        while left > 0 and lots[t.ticker]:
            lot = lots[t.ticker][0]
            n = min(left, lot[0])
            pnl += (proceeds - lot[1]) * n
            lot[0] -= n
            left -= n
            if lot[0] <= 0:
                lots[t.ticker].popleft()
        if pnl < 0:
            out.append(ExtLoss(t.ticker, t.ts, -pnl))
    return out


def store(journal: SqlJournal, trades: list[ExtTrade], broker: str, source: str,
          now: datetime) -> int:
    """Insert new rows (idempotent by row hash). Returns how many were new."""
    have = {r["row_hash"] for r in journal.rows(s.external_trades)}
    new = [t for t in trades if t.row_hash(broker) not in have]
    if new:
        journal.insert_many(s.external_trades, [{
            "imported_at": now, "broker": broker, "account": "", "ts": t.ts,
            "ticker": t.ticker, "side": t.side, "qty": float(t.qty), "price": t.price,
            "fees": t.fees, "source_file": Path(source).name, "row_hash": t.row_hash(broker)}
            for t in new])
    return len(new)


def load(journal: SqlJournal) -> list[ExtTrade]:
    return [ExtTrade(r["ts"], r["ticker"], r["side"], Decimal(str(r["qty"])),
                     Decimal(str(r["price"])), Decimal(str(r["fees"] or 0)))
            for r in journal.rows(s.external_trades)]


def apply_locks(compliance: Any, trades: list[ExtTrade], now: datetime, lock_days: int,
                canonical: Any = None) -> tuple[int, list[str]]:
    """Create WASH locks for other-broker losses within ``lock_days`` (not already locked)
    and list other-broker buys inside a lock window. Returns (locks created, warnings)."""
    canon = canonical or (lambda x: x)
    created = 0
    for ls in realized_losses(trades):
        until = ls.ts + timedelta(days=lock_days)
        if until <= now:
            continue
        g = canon(ls.ticker)
        exists = any(lk.ticker == g and lk.lock_type == "WASH" and abs(
            (lk.start - ls.ts).total_seconds()) < 60 for lk in compliance.locks)
        if not exists:
            compliance.add_lock(g, "WASH", ls.ts, until,
                                f"other-broker loss {ls.loss:.2f} (WS-9)")
            created += 1
    warnings = []
    for t in trades:
        if t.side != "buy":
            continue
        for lk in compliance.locks:
            if lk.ticker == canon(t.ticker) and lk.start <= t.ts < lk.until:
                warnings.append(f"{t.ticker}: bought {t.qty} at another broker on "
                                f"{t.ts.astimezone(ET):%Y-%m-%d} inside a lock window "
                                f"({lk.reason}) - possible wash sale")
                break
    return created, warnings
