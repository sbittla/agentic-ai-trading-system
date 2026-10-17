"""Free macro sources (MA-1, MA-2, MA-4): FRED public CSV files, the Federal Reserve press
RSS feed and Finnhub general news. Text is untrusted (DQ-4); feeds are read with regular
expressions, never an XML parser on untrusted input (as news/ratings.py)."""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)
UA = "agentic-ai-trading-system/0.1 (macro research)"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FED_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"
ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
FIELD_RE = {k: re.compile(rf"<{k}>(.*?)</{k}>", re.S) for k in ("title", "link", "pubDate",
                                                                "description")}
CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    """``observation_date,<ID>`` rows; '.' marks a missing value."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) != 2 or parts[1] in ("", "."):
            continue
        try:
            out.append((date.fromisoformat(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return out


def _field(block: str, key: str) -> str:
    m = FIELD_RE[key].search(block)
    if not m:
        return ""
    v = m.group(1)
    c = CDATA_RE.search(v)
    return html.unescape((c.group(1) if c else v).strip())


def parse_rss(text: str) -> list[dict[str, Any]]:
    out = []
    for block in ITEM_RE.findall(text[:2_000_000]):
        title = _field(block, "title")
        if not title:
            continue
        try:
            ts = parsedate_to_datetime(_field(block, "pubDate")).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        out.append({"title": title, "link": _field(block, "link"), "ts": ts,
                    "text": re.sub(r"<[^>]+>", " ", _field(block, "description"))})
    return out


def item_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:24]


class MacroFeeds:
    """``user_agent`` must name a contact: FRED stalls requests whose User-Agent has none
    (found October 16, 2026), so the SEC User-Agent from config is passed in."""

    def __init__(self, finnhub_token: str | None = None, user_agent: str = UA,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token = finnhub_token
        self.ua = user_agent
        self.transport = transport
        self.errors: dict[str, str] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=True,
                                 headers={"User-Agent": self.ua})

    async def fred(self, series: Sequence[str], start: date | None = None
                   ) -> dict[str, list[tuple[date, float]]]:
        """MA-1/MA-2: one CSV per series (``cosd`` limits the history when given)."""
        out: dict[str, list[tuple[date, float]]] = {}
        async with self._client() as c:
            for sid in series:
                params = {"id": sid, **({"cosd": start.isoformat()} if start else {})}
                try:
                    r = await c.get(FRED_CSV, params=params)
                    r.raise_for_status()
                    out[sid] = parse_fred_csv(r.text)
                except httpx.HTTPError as exc:
                    self.errors[f"fred:{sid}"] = (str(exc) or type(exc).__name__)[:160]
                    log.warning("fred_series_failed", series=sid, error=str(exc)[:160])
                await asyncio.sleep(0.3)
        return out

    async def fed_news(self) -> list[dict[str, Any]]:
        """MA-4: Federal Reserve press releases, statements, minutes and speeches."""
        async with self._client() as c:
            try:
                r = await c.get(FED_RSS)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                self.errors["fed_rss"] = str(exc)[:160]
                return []
        return [{"item_id": "fed-" + item_id(x["link"] or x["title"]), "source": "fed", **x}
                for x in parse_rss(r.text)]

    async def general_news(self, categories: Sequence[str] = ("general",)
                           ) -> list[dict[str, Any]]:
        """MA-4: Finnhub market and economy headlines."""
        if not self.token:
            return []
        out = []
        async with self._client() as c:
            for cat in categories:
                try:
                    r = await c.get("https://finnhub.io/api/v1/news",
                                    params={"category": cat, "token": self.token})
                    r.raise_for_status()
                    rows = r.json() or []
                except (httpx.HTTPError, ValueError) as exc:
                    self.errors[f"finnhub:{cat}"] = str(exc)[:160]
                    continue
                for x in rows:
                    ts = datetime.fromtimestamp(int(x.get("datetime") or 0), UTC)
                    out.append({"item_id": f"fhg-{x.get('id')}", "source": "finnhub",
                                "title": str(x.get("headline", ""))[:500],
                                "text": str(x.get("summary", ""))[:3000],
                                "link": str(x.get("url", "")), "ts": ts})
        return out
