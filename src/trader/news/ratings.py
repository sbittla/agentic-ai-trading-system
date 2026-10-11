"""Analyst upgrades, downgrades and price-target changes (UI-33, A3-3 RATING_CHANGE).

Sources, all free and meant for programmatic use (no HTML scraping of web pages):

- Finnhub ``/stock/upgrade-downgrade``: structured rows. Some Finnhub plans do not include
  it; a 401/403 turns it off for the rest of the run (logged once).
- RSS headlines per ticker: Yahoo Finance's headline feed and a Google News search feed
  (``"<TICKER>" upgrade OR downgrade OR "price target"``). Feeds are read with a regular
  expression over ``<item>`` blocks (no XML parser on untrusted input) and every headline
  goes through ``parse_headline``; headlines that are not a rating action are dropped.

``RatingsFeed`` is also an A3 ``NewsSource``: the headlines it returns (kind ``rating``) are
journaled with the other news and raise the RATING_CHANGE flag. Each parsed action is stored
once in ``analyst_actions`` (unique ``action_hash``).
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import httpx
import structlog

from trader.models.messages import NewsItem
from trader.news.sources import RateLimiter

log = structlog.get_logger(__name__)

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
GOOGLE_RSS = ("https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
USER_AGENT = "Mozilla/5.0 (compatible; agentic-trader/1.0; personal research)"

ACTIONS = ("upgrade", "downgrade", "initiate", "reiterate", "pt_raise", "pt_cut")
LABEL = {"upgrade": "Upgrade", "downgrade": "Downgrade", "initiate": "Initiated",
         "reiterate": "Reiterated", "pt_raise": "Target raised", "pt_cut": "Target cut"}

_RATING = (r"(?:strong[ -]buy|buy|outperform|overweight|accumulate|positive|add|"
           r"market[ -]perform|sector[ -]perform|peer[ -]perform|in[ -]line|equal[ -]weight|"
           r"neutral|hold|market[ -]weight|sector[ -]weight|"
           r"underperform|underweight|reduce|sell|strong[ -]sell|negative)")
_FIRM = r"(?P<firm>(?-i:[A-Z][\w&.'\-]*(?:\s+(?:[A-Z][\w&.'\-]*|&|of|de))*?))"
# a firm at the end of the headline: greedy over capitalised words ("at Goldman Sachs")
_FIRM_END = r"(?P<firm>(?-i:[A-Z][\w&.'\-]*(?:\s+(?:[A-Z][\w&.'\-]*|&))*))"
_MONEY = r"\$?(?P<{name}>\d{{1,5}}(?:,\d{{3}})*(?:\.\d+)?)"
_NEW = _MONEY.format(name="new")
_OLD = _MONEY.format(name="old")
_PT = r"(?:price target|pt|target|price objective)"
_COMPANY = r"(?P<co>[A-Z][\w.&'\-]*(?:\s+[A-Z][\w.&'\-]*)*?(?:\s*\([A-Z.]{1,6}\))?|\$?[A-Z.]{1,6})"

# (action, pattern); first match wins. Ratings are captured as "to" (and "frm").
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (a, re.compile(p, re.IGNORECASE)) for a, p in [
        ("upgrade", rf"{_FIRM}\s+upgrades?\s+{_COMPANY}\s+to\s+(?P<to>{_RATING})"
                    rf"(?:\s+from\s+(?P<frm>{_RATING}))?"),
        ("downgrade", rf"{_FIRM}\s+downgrades?\s+{_COMPANY}\s+to\s+(?P<to>{_RATING})"
                      rf"(?:\s+from\s+(?P<frm>{_RATING}))?"),
        ("upgrade", rf"{_COMPANY}\s+(?:stock\s+|shares\s+)?(?:is\s+|gets\s+)?upgraded\s+"
                    rf"(?:to\s+(?P<to>{_RATING})\s+)?(?:from\s+(?P<frm>{_RATING})\s+)?"
                    rf"(?:at|by)\s+{_FIRM_END}"),
        ("downgrade", rf"{_COMPANY}\s+(?:stock\s+|shares\s+)?(?:is\s+|gets\s+)?downgraded\s+"
                      rf"(?:to\s+(?P<to>{_RATING})\s+)?(?:from\s+(?P<frm>{_RATING})\s+)?"
                      rf"(?:at|by)\s+{_FIRM_END}"),
        ("initiate", rf"{_FIRM}\s+(?:initiates?|starts?|launches?|begins?)\s+(?:coverage\s+"
                     rf"(?:of|on)\s+)?{_COMPANY}\s+(?:coverage\s+)?(?:with|at)\s+(?:an?\s+)?"
                     rf"(?P<to>{_RATING})"),
        ("pt_raise", rf"{_FIRM}\s+(?:raises|lifts|boosts|hikes|increases|ups)\s+{_COMPANY}"
                     rf"(?:'s)?\s+{_PT}\s+to\s+{_NEW}(?:\s+from\s+{_OLD})?"),
        ("pt_cut", rf"{_FIRM}\s+(?:cuts|lowers|trims|reduces|slashes)\s+{_COMPANY}"
                   rf"(?:'s)?\s+{_PT}\s+to\s+{_NEW}(?:\s+from\s+{_OLD})?"),
        ("pt_raise", rf"{_COMPANY}\s+{_PT}\s+(?:raised|lifted|boosted|increased)\s+to\s+{_NEW}"
                     rf"(?:\s+from\s+{_OLD})?\s+(?:at|by)\s+{_FIRM_END}"),
        ("pt_cut", rf"{_COMPANY}\s+{_PT}\s+(?:cut|lowered|trimmed|reduced)\s+to\s+{_NEW}"
                   rf"(?:\s+from\s+{_OLD})?\s+(?:at|by)\s+{_FIRM_END}"),
        ("reiterate", rf"{_FIRM}\s+(?:reiterates?|maintains?|reaffirms?|keeps?)\s+"
                      rf"(?:an?\s+)?(?P<to>{_RATING})\s+(?:rating\s+)?on\s+{_COMPANY}"),
        ("reiterate", rf"{_FIRM}\s+(?:reiterates?|maintains?|reaffirms?)\s+{_COMPANY}\s+"
                      rf"(?:at\s+|with\s+(?:an?\s+)?)?(?P<to>{_RATING})"),
    ]]
NOT_A_FIRM = {"the", "stock", "shares", "analyst", "analysts", "wall", "why", "how", "is",
              "this", "here", "report", "investors", "it", "should", "will"}


@dataclass(frozen=True)
class RatingAction:
    ticker: str
    ts: datetime
    firm: str
    action: str                  # one of ACTIONS
    from_rating: str | None
    to_rating: str | None
    pt_old: float | None
    pt_new: float | None
    headline: str
    source: str
    url: str = ""

    @property
    def action_hash(self) -> str:
        key = (f"{self.ticker}|{self.firm.lower()}|{self.action}|{self.to_rating or ''}|"
               f"{self.pt_new or ''}|{self.ts.date().isoformat()}")
        return hashlib.sha256(key.encode()).hexdigest()[:40]

    def row(self) -> dict[str, Any]:
        return {"ts": self.ts, "ticker": self.ticker, "firm": self.firm[:64],
                "action": self.action, "from_rating": self.from_rating,
                "to_rating": self.to_rating, "pt_old": self.pt_old, "pt_new": self.pt_new,
                "headline": self.headline[:400], "source": self.source, "url": self.url[:500],
                "action_hash": self.action_hash}


def _rating(v: str | None) -> str | None:
    return v.replace("-", " ").title() if v else None


def _num(v: str | None) -> float | None:
    return float(v.replace(",", "")) if v else None


def parse_headline(headline: str, ticker: str, ts: datetime, source: str, url: str = "",
                   names: Sequence[str] = ()) -> RatingAction | None:
    """One rating action whose subject company is ``ticker`` (by symbol or ``names``),
    or None. "Bank of America raises Amazon target" is an AMZN action, never a BAC one."""
    text = " ".join(html.unescape(headline).split())
    text = re.sub(r"\s+-\s+[^-]{2,40}$", "", text)    # Google News appends " - Publisher"
    for action, pat in PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if not mentions(m.group("co"), ticker, names):
            continue
        firm = m.group("firm").strip(" ,.'")
        if not firm or firm.split()[0].lower() in NOT_A_FIRM or len(firm) > 60:
            continue
        g = m.groupdict()
        return RatingAction(ticker, ts, firm, action, _rating(g.get("frm")),
                            _rating(g.get("to")), _num(g.get("old")), _num(g.get("new")),
                            text, source, url)
    return None


def mentions(headline: str, ticker: str, names: Sequence[str] = ()) -> bool:
    """The headline is about ``ticker`` (symbol or a company name)."""
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", headline):
        return True
    low = headline.lower()
    return any(n.lower() in low for n in names)


ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
FIELD_RE = {k: re.compile(rf"<{k}\b[^>]*>(.*?)</{k}>", re.IGNORECASE | re.DOTALL)
            for k in ("title", "link", "pubDate")}


def rss_items(xml: str) -> list[dict[str, str]]:
    """Title, link and pubDate from each RSS ``<item>`` (CDATA-tolerant, no XML parser)."""
    out = []
    for block in ITEM_RE.findall(xml[:2_000_000]):
        rec = {}
        for k, rx in FIELD_RE.items():
            m = rx.search(block)
            v = m.group(1) if m else ""
            v = re.sub(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", r"\1", v, flags=re.DOTALL)
            rec[k] = html.unescape(re.sub(r"<[^>]+>", "", v)).strip()
        if rec.get("title"):
            out.append(rec)
    return out


def _ts(v: str) -> datetime | None:
    try:
        return parsedate_to_datetime(v).astimezone(UTC) if v else None
    except (TypeError, ValueError):
        return None


FINNHUB_ACTION = {"up": "upgrade", "down": "downgrade", "init": "initiate",
                  "main": "reiterate", "reit": "reiterate"}


class RatingsFeed:
    """Collects rating actions for the universe; also an A3 NewsSource (kind ``rating``)."""

    name = "rss"

    def __init__(self, journal: Any = None, finnhub_token: str | None = None,
                 names: Mapping[str, Sequence[str]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 min_interval_s: int = 1800, now: Any = None) -> None:
        self.journal = journal
        self.token = finnhub_token
        self.names = names or {}
        self._transport = transport
        self.limiter = RateLimiter(30)
        self.finnhub_ok = bool(finnhub_token)
        self.min_interval = timedelta(seconds=min_interval_s)
        self.now = now or (lambda: datetime.now(UTC))
        self.last: dict[str, datetime] = {}
        self.errors = 0

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        await self.limiter.wait()
        async with httpx.AsyncClient(timeout=15, transport=self._transport,
                                     headers={"User-Agent": USER_AGENT},
                                     follow_redirects=True) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r

    async def finnhub(self, sym: str, since: datetime) -> list[RatingAction]:
        if not self.finnhub_ok:
            return []
        try:
            r = await self._get("https://finnhub.io/api/v1/stock/upgrade-downgrade",
                                {"symbol": sym, "from": since.date().isoformat(),
                                 "to": self.now().date().isoformat(), "token": self.token})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                self.finnhub_ok = False
                log.info("finnhub_ratings_not_in_plan", status=exc.response.status_code)
            return []
        out = []
        for row in r.json() or []:
            ts = datetime.fromtimestamp(int(row.get("gradeTime") or 0), UTC)
            action = FINNHUB_ACTION.get(str(row.get("action", "")).lower())
            firm = str(row.get("company") or "").strip()
            if not action or not firm or ts < since:
                continue
            frm, to = _rating(row.get("fromGrade")), _rating(row.get("toGrade"))
            head = (f"{firm} {LABEL[action].lower()} {sym}"
                    + (f" to {to}" if to else "") + (f" from {frm}" if frm else ""))
            out.append(RatingAction(sym, ts, firm, action, frm, to, None, None, head,
                                    "finnhub"))
        return out

    async def rss(self, sym: str, since: datetime) -> list[RatingAction]:
        out: list[RatingAction] = []
        q = quote_plus(f'"{sym}" upgrade OR downgrade OR "price target" OR "initiates coverage"')
        for src, url in (("yahoo", YAHOO_RSS.format(sym=sym)),
                         ("google", GOOGLE_RSS.format(q=q))):
            try:
                xml = (await self._get(url)).text
            except httpx.HTTPError as exc:
                self.errors += 1
                log.info("ratings_rss_failed", source=src, symbol=sym, error=str(exc)[:120])
                continue
            for it in rss_items(xml):
                ts = _ts(it.get("pubDate", "")) or self.now()
                if ts < since:
                    continue
                act = parse_headline(it["title"], sym, ts, src, it.get("link", ""),
                                     self.names.get(sym, ()))
                if act is not None:
                    out.append(act)
        return out

    async def collect(self, symbols: Sequence[str], since: datetime) -> list[RatingAction]:
        """New actions (deduplicated by hash) for symbols not fetched in the last interval;
        stored in ``analyst_actions`` when a journal is attached."""
        now = self.now()
        found: dict[str, RatingAction] = {}
        for sym in symbols:
            last = self.last.get(sym)
            if last is not None and now - last < self.min_interval:
                continue
            self.last[sym] = now
            for act in await self.finnhub(sym, since) + await self.rss(sym, since):
                found.setdefault(act.action_hash, act)
        return self.store(list(found.values()))

    def store(self, acts: list[RatingAction]) -> list[RatingAction]:
        if self.journal is None or not acts:
            return acts
        from trader.journal import schema as s

        have = {r["action_hash"] for r in self.journal.rows(s.analyst_actions)}
        new = [a for a in acts if a.action_hash not in have]
        if new:
            self.journal.insert_many(s.analyst_actions, [a.row() for a in new])
        return new

    async def fetch(self, symbols: Sequence[str], since: datetime) -> list[NewsItem]:
        """NewsSource: new rating actions as ``rating`` news items (RATING_CHANGE flag)."""
        acts = await self.collect(symbols, since)
        return [NewsItem(id=f"rt-{a.action_hash[:24]}", ticker=a.ticker, source="rss",
                         published_at=a.ts, headline=a.headline, url=a.url, kind="rating",
                         meta={"firm": a.firm, "action": a.action, "to": a.to_rating,
                               "pt_new": a.pt_new}) for a in acts]
