"""Catalyst data collected in news-sentiment (NC-1, NC-11, NC-16; DATA_SPEC §1).

- ``Enricher``: at ingest, a sentence embedding per news item and a social-model score per
  post (both CPU, loaded once).
- ``FundamentalsFeed``: earnings surprises, analyst consensus and the next report date
  (Finnhub), insider open-market trades (SEC Form 4) and daily short-sale volume (FINRA
  Reg SHO), stored in ``fundamentals_daily``.
- ``StocktwitsPosts`` and ``ApeWisdom``: optional social sources, off until their terms are
  verified (``catalyst.social_sources``).

Every source is free and read with regular expressions or JSON, never an XML parser on
untrusted input (as ratings.py). A failing source is logged once and skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

from trader.catalyst.social import author_hash
from trader.catalyst.text import Embedder, Scorer, encode_vec
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import NewsItem

log = structlog.get_logger(__name__)
UA = "agentic-ai-trading-system/0.1 (research)"


class Enricher:
    """NC-1 / NC-11 at ingest time (called by A3 with the cleaned text)."""

    def __init__(self, journal: SqlJournal | None, embedder: Embedder | None,
                 social: Scorer | None, social_name: str = "") -> None:
        self.journal = journal
        self.embedder = embedder
        self.social = social
        self.social_name = social_name

    def __call__(self, fresh: Sequence[tuple[NewsItem, str]]) -> None:
        if self.journal is None or not fresh:
            return
        news = [(it, c) for it, c in fresh if it.kind != "post"]
        posts = [(it, c) for it, c in fresh if it.kind == "post"]
        if self.embedder is not None and news:
            vecs = self.embedder.embed([f"{it.headline}. {c[:300]}" for it, c in news])
            rows: list[dict[str, Any]] = [
                {"item_id": it.id, "ticker": it.ticker, "model": self.embedder.name,
                     "vec": encode_vec(v)} for (it, _), v in zip(news, vecs, strict=True)]
            _insert_new(self.journal, s.news_embeddings, rows)
        if self.social is not None and posts:
            scores = self.social.score([c for _, c in posts])
            rows = [
                {"item_id": it.id, "ticker": it.ticker, "ts": it.published_at,
                     "source": it.source, "author": author_hash(str(it.meta.get("author") or
                                                                    it.id)),
                     "score": sc, "tag": it.meta.get("tag"), "length": len(c),
                     "text_hash": text_hash(c),
                     "model": self.social_name[:64]}
                    for (it, c), sc in zip(posts, scores, strict=True)]
            _insert_new(self.journal, s.social_posts, rows)


def _insert_new(journal: SqlJournal, table: Any, rows: list[dict[str, Any]]) -> None:
    """Insert rows whose ``item_id`` is not stored yet (re-ingest after a restart is a no-op)."""
    from sqlalchemy import select

    ids = [r["item_id"] for r in rows]
    have = {r["item_id"] for r in journal.query(select(table.c.item_id).where(
        table.c.item_id.in_(ids)))} if ids else set()
    new = [r for r in rows if r["item_id"] not in have]
    if new:
        journal.insert_many(table, new)


async def embed_backlog(journal: SqlJournal, embedder: Embedder, since: datetime,
                        batch: int = 256) -> int:
    """Embeddings for stored items that have none (after a backfill or a model change)."""
    from sqlalchemy import and_, select

    q = (select(s.news_items.c.item_id, s.news_items.c.ticker, s.news_items.c.headline,
                s.news_items.c.cleaned_text)
         .outerjoin(s.news_embeddings, s.news_embeddings.c.item_id == s.news_items.c.item_id)
         .where(and_(s.news_embeddings.c.item_id.is_(None),
                     s.news_items.c.published_at >= since,
                     s.news_items.c.source != "reddit")))
    rows = journal.query(q)
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vecs = await asyncio.to_thread(embedder.embed, [
            f"{r['headline'] or ''}. {(r['cleaned_text'] or '')[:300]}" for r in chunk])
        _insert_new(journal, s.news_embeddings, [
            {"item_id": r["item_id"], "ticker": r["ticker"], "model": embedder.name,
             "vec": encode_vec(v)} for r, v in zip(chunk, vecs, strict=True)])
        done += len(chunk)
    return done


# --- fundamentals --------------------------------------------------------------------------

FORM4_TX = re.compile(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", re.S)
TAG = {k: re.compile(rf"<{k}>\s*(?:<value>)?\s*([^<\s]+)", re.S)
       for k in ("transactionCode", "transactionShares", "transactionPricePerShare",
                 "transactionAcquiredDisposedCode")}


def parse_form4(xml: str) -> list[dict[str, Any]]:
    """Open-market purchases (P) and sales (S) from a Form 4 XML document."""
    out = []
    for block in FORM4_TX.findall(xml[:500_000]):
        vals = {k: (m.group(1) if (m := rx.search(block)) else None) for k, rx in TAG.items()}
        code = vals["transactionCode"]
        if code not in ("P", "S"):
            continue
        try:
            shares = float(vals["transactionShares"] or 0)
            price = float(vals["transactionPricePerShare"] or 0)
        except ValueError:
            continue
        out.append({"code": code, "shares": shares, "value": shares * price})
    return out


def summarize_insiders(txs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    buys = [t for t in txs if t["code"] == "P"]
    sells = [t for t in txs if t["code"] == "S"]
    return {"buys": len(buys), "sells": len(sells),
            "net_value": round(sum(t["value"] for t in buys) - sum(t["value"] for t in sells),
                               2)}


def parse_regsho(text: str, tickers: Sequence[str]) -> dict[str, tuple[float, float]]:
    """FINRA Reg SHO daily file: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market.
    Returns {symbol: (short volume, total volume)}."""
    want = set(tickers)
    out: dict[str, tuple[float, float]] = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) >= 5 and parts[1] in want:
            try:
                out[parts[1]] = (float(parts[2]), float(parts[4]))
            except ValueError:
                continue
    return out


class FundamentalsFeed:
    """NC-16 inputs, collected once a day (07:30 ET) and by ``backfill-catalysts``."""

    def __init__(self, journal: SqlJournal, finnhub_token: str | None, sec_user_agent: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.journal = journal
        self.token = finnhub_token
        self.ua = sec_user_agent
        self.transport = transport
        self.off: set[str] = set()          # endpoints refused on this plan (logged once)
        self.errors: dict[str, str] = {}

    async def _get(self, c: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None,
                   name: str = "") -> httpx.Response | None:
        if name in self.off:
            return None
        try:
            r = await c.get(url, params=params)
        except httpx.HTTPError as exc:
            self.errors[name] = str(exc)[:200]
            return None
        if r.status_code in (401, 403):
            self.off.add(name)
            log.warning("catalyst_source_not_on_plan", source=name, status=r.status_code)
            return None
        if r.status_code != 200:
            self.errors[name] = f"HTTP {r.status_code}"
            return None
        return r

    def _store(self, d: date, ticker: str, kind: str, data: Any) -> None:
        self.journal.upsert(s.fundamentals_daily, {"date": d, "ticker": ticker, "kind": kind},
                            {"json": data})

    async def finnhub(self, tickers: Sequence[str], d: date) -> int:
        if not self.token:
            return 0
        n = 0
        base = "https://finnhub.io/api/v1"
        async with httpx.AsyncClient(timeout=20, transport=self.transport,
                                     headers={"X-Finnhub-Token": self.token}) as c:
            for t in tickers:
                for kind, path in (("earnings", "/stock/earnings"),
                                   ("recommendation", "/stock/recommendation")):
                    r = await self._get(c, base + path, {"symbol": t}, f"finnhub:{kind}")
                    if r is not None and isinstance(r.json(), list):
                        self._store(d, t, kind, r.json()[:8])
                        n += 1
                    await asyncio.sleep(1.1)         # free tier: under 60 calls a minute
            r = await self._get(c, base + "/calendar/earnings",
                                {"from": d.isoformat(),
                                 "to": (d + timedelta(days=60)).isoformat()},
                                "finnhub:calendar")
            if r is not None:
                rows = (r.json() or {}).get("earningsCalendar") or []
                for t in tickers:
                    nxt = sorted(x["date"] for x in rows if x.get("symbol") == t and x.get("date"))
                    if nxt:
                        self._store(d, t, "earnings_calendar", {"date": nxt[0]})
                        n += 1
        return n

    async def insiders(self, tickers: Sequence[str], d: date, ciks: dict[str, str],
                       days: int = 90, max_filings: int = 25) -> int:
        """SEC Form 4: open-market buys and sells by insiders over ``days``."""
        n = 0
        async with httpx.AsyncClient(timeout=20, transport=self.transport,
                                     headers={"User-Agent": self.ua}) as c:
            for t in tickers:
                cik = ciks.get(t.replace(".", "-"))
                if cik is None:
                    continue
                r = await self._get(c, f"https://data.sec.gov/submissions/CIK{cik}.json",
                                    name="sec:submissions")
                if r is None:
                    continue
                rec = r.json().get("filings", {}).get("recent", {})
                txs: list[dict[str, Any]] = []
                seen = 0
                for form, fdate, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []),
                                                 rec.get("accessionNumber", []),
                                                 rec.get("primaryDocument", []), strict=False):
                    if form != "4":
                        continue
                    if date.fromisoformat(fdate) < d - timedelta(days=days):
                        break
                    seen += 1
                    if seen > max_filings:
                        break
                    xml_name = doc.rsplit("/", 1)[-1]
                    url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                           f"{acc.replace('-', '')}/{xml_name}")
                    x = await self._get(c, url, name="sec:form4")
                    await asyncio.sleep(0.15)        # SEC fair access: < 10 requests a second
                    if x is not None:
                        txs += parse_form4(x.text)
                self._store(d, t, "insider", summarize_insiders(txs))
                n += 1
        return n

    async def short_volume(self, tickers: Sequence[str], d: date, sessions: int = 20) -> int:
        """FINRA Reg SHO consolidated daily short-sale volume files (public, no key)."""
        ratios: dict[str, list[float]] = {t: [] for t in tickers}
        day, tried = d, 0
        async with httpx.AsyncClient(timeout=20, transport=self.transport,
                                     headers={"User-Agent": UA}) as c:
            while tried < sessions + 12 and min((len(v) for v in ratios.values()),
                                                default=sessions) < sessions:
                tried += 1
                if day.weekday() < 5:
                    url = ("https://cdn.finra.org/equity/regsho/daily/"
                           f"CNMSshvol{day.strftime('%Y%m%d')}.txt")
                    r = await self._get(c, url, name="finra:regsho")
                    if r is not None:
                        for sym, (sv, tv) in parse_regsho(r.text, tickers).items():
                            if tv > 0:
                                ratios[sym].append(sv / tv)
                day -= timedelta(days=1)
        n = 0
        for t, rs in ratios.items():
            if rs:
                self._store(d, t, "short_volume", {
                    "ratio_5d": round(sum(rs[:5]) / len(rs[:5]), 4),
                    "ratio_20d": round(sum(rs) / len(rs), 4), "sessions": len(rs)})
                n += 1
        return n

    async def apewisdom(self, tickers: Sequence[str], d: date) -> int:
        """Reddit mention counts per ticker (volume only, no text). Off by default."""
        want = set(tickers)
        n = 0
        async with httpx.AsyncClient(timeout=20, transport=self.transport,
                                     headers={"User-Agent": UA}) as c:
            for page in range(1, 4):
                r = await self._get(c, "https://apewisdom.io/api/v1.0/filter/all-stocks/page/"
                                    f"{page}", name="apewisdom")
                if r is None:
                    break
                for row in (r.json() or {}).get("results", []):
                    if row.get("ticker") in want:
                        self._store(d, row["ticker"], "apewisdom", {
                            "mentions": int(row.get("mentions") or 0),
                            "mentions_24h_ago": int(row.get("mentions_24h_ago") or 0),
                            "rank": row.get("rank")})
                        n += 1
        return n


class StocktwitsPosts:
    """Public symbol stream (messages with optional user Bullish/Bearish tags). Off until its
    terms are verified (``catalyst.social_sources``)."""

    name = "stocktwits"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]:
        out: list[NewsItem] = []
        async with httpx.AsyncClient(timeout=20, transport=self.transport,
                                     headers={"User-Agent": UA}) as c:
            for sym in symbols:
                try:
                    r = await c.get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                for m in (r.json() or {}).get("messages", []):
                    try:
                        ts = datetime.fromisoformat(str(m["created_at"]).replace("Z", "+00:00"))
                    except (KeyError, ValueError):
                        continue
                    if ts < since:
                        continue
                    tag = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                    body = str(m.get("body", ""))[:1000]
                    out.append(NewsItem(
                        id=f"stw-{m.get('id')}", ticker=sym, source="stocktwits",
                        published_at=ts.astimezone(UTC), headline=body[:300], text=body,
                        kind="post",
                        meta={"author": str((m.get("user") or {}).get("username", "")),
                              "tag": tag.lower() if isinstance(tag, str) else None,
                              "publisher": "stocktwits"}))
                await asyncio.sleep(0.5)
        return out


def text_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text.lower()).strip().encode(),
                        usedforsecurity=False).hexdigest()[:16]


async def backfill_news(journal: SqlJournal, sources: Sequence[Any], tickers: Sequence[str],
                        days: int, scorer: Scorer, enricher: Enricher | None = None,
                        now: datetime | None = None) -> dict[str, int]:
    """NC-30: news history for the event study, for every ticker in the history universe
    (A3's own backfill covers only the traded universe). Same cleaning and scoring as A3."""
    from sqlalchemy import select

    from trader.agents.a03_news import clean_text

    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    counts: dict[str, int] = {}
    for src in sources:
        try:
            got = await src.fetch(list(tickers), since)
        except Exception as exc:   # noqa: BLE001 - report and continue with the rest
            log.warning("catalyst_backfill_source_failed", source=src.name, error=str(exc))
            counts[f"{src.name}_failed"] = 1
            continue
        got = [it for it in got if it.kind != "earnings"]
        ids = [it.id for it in got]
        have: set[str] = set()
        for i in range(0, len(ids), 2000):
            have |= {r["item_id"] for r in journal.query(select(s.news_items.c.item_id).where(
                s.news_items.c.item_id.in_(ids[i:i + 2000])))}
        fresh = []
        for it in got:
            if it.id in have:
                continue
            have.add(it.id)
            cleaned, sus = clean_text(f"{it.headline}. {it.text}")
            fresh.append((it, cleaned, sus))
        for i in range(0, len(fresh), 500):
            chunk = fresh[i:i + 500]
            scores = await asyncio.to_thread(scorer.score, [c for _, c, _ in chunk])
            journal.insert_many(s.news_items, [
                {"item_id": it.id, "ticker": it.ticker, "source": it.source,
                 "published_at": it.published_at, "received_at": now,
                 "headline": clean_text(it.headline)[0][:500], "cleaned_text": c,
                 "raw_text": (it.text or "")[:20000], "finbert_score": sc, "suspicious": sus,
                 "kind": it.kind, "publisher": str(it.meta.get("publisher") or it.source)[:64]}
                for (it, c, sus), sc in zip(chunk, scores, strict=True)])
            if enricher is not None:
                await asyncio.to_thread(enricher, [(it, c) for it, c, _ in chunk])
        counts[src.name] = len(fresh)
    return counts
