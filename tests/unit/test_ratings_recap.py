"""Analyst ratings (UI-33), the past-week/month recap and backfill (UI-34)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import PLAIN_DAY, et
from trader.agents.a03_news import LexiconScorer, NewsAgent
from trader.agents.recap import build_recap, recap_text, run_recap, tags_for, ticker_metrics
from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.fakes import RulesLLM
from trader.models.messages import Bar, NewsItem
from trader.news.ratings import RatingsFeed, parse_headline, rss_items

TS = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# --- headline parser --------------------------------------------------------------------------

@pytest.mark.rule("UI-33")
@pytest.mark.parametrize(("headline", "ticker", "action", "firm", "frm", "to", "old", "new"), [
    ("Morgan Stanley upgrades NVDA to Overweight from Equal-Weight", "NVDA", "upgrade",
     "Morgan Stanley", "Equal Weight", "Overweight", None, None),
    ("Apple downgraded to Neutral at Goldman Sachs - Investing.com", "AAPL", "downgrade",
     "Goldman Sachs", None, "Neutral", None, None),
    ("Bank of America raises Amazon price target to $250 from $230", "AMZN", "pt_raise",
     "Bank of America", None, None, 230.0, 250.0),
    ("Barclays cuts Meta Platforms price target to $600", "META", "pt_cut", "Barclays",
     None, None, None, 600.0),
    ("Jefferies initiates coverage of Microsoft with a Buy rating", "MSFT", "initiate",
     "Jefferies", None, "Buy", None, None),
])
def test_parse_headline_actions(headline: str, ticker: str, action: str, firm: str,
                                frm: str | None, to: str | None, old: float | None,
                                new: float | None) -> None:
    names = {"AMZN": ["Amazon"], "AAPL": ["Apple"], "META": ["Meta"],
             "MSFT": ["Microsoft"], "NVDA": ["Nvidia"]}[ticker]
    a = parse_headline(headline, ticker, TS, "google", names=names)
    assert a is not None, headline
    assert (a.action, a.firm, a.from_rating, a.to_rating) == (action, firm, frm, to)
    assert (a.pt_old, a.pt_new) == (old, new)
    assert " - Investing.com" not in a.headline


@pytest.mark.rule("UI-33")
def test_parse_headline_needs_the_subject_company() -> None:
    # "Bank of America raises Amazon target" is never a BAC action
    assert parse_headline("Bank of America raises Amazon price target to $250", "BAC", TS,
                          "google", names=["Bank of America"]) is None
    assert parse_headline("Nvidia unveils new data-center chips", "NVDA", TS, "yahoo",
                          names=["Nvidia"]) is None
    assert parse_headline("Why the stock upgrades NVDA to Buy", "NVDA", TS, "yahoo") is None


def rss(*titles: tuple[str, str]) -> str:
    items = "".join(f"<item><title><![CDATA[{t}]]></title><link>https://x/{i}</link>"
                    f"<pubDate>{d}</pubDate></item>" for i, (t, d) in enumerate(titles))
    return f"<rss><channel>{items}</channel></rss>"


def test_rss_items_cdata_and_entities() -> None:
    xml = rss(("Citi raises NVDA target &amp; more", "Wed, 14 Oct 2026 10:00:00 GMT"))
    [it] = rss_items(xml)
    assert it["title"] == "Citi raises NVDA target & more" and it["link"] == "https://x/0"


# --- feed --------------------------------------------------------------------------------------

def feed_transport(calls: list[str], finnhub_status: int = 403) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.host)
        if req.url.host == "finnhub.io":
            return httpx.Response(finnhub_status, json=[{
                "symbol": "NVDA", "company": "Citi", "action": "up", "fromGrade": "Neutral",
                "toGrade": "Buy", "gradeTime": int(TS.timestamp())}])
        if "yahoo" in req.url.host:
            return httpx.Response(200, text=rss(
                ("Morgan Stanley upgrades NVDA to Overweight", "Thu, 15 Oct 2026 10:00:00 GMT"),
                ("Old: Citi downgrades NVDA to Sell", "Mon, 01 Jun 2026 10:00:00 GMT")))
        return httpx.Response(200, text=rss(
            ("Morgan Stanley upgrades NVDA to Overweight - Reuters",
             "Thu, 15 Oct 2026 10:00:00 GMT"),
            ("Nvidia hits record", "Thu, 15 Oct 2026 11:00:00 GMT")))
    return httpx.MockTransport(handler)


@pytest.mark.rule("UI-33")
async def test_ratings_feed_rss_dedupes_and_stores(journal: SqlJournal) -> None:
    calls: list[str] = []
    now = TS + timedelta(hours=2)
    feed = RatingsFeed(journal, "tok", names={"NVDA": ["Nvidia"]},
                       transport=feed_transport(calls), now=lambda: now)
    feed.limiter.wait = _no_wait                                      # type: ignore[method-assign]
    items = await feed.fetch(["NVDA"], now - timedelta(days=7))
    assert len(items) == 1 and items[0].kind == "rating" and items[0].source == "rss"
    assert items[0].meta["firm"] == "Morgan Stanley"
    assert not feed.finnhub_ok                                        # 403: not in the plan
    [row] = journal.rows(s.analyst_actions)
    assert row["action"] == "upgrade" and row["to_rating"] == "Overweight"
    n = len(calls)
    assert await feed.fetch(["NVDA"], now - timedelta(days=7)) == []   # polling interval
    assert len(calls) == n
    feed.last.clear()
    assert await feed.fetch(["NVDA"], now - timedelta(days=7)) == []   # already journaled
    assert "finnhub.io" not in calls[n:]                              # disabled after 403


async def test_ratings_feed_finnhub_rows(journal: SqlJournal) -> None:
    calls: list[str] = []
    now = TS + timedelta(hours=2)
    feed = RatingsFeed(None, "tok", transport=feed_transport(calls, 200), now=lambda: now)
    feed.limiter.wait = _no_wait                                      # type: ignore[method-assign]
    acts = await feed.finnhub("NVDA", now - timedelta(days=7))
    assert [(a.firm, a.action, a.from_rating, a.to_rating) for a in acts] == [
        ("Citi", "upgrade", "Neutral", "Buy")]


async def _no_wait() -> None:
    return None


# --- recap -------------------------------------------------------------------------------------

def daily(sym: str, closes: list[float], end: date = PLAIN_DAY) -> list[Bar]:
    out = []
    for i, c in enumerate(closes):
        d = end - timedelta(days=len(closes) - 1 - i)
        out.append(Bar(symbol=sym, ts=datetime(d.year, d.month, d.day, 14, 30, tzinfo=UTC),
                       timeframe="1Day", o=c - 0.5, h=c + 1, l=c - 1, c=c, v=1_000_000))
    return out


@pytest.mark.rule("UI-34")
def test_ticker_metrics_levels_and_strength() -> None:
    qqq = daily("QQQ", [100 + 0.1 * i for i in range(60)])
    up = daily("NVDA", [100 + 1.0 * i for i in range(60)])
    m = ticker_metrics(up, qqq, 21)
    assert m is not None
    assert m["close"] == 159 and m["levels"]["prior_high"] == 160
    assert m["levels"]["prior_low"] == 158 and m["levels"]["high20"] == 160
    assert m["rs_qqq"] is not None and m["rs_qqq"] > 3 and m["above_ema20"]
    assert m["atr14"] == pytest.approx(2.0, abs=0.01)
    tags = tags_for(m, 21, Counter({"upgrade": 1}), earnings_soon=True)
    assert tags == ["leader", "near 20d high", "upgraded", "earnings soon"]
    short = ticker_metrics(up[:5], qqq, 21)
    assert short is not None and short["ret_5d"] is None and short["ret_21d"] is None
    assert ticker_metrics(up[:1], qqq, 21) is None


@pytest.mark.rule("UI-34")
def test_recap_text_leaders_watchlist_and_events() -> None:
    bars = {"SPY": daily("SPY", [400 + 0.2 * i for i in range(60)]),
            "QQQ": daily("QQQ", [100 + 0.1 * i for i in range(60)]),
            "NVDA": daily("NVDA", [100 + 1.0 * i for i in range(60)]),
            "AAPL": daily("AAPL", [200 - 1.0 * i for i in range(60)])}
    ratings = [{"ticker": "NVDA", "ts": TS, "firm": "Citi", "action": "upgrade",
                "to_rating": "Buy", "pt_new": None}]
    rows = build_recap(bars, ["NVDA", "AAPL", "MSFT"], 5, ratings, [], {"AAPL"})
    assert [r["ticker"] for r in rows] == ["SPY", "QQQ", "NVDA", "AAPL"]   # no MSFT bars
    text = recap_text(rows, 5, PLAIN_DAY, PLAIN_DAY + timedelta(days=1), ["FOMC Thu"])
    assert text.startswith("RECAP 5 sessions")
    assert "Leaders vs QQQ (5d, pts): NVDA" in text and "Laggards vs QQQ (5d, pts): AAPL" in text
    assert "NVDA: Citi: upgrade to Buy" in text and "Events: FOMC Thu" in text
    assert "earnings soon" in text and "above 160.00 (prior high)" in text
    assert text.endswith("Not advice.")
    quiet = recap_text([r for r in rows if r["kind"] == "index"], 5, PLAIN_DAY, PLAIN_DAY)
    assert "No ticker stands out" in quiet


@pytest.mark.rule("UI-34")
async def test_run_recap_journals_rows(cfg: Config, cal: MarketCalendar, macro: MacroCalendar,
                                       journal: SqlJournal) -> None:
    end = cal.prev_trading_day(PLAIN_DAY)

    class Hist:
        async def daily_bars(self, sym: str, start: date, stop: date) -> list[Bar]:
            if sym == "TSLA":
                raise RuntimeError("feed down")              # one symbol must not stop it
            return daily(sym, [100 + 0.5 * i for i in range(80)], end)

    rows, text = await run_recap(cfg, journal, Hist(), cal, macro, 5, et(PLAIN_DAY, "08:00"))
    tickers = {r["ticker"] for r in rows}
    assert {"SPY", "QQQ", "SMH"} <= tickers and "TSLA" not in tickers
    assert f"to {end:%a %b %d}" in text
    stored = journal.rows(s.market_recap)
    assert len(stored) == len(rows) and stored[0]["as_of"] == end


# --- backfill and RECAP command -----------------------------------------------------------------

@pytest.mark.rule("UI-34")
async def test_news_backfill_skips_known_items(cfg: Config, cal: MarketCalendar,
                                               macro: MacroCalendar,
                                               journal: SqlJournal) -> None:
    now = et(PLAIN_DAY, "07:15")
    seen: list[datetime] = []

    class Src:
        name = "finnhub"

        async def fetch(self, symbols: list[str], since: datetime) -> list[NewsItem]:
            seen.append(since)
            return [NewsItem(id=f"b{i}", ticker="NVDA", source="recorded", kind="news",
                             published_at=now - timedelta(days=i), headline=f"Nvidia {i}")
                    for i in range(3)]

    class Reddit(Src):
        name = "reddit"

        async def fetch(self, symbols: list[str], since: datetime) -> list[NewsItem]:
            raise AssertionError("reddit has no history; backfill skips it")

    agent = NewsAgent(cfg, [Src(), Reddit()], LexiconScorer(), RulesLLM(), journal, cal,
                      macro, lambda: now)
    first = await agent.backfill(30)
    assert first["finnhub"] == 3 and first["new_items"] == 3 and "reddit" not in first
    assert seen[0] == now - timedelta(days=30)
    again = await agent.backfill(30)
    assert again["new_items"] == 0
    assert len(journal.rows(s.news_items)) == 3


@pytest.mark.rule("UI-34")
async def test_recap_command_sends_watchlist(tmp_path: Path) -> None:
    from tests.replay.helpers import Harness
    from trader.config import default_config
    from trader.models.messages import Command
    from trader.notify.commands import SAFE

    assert "RECAP" in SAFE
    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    h.clock.advance_to(et(PLAIN_DAY, "17:00").astimezone(UTC))
    reply = await h.engine.handle_command(Command(name="RECAP", arg="21", sender="telegram:42",
                                                  received_at=h.clock.now(),
                                                  source="telegram"))
    assert reply.startswith("RECAP of the last 21 sessions started")
    await h.engine.settle()
    sent: list[Any] = [m for k, m in h.notifier.sent if k == "recap"]
    assert sent and "RECAP 21 sessions to Fri Oct 16" in sent[0]


def test_run_sh_has_backfill() -> None:
    sh = Path("run.sh").read_text()
    assert "backfill) cmd_backfill" in sh and "backfill-news --days" in sh
    assert "trader recap --days" in sh
