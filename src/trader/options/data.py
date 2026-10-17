"""A13 Options Data agent (code, inside trader-core).

- A13-1 chains: listed contracts (with open interest) from Alpaca's trading API
  ``/v2/options/contracts``, 0-60 DTE and strikes within +-20% of the stock, loaded at 8:30
  and on demand.
- A13-2 snapshots: bid/ask/last/volume/IV/greeks from the data API
  ``/v1beta1/options/snapshots/{underlying}``, only when a signal or command needs them,
  cached 15 s. The chain is never streamed.
- A13-3 volatility context: ATM IV 30d, IV rank/percentile from ``options_iv_daily``,
  expected move per expiry (ATM straddle), term structure and 25-delta skew.
- A13-4 events per expiry (earnings, ex-dividend, FOMC/CPI).
- A13-5 quality: quote age, zero bids; the free plan's feed is "indicative".
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import structlog

from trader.journal import schema as s
from trader.options import pricing

log = structlog.get_logger(__name__)

DATA_URL = "https://data.alpaca.markets"
TRADING_URLS = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")
SNAPSHOT_TTL_S = 15
MAX_QUOTE_AGE_S = 60
IV_HISTORY_FULL = 252


def parse_occ(symbol: str) -> tuple[str, date, str, float] | None:
    """``NVDA261024C00130000`` -> ("NVDA", 2026-10-24, "C", 130.0)."""
    m = OCC.match(symbol)
    if not m:
        return None
    u, ymd, kind, k = m.groups()
    return u, date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])), kind, int(k) / 1000


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    underlying: str
    expiry: date
    kind: str                 # C | P
    strike: float
    bid: float = 0.0
    ask: float = 0.0
    last: float | None = None
    volume: int = 0
    open_interest: int = 0
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    quote_ts: datetime | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 100 if m > 0 else math.inf

    def age_s(self, now: datetime) -> float:
        return (now - self.quote_ts).total_seconds() if self.quote_ts else math.inf

    def usable(self, now: datetime, max_age_s: float = MAX_QUOTE_AGE_S) -> bool:
        """A13-5: a stale quote (> 60 s in the session) or a zero bid makes the contract
        unusable. After the close (next-day ideas) the last quote of the session is used."""
        return self.bid > 0 and self.ask >= self.bid and self.age_s(now) <= max_age_s

    def dte(self, today: date) -> int:
        return (self.expiry - today).days


def years_to(expiry: date, now: datetime) -> float:
    """Time to the 16:00 ET close of expiry day, in years."""
    from trader.timeutil import ET

    close = datetime(expiry.year, expiry.month, expiry.day, 16, tzinfo=ET)
    return max(0.0, (close - now).total_seconds() / (365 * 86400))


def fill_greeks(q: OptionQuote, spot: float, now: datetime) -> OptionQuote:
    """Missing IV/greeks (the indicative feed sometimes omits them) from the mid price."""
    if q.iv is not None and q.delta is not None:
        return q
    t = years_to(q.expiry, now)
    iv = q.iv if q.iv is not None else (pricing.implied_vol(q.kind, q.mid, spot, q.strike, t)
                                        if q.mid > 0 else None)
    if iv is None:
        return q
    g = pricing.greeks(q.kind, spot, q.strike, t, iv)
    return replace(q, iv=iv, delta=g.delta, gamma=g.gamma, theta=g.theta, vega=g.vega)


# --- A13-3 volatility context (pure) -----------------------------------------------------------

def expiries(quotes: Iterable[OptionQuote]) -> list[date]:
    return sorted({q.expiry for q in quotes})


def atm_pair(quotes: Iterable[OptionQuote], spot: float, expiry: date
             ) -> tuple[OptionQuote | None, OptionQuote | None]:
    qs = [q for q in quotes if q.expiry == expiry]
    if not qs:
        return None, None
    k = min({q.strike for q in qs}, key=lambda x: abs(x - spot))
    call = next((q for q in qs if q.strike == k and q.kind == "C"), None)
    put = next((q for q in qs if q.strike == k and q.kind == "P"), None)
    return call, put


def atm_iv(quotes: Iterable[OptionQuote], spot: float, expiry: date) -> float | None:
    ivs = [q.iv for q in atm_pair(list(quotes), spot, expiry) if q is not None and q.iv]
    return sum(ivs) / len(ivs) if ivs else None


def expected_move(quotes: Iterable[OptionQuote], spot: float, expiry: date) -> float | None:
    """1x expected move to expiry = ATM straddle mid."""
    c, p = atm_pair(list(quotes), spot, expiry)
    if c is None or p is None or c.mid <= 0 or p.mid <= 0:
        return None
    return c.mid + p.mid


def skew_25d(quotes: Iterable[OptionQuote], expiry: date) -> float | None:
    qs = [q for q in quotes if q.expiry == expiry and q.delta is not None and q.iv]
    puts = [q for q in qs if q.kind == "P"]
    calls = [q for q in qs if q.kind == "C"]
    if not puts or not calls:
        return None
    p = min(puts, key=lambda q: abs((q.delta or 0) + 0.25))
    c = min(calls, key=lambda q: abs((q.delta or 0) - 0.25))
    return (p.iv or 0) - (c.iv or 0)


def iv_rank(history: list[float], current: float) -> tuple[float | None, float | None, int]:
    """(IV rank 0-100, IV percentile 0-100, days of history) over up to 252 sessions."""
    h = [x for x in history[-IV_HISTORY_FULL:] if x and x > 0]
    if len(h) < 5:
        return None, None, len(h)
    lo, hi = min(h + [current]), max(h + [current])
    rank = (current - lo) / (hi - lo) * 100 if hi > lo else 50.0
    pct = sum(1 for x in h if x < current) / len(h) * 100
    return round(rank, 1), round(pct, 1), len(h)


@dataclass
class VolContext:
    ticker: str
    spot: float
    atm_iv_30d: float | None
    iv_rank: float | None
    iv_pct: float | None
    history_days: int
    expected_moves: dict[date, float] = field(default_factory=dict)
    term_front: float | None = None
    term_30d: float | None = None
    skew: float | None = None

    @property
    def history_label(self) -> str:
        return ("" if self.history_days >= IV_HISTORY_FULL
                else f"short history: {self.history_days} days")

    def text(self) -> str:
        def pct(x: float | None) -> str:
            return f"{x * 100:.1f}%" if x is not None else "n/a"
        rank = f"{self.iv_rank:.0f}" if self.iv_rank is not None else "n/a"
        ivp = f"{self.iv_pct:.0f}" if self.iv_pct is not None else "n/a"
        moves = ", ".join(f"{d:%b %d} +-{m:.2f}" for d, m in sorted(self.expected_moves.items())
                          [:4])
        lines = [f"IV {self.ticker} (stock {self.spot:.2f})",
                 f"ATM IV 30d {pct(self.atm_iv_30d)} · IV rank {rank} · IV pct {ivp}"
                 + (f" ({self.history_label})" if self.history_label else ""),
                 f"Term: front {pct(self.term_front)} vs 30d {pct(self.term_30d)}"
                 + (" (inverted)" if self.term_front and self.term_30d
                    and self.term_front > self.term_30d else ""),
                 "Skew 25d put-call: " + (f"{self.skew * 100:+.1f} vol pts"
                                          if self.skew is not None else "n/a"),
                 f"Expected move: {moves or 'n/a'}"]
        return "\n".join(lines)


def vol_context(ticker: str, quotes: list[OptionQuote], spot: float, today: date,
                history: list[float]) -> VolContext:
    exps = expiries(quotes)
    moves = {e: m for e in exps if (m := expected_move(quotes, spot, e)) is not None}
    near30 = min(exps, key=lambda e: abs((e - today).days - 30)) if exps else None
    front = next((e for e in exps if (e - today).days >= 1), exps[0] if exps else None)
    iv30 = atm_iv(quotes, spot, near30) if near30 else None
    rank, pct, n = iv_rank(history, iv30) if iv30 else (None, None, len(history))
    return VolContext(ticker, spot, iv30, rank, pct, n, moves,
                      atm_iv(quotes, spot, front) if front else None, iv30,
                      skew_25d(quotes, near30) if near30 else None)


def events_in(expiry: date, today: date, earnings: date | None, ex_div: date | None,
              macro: Iterable[tuple[date, str]] = ()) -> list[str]:
    """A13-4: events from today up to and including expiry."""
    out = []
    if earnings and today <= earnings <= expiry:
        out.append(f"earnings {earnings:%b %d}")
    if ex_div and today <= ex_div <= expiry:
        out.append(f"ex-dividend {ex_div:%b %d}")
    out += [f"{name} {d:%b %d}" for d, name in macro if today <= d <= expiry]
    return out


# --- HTTP client --------------------------------------------------------------------------------

class AlpacaOptionsClient:
    """Raw JSON over httpx (same style as ``trader.alerts.feed.AlpacaRest``)."""

    def __init__(self, key: str, secret: str, *, paper: bool = True, feed: str = "indicative",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.feed = feed
        self.trading_url = TRADING_URLS["paper" if paper else "live"]
        self._client = httpx.AsyncClient(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=20, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        r = await self._client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def contracts(self, underlying: str, exp_from: date, exp_to: date, k_lo: float,
                        k_hi: float) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "underlying_symbols": underlying, "status": "active",
            "expiration_date_gte": exp_from.isoformat(), "expiration_date_lte": exp_to.isoformat(),
            "strike_price_gte": f"{k_lo:.2f}", "strike_price_lte": f"{k_hi:.2f}", "limit": 10000}
        out: list[dict[str, Any]] = []
        for _ in range(10):
            data = await self._get(f"{self.trading_url}/v2/options/contracts", params)
            out += data.get("option_contracts") or []
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out

    async def snapshots(self, underlying: str, exp_from: date, exp_to: date, k_lo: float,
                        k_hi: float) -> dict[str, dict[str, Any]]:
        params: dict[str, Any] = {
            "feed": self.feed, "limit": 1000,
            "expiration_date_gte": exp_from.isoformat(), "expiration_date_lte": exp_to.isoformat(),
            "strike_price_gte": f"{k_lo:.2f}", "strike_price_lte": f"{k_hi:.2f}"}
        out: dict[str, dict[str, Any]] = {}
        for _ in range(10):
            data = await self._get(f"{DATA_URL}/v1beta1/options/snapshots/{underlying}", params)
            out.update(data.get("snapshots") or {})
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    from trader.alerts.feed import parse_ts

    return parse_ts(str(v))


def quote_from_snapshot(symbol: str, snap: dict[str, Any], oi: int = 0) -> OptionQuote | None:
    p = parse_occ(symbol)
    if p is None:
        return None
    lq = snap.get("latestQuote") or {}
    lt = snap.get("latestTrade") or {}
    g = snap.get("greeks") or {}
    return OptionQuote(symbol, p[0], p[1], p[2], p[3], float(lq.get("bp") or 0),
                       float(lq.get("ap") or 0), float(lt["p"]) if lt.get("p") else None,
                       int((snap.get("dailyBar") or {}).get("v") or 0), oi,
                       snap.get("impliedVolatility"), g.get("delta"), g.get("gamma"),
                       g.get("theta"), g.get("vega"), _ts(lq.get("t")))


# --- the agent -----------------------------------------------------------------------------------

class OptionsData:
    def __init__(self, client: Any, journal: Any, *, now: Callable[[], datetime],
                 max_dte: int = 60, strike_band: float = 0.20) -> None:
        self.client = client
        self.journal = journal
        self.now = now
        self.max_dte = max_dte
        self.band = strike_band
        self.oi: dict[str, dict[str, int]] = {}            # ticker -> occ symbol -> OI
        self.chain_day: dict[str, date] = {}
        self._snaps: dict[tuple[str, date, date, int, int], tuple[datetime,
                                                                   list[OptionQuote]]] = {}
        self.calls = 0

    async def load_chain(self, ticker: str, spot: float, today: date) -> int:
        """A13-1 (8:30 and on demand): contracts and open interest."""
        rows = await self.client.contracts(ticker, today, today + timedelta(days=self.max_dte),
                                           spot * (1 - self.band), spot * (1 + self.band))
        self.calls += 1
        self.oi[ticker] = {r["symbol"]: int(float(r.get("open_interest") or 0)) for r in rows
                           if r.get("symbol")}
        self.chain_day[ticker] = today
        return len(rows)

    async def quotes(self, ticker: str, spot: float, today: date, dte: tuple[int, int],
                     band: float | None = None) -> list[OptionQuote]:
        """A13-2: snapshots for one DTE range near the money, cached 15 s."""
        now = self.now()
        if self.chain_day.get(ticker) != today:
            try:
                await self.load_chain(ticker, spot, today)
            except Exception as exc:   # noqa: BLE001 - OI then reads 0 and fails OP-4
                log.warning("option_chain_failed", ticker=ticker, error=str(exc))
        b = self.band if band is None else band
        lo, hi = today + timedelta(days=dte[0]), today + timedelta(days=dte[1])
        key = (ticker, lo, hi, round(spot * (1 - b)), round(spot * (1 + b)))
        hit = self._snaps.get(key)
        if hit and (now - hit[0]).total_seconds() < SNAPSHOT_TTL_S:
            return hit[1]
        snaps = await self.client.snapshots(ticker, lo, hi, spot * (1 - b), spot * (1 + b))
        self.calls += 1
        oi = self.oi.get(ticker, {})
        out = [fill_greeks(q, spot, now) for sym, sn in snaps.items()
               if (q := quote_from_snapshot(sym, sn, oi.get(sym, 0))) is not None]
        self._snaps[key] = (now, out)
        return out

    def iv_history(self, ticker: str) -> list[float]:
        rows = sorted(self.journal.rows(s.options_iv_daily, {"ticker": ticker}),
                      key=lambda r: r["date"])
        return [float(r["atm_iv_30d"]) for r in rows if r.get("atm_iv_30d")]

    async def context(self, ticker: str, spot: float, today: date) -> tuple[VolContext,
                                                                           list[OptionQuote]]:
        qs = await self.quotes(ticker, spot, today, (0, 45), band=0.10)
        return vol_context(ticker, qs, spot, today, self.iv_history(ticker)), qs

    def store_daily(self, ctx: VolContext, d: date) -> None:
        """A13-6: one row per ticker per session (the IV-rank history)."""
        if ctx.atm_iv_30d is None:
            return
        if self.journal.rows(s.options_iv_daily, {"ticker": ctx.ticker, "date": d}):
            return
        near30 = next((m for e, m in sorted(ctx.expected_moves.items(),
                                            key=lambda kv: abs((kv[0] - d).days - 30))), None)
        self.journal.insert(s.options_iv_daily, {
            "date": d, "ticker": ctx.ticker, "atm_iv_30d": ctx.atm_iv_30d,
            "iv_rank": ctx.iv_rank, "iv_pct": ctx.iv_pct, "expected_move_30d": near30,
            "history_days": ctx.history_days + 1})
