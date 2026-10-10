"""news-sentiment service: A3 (+ the A1 weekly market-cap check, which needs Finnhub).

Holds no Alpaca keys (SEC-17). Publishes event flags on the ``news_flags`` channel only;
the database trigger rejects anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from trader import config as config_mod
from trader.agents.a01_universe import MEGA_CAP_CANDIDATES, UniverseAgent
from trader.agents.a03_news import NewsAgent, make_scorer
from trader.bus import SqlOutbox
from trader.calendar import MacroCalendar, MarketCalendar
from trader.interfaces import NewsSource
from trader.journal.store import SqlJournal
from trader.llm.ollama_client import OllamaClient
from trader.news.sources import EdgarFilings, FinnhubNews, RedditPosts
from trader.services.runtime import touch_health
from trader.timeutil import to_et

log = structlog.get_logger(__name__)


class NewsService:
    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path

    async def run(self) -> None:
        loaded = config_mod.load(self.config_path, check_env=True)
        cfg = loaded.config
        journal = SqlJournal(os.environ[cfg.storage.journal_url_env], create=False)
        outbox = SqlOutbox(journal, "news")
        sources: list[NewsSource] = []
        finnhub = None
        if os.environ.get("FINNHUB_TOKEN"):
            finnhub = FinnhubNews(os.environ["FINNHUB_TOKEN"])
            sources.append(finnhub)
        ua = os.environ.get("SEC_USER_AGENT") or cfg.data.sec_user_agent
        sources.append(EdgarFilings(ua))
        if os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET"):
            with contextlib.suppress(Exception):
                sources.append(RedditPosts(os.environ["REDDIT_CLIENT_ID"],
                                           os.environ["REDDIT_CLIENT_SECRET"],
                                           os.environ.get("REDDIT_USER_AGENT", ua),
                                           cfg.data.reddit_subreddits))
        base = Path(loaded.path).resolve().parent.parent
        cal = MarketCalendar()
        macro = MacroCalendar.load(base / cfg.windows.macro_calendar_file)
        llm = OllamaClient(cfg.llm.base_url, cfg.llm.model, journal=journal,
                           temperature=cfg.llm.temperature, seed=cfg.llm.seed)
        agent = NewsAgent(cfg, sources, await asyncio.to_thread(make_scorer), llm, journal, cal,
                          macro, lambda: datetime.now(UTC))
        universe = UniverseAgent(cfg, None, lambda: datetime.now(UTC))

        async def heartbeat() -> None:
            while True:
                touch_health("news")
                with contextlib.suppress(Exception):
                    await outbox.publish("heartbeat", {"service": "news",
                                                       "errors": agent.source_errors})
                await asyncio.sleep(cfg.services.heartbeat_sec)

        async def poll() -> None:
            last_universe = None
            while True:
                now = datetime.now(UTC)
                local = to_et(now)
                market_day = cal.is_trading_day(local.date())
                active = market_day and 7 * 60 + 15 <= local.hour * 60 + local.minute <= 16 * 60
                if active:
                    try:
                        for ef in await agent.cycle():
                            await outbox.publish("news_flags", ef)
                    except Exception as exc:   # noqa: BLE001
                        log.exception("news_cycle_failed", error=str(exc))
                if local.hour == 16 and local.minute < cfg.data.news_poll_min:
                    agent.end_of_day()
                # A1-2: Sunday 18:00 universe comparison (never edits the list, UR-4)
                if (finnhub is not None and local.weekday() == 6 and local.hour >= 18
                        and last_universe != local.date()):
                    last_universe = local.date()
                    caps = await finnhub.market_caps(sorted(set(MEGA_CAP_CANDIDATES)
                                                            | set(cfg.universe.tickers)))
                    rep = universe.weekly_compare(caps, local.date())
                    await outbox.publish("news_flags", {"type": "universe_report", **rep})
                wait = cfg.data.news_poll_min * 60 if active else 15 * 60
                await asyncio.sleep(_until_next(now, wait))

        def hot_tickers() -> list[str]:
            """NW-1: tickers with an open position or on today's game plan (journal reads
            granted to the news role by `trader migrate`)."""
            from sqlalchemy import select

            from trader.journal import schema as s

            out: set[str] = set()
            with contextlib.suppress(Exception):
                for r in journal.query(select(s.positions.c.ticker).where(
                        s.positions.c.closed_at.is_(None))):
                    out.add(r["ticker"])
            with contextlib.suppress(Exception):
                today = to_et(datetime.now(UTC)).date()
                for r in journal.query(select(s.game_plans.c.ticker).where(
                        s.game_plans.c.date == today)):
                    if r["ticker"]:
                        out.add(r["ticker"])
            return sorted(out)

        async def hot_poll() -> None:
            while True:
                now = datetime.now(UTC)
                local = to_et(now)
                minute = local.hour * 60 + local.minute
                in_window = cal.is_trading_day(local.date()) and (
                    9 * 60 + 25 <= minute <= 10 * 60 + 35 or 14 * 60 + 55 <= minute <= 16 * 60)
                if in_window:
                    try:
                        hot = await asyncio.to_thread(hot_tickers)
                        for ef in await agent.hot_cycle(hot):
                            await outbox.publish("news_flags", ef)
                    except Exception as exc:   # noqa: BLE001
                        log.warning("news_hot_cycle_failed", error=str(exc))
                await asyncio.sleep(_until_next(now, cfg.data.news_poll_open_s if in_window
                                                else 300))

        await asyncio.gather(heartbeat(), poll(), hot_poll())


def _until_next(now: datetime, seconds: int) -> float:
    nxt = now + timedelta(seconds=seconds)
    return max(1.0, (nxt - datetime.now(UTC)).total_seconds())


def main(config_path: str | None = None) -> None:
    asyncio.run(NewsService(config_path).run())
