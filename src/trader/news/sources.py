"""News sources for A3 (DATA_SPEC §1): Finnhub, SEC EDGAR, Reddit, and a recorded fake.

Each respects its free-tier budget and returns ``NewsItem``s. Text is stored raw but only
cleaned text ever reaches Qwen (DQ-4). Endpoints per provider docs; verify during the build.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

from trader.models.messages import NewsItem

log = structlog.get_logger(__name__)


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class FinnhubNews:
    """Company news, earnings calendar, profile (market cap). ~60 calls/min free."""

    name = "finnhub"
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token = token
        self.limiter = RateLimiter(50)
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(2):
            await self.limiter.wait()
            async with httpx.AsyncClient(base_url=self.BASE, timeout=15,
                                         transport=self._transport) as c:
                r = await c.get(path, params={**params, "token": self.token})
            if r.status_code == 429 and attempt == 0:   # rate limited: wait once, then retry
                await asyncio.sleep(30)
                continue
            r.raise_for_status()
            return r.json()
        return None

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]:
        out: list[NewsItem] = []
        frm, to = since.astimezone(UTC).date(), datetime.now(UTC).date()
        # a long range in one call comes back truncated: ask month by month (NC-30 backfill)
        windows, a = [], frm
        while a <= to:
            b = min(to, a + timedelta(days=29))
            windows.append((a, b))
            a = b + timedelta(days=1)
        for sym in symbols:
            rows: list[Any] = []
            for a, b in windows:
                rows += await self._get("/company-news", {"symbol": sym, "from": a.isoformat(),
                                                          "to": b.isoformat()}) or []
            seen: set[str] = set()
            for r in rows:
                if str(r.get("id")) in seen:
                    continue
                seen.add(str(r.get("id")))
                ts = datetime.fromtimestamp(int(r.get("datetime", 0)), UTC)
                if ts < since:
                    continue
                out.append(NewsItem(id=f"fh-{r.get('id')}", ticker=sym, source="finnhub",
                                    published_at=ts, headline=str(r.get("headline", ""))[:500],
                                    text=str(r.get("summary", ""))[:5000],
                                    url=str(r.get("url", "")), kind="news",
                                    meta={"publisher": str(r.get("source", ""))[:64]}))
        out += await self.earnings(symbols)
        return out

    async def earnings(self, symbols: Sequence[str]) -> list[NewsItem]:
        today = datetime.now(UTC).date()
        js = await self._get("/calendar/earnings", {"from": today.isoformat(),
                                                    "to": (today + timedelta(days=14)).isoformat()})
        out = []
        wanted = set(symbols)
        for r in (js or {}).get("earningsCalendar", []):
            if r.get("symbol") in wanted and r.get("date"):
                out.append(NewsItem(id=f"fh-earn-{r['symbol']}-{r['date']}", ticker=r["symbol"],
                                    source="finnhub", published_at=datetime.now(UTC),
                                    headline=f"Earnings {r['date']} {r.get('hour', '')}",
                                    kind="earnings", meta={"date": r["date"],
                                                           "hour": r.get("hour", "")}))
        return out

    async def market_caps(self, symbols: Sequence[str]) -> dict[str, float]:
        """A1-2: market cap in $ from profile2 (reported in millions). The same call's shares
        outstanding are kept in ``self.shares`` for the EDGAR cross-check."""
        out = {}
        self.shares: dict[str, float] = {}
        for sym in symbols:
            try:
                js = await self._get("/stock/profile2", {"symbol": sym})
                cap = js.get("marketCapitalization")
                if cap:
                    out[sym] = float(cap) * 1e6
                if js.get("shareOutstanding"):
                    self.shares[sym] = float(js["shareOutstanding"]) * 1e6
            except httpx.HTTPError as exc:
                log.warning("profile_failed", symbol=sym, error=str(exc))
        return out


class EdgarFilings:
    """Recent 8-K filings from data.sec.gov (fair access: User-Agent with email, <10 req/s)."""

    name = "edgar"

    def __init__(self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None
                 ) -> None:
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self.limiter = RateLimiter(300)
        self._transport = transport
        self._cik: dict[str, str] = {}

    async def _get(self, url: str) -> Any:
        await self.limiter.wait()
        async with httpx.AsyncClient(timeout=20, headers=self.headers,
                                     transport=self._transport) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()

    async def cik_map(self) -> dict[str, str]:
        if not self._cik:
            js = await self._get("https://www.sec.gov/files/company_tickers.json")
            self._cik = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in js.values()}
        return self._cik

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]:
        ciks = await self.cik_map()
        out = []
        for sym in symbols:
            cik = ciks.get(sym.replace(".", "-"))
            if cik is None:
                continue
            js = await self._get(f"https://data.sec.gov/submissions/CIK{cik}.json")
            recent = js.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            items = recent.get("items", []) or [""] * len(forms)
            for form, fdate, acc, doc, its in zip(forms, recent.get("filingDate", []),
                                                  recent.get("accessionNumber", []),
                                                  recent.get("primaryDocDescription", []),
                                                  items, strict=False):
                if form not in ("8-K", "6-K"):
                    continue
                ts = datetime.combine(date.fromisoformat(fdate), datetime.min.time(), UTC)
                if ts < since - timedelta(days=1):
                    break
                head = f"{form} filed: {doc or ''}" + (f" (items {its})" if its else "")
                out.append(NewsItem(id=f"sec-{acc}", ticker=sym, source="edgar",
                                    published_at=ts, headline=head[:300], kind="8-K",
                                    meta={"publisher": "SEC", "items": its or ""}))
        return out

    async def shares_outstanding(self, symbol: str) -> float | None:
        """A1-2 cross-check: the latest dei:EntityCommonStockSharesOutstanding (None when the
        company does not report it, e.g. foreign filers on 20-F)."""
        ciks = await self.cik_map()
        cik = ciks.get(symbol.replace(".", "-"))
        if cik is None:
            return None
        js = await self._get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        units = js.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding",
                                                       {}).get("units", {}).get("shares", [])
        return float(units[-1]["val"]) if units else None


class RedditPosts:
    """Posts mentioning the tickers in 4 finance subreddits (PRAW, <100 queries/min)."""

    name = "reddit"

    def __init__(self, client_id: str, client_secret: str, user_agent: str,
                 subreddits: Sequence[str]) -> None:
        import praw

        self.reddit = praw.Reddit(client_id=client_id, client_secret=client_secret,
                                  user_agent=user_agent, check_for_async=False)
        self.subs = "+".join(subreddits)
        self.limiter = RateLimiter(60)

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]:
        out: list[NewsItem] = []
        for sym in symbols:
            await self.limiter.wait()
            posts = await asyncio.to_thread(self._search, sym)
            for p in posts:
                ts = datetime.fromtimestamp(p["created_utc"], UTC)
                if ts >= since:
                    out.append(NewsItem(id=f"rd-{p['id']}", ticker=sym, source="reddit",
                                        published_at=ts, headline=p["title"][:300],
                                        text=p["selftext"][:2000], kind="post",
                                        meta={"comments": p["num_comments"]}))
        return out

    def _search(self, sym: str) -> list[dict[str, Any]]:
        res = self.reddit.subreddit(self.subs).search(f'"${sym}" OR "{sym}"', sort="new",
                                                      time_filter="day", limit=50)
        return [{"id": p.id, "title": p.title, "selftext": p.selftext or "",
                 "created_utc": p.created_utc, "num_comments": p.num_comments} for p in res]


class RecordedNews:
    """Fake NewsSource returning items by published_at (BT-2: never before their time)."""

    name = "recorded"

    def __init__(self, items: Sequence[NewsItem], now: Any = None) -> None:
        self.items = list(items)
        self.now = now

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]:
        now = self.now() if self.now else datetime.now(UTC)
        return [i for i in self.items if i.ticker in symbols and since <= i.published_at <= now]


def item_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:16]
