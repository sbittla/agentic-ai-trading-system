"""A17 News catalyst agent (NEWS_CATALYST_REQUIREMENTS.md §7). Runs in trader-core and in
``trader catalyst`` / ``trader catalyst-eval``.

Reads news items (+ embeddings), social posts, fundamentals, analyst actions and IV from the
journal and daily bars from the bar store (or trader-core's history feed); writes stories,
outlooks and grades to the journal and the newest outlooks to ``catalyst.latest_file`` for the
console. Advisory only: nothing here reaches the order path (ADR-006).
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import and_, desc, select

from trader.calendar import MacroCalendar, MarketCalendar
from trader.catalyst import evaluate as ev
from trader.catalyst import report
from trader.catalyst.classify import classify_llm, classify_rules
from trader.catalyst.outlook import EventObs, Pool
from trader.catalyst.pipeline import (
    Inputs,
    Outlook,
    Typed,
    build_stories,
    event_obs,
    live,
    outlook,
    type_story,
)
from trader.catalyst.social import Post, SocialDay
from trader.catalyst.social import daily as social_daily
from trader.catalyst.stories import Item, Story
from trader.catalyst.text import decode_vec
from trader.config import Config
from trader.interfaces import LLMClient
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.scanner.techniques import Series
from trader.timeutil import et_date

log = structlog.get_logger(__name__)
SeriesLoader = Callable[[Sequence[str], date], Awaitable[dict[str, Series]]]
SECTOR = {"NVDA": "SMH", "AVGO": "SMH", "TSM": "SMH", "AMD": "SMH", "MU": "SMH",
          "INTC": "SMH", "QCOM": "SMH"}
MAX_LLM_PER_RUN = 20
GRADE_LOOKBACK_D = 120     # NC-35: outlooks graded even after weeks of downtime


def extended_tickers(cfg: Config) -> list[str]:
    from trader.news.names import COMPANY_NAMES

    base = list(cfg.universe.tickers)
    if cfg.catalyst.history_universe == "extended":
        base += [t for t in COMPANY_NAMES if t not in base and "." not in t and t != "GOOG"]
    return list(dict.fromkeys(base))


def parquet_loader(root: Path) -> SeriesLoader:
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        from trader.scanner.data import load_daily

        got = await asyncio.to_thread(load_daily, root, list(symbols))
        out = {}
        for k, v in got.items():
            n = sum(1 for d in v.dates if d <= until)
            if n:
                out[k] = v.upto(n)
        return out
    return load


def history_loader(history: Any) -> SeriesLoader:
    """Daily bars from a HistoricalData client (Alpaca in trader-core and the CLI)."""
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        out: dict[str, Series] = {}
        for t in symbols:
            try:
                bars = await history.daily_bars(t, until - timedelta(days=420),
                                                until + timedelta(days=1))
            except Exception as exc:   # noqa: BLE001
                log.warning("catalyst_history_failed", ticker=t, error=str(exc))
                continue
            rows = [(et_date(b.ts), b.o, b.h, b.l, b.c, float(b.v)) for b in bars
                    if et_date(b.ts) <= until]
            if rows:
                out[t] = Series.from_rows(t, rows)
        return out
    return load


def combined_loader(first: SeriesLoader, then: SeriesLoader | None) -> SeriesLoader:
    """The bar store first; symbols it lacks (or holds stale) from ``then``."""
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        got = await first(symbols, until)
        due = until                     # the newest weekday on or before ``until``
        while due.weekday() >= 5:
            due -= timedelta(days=1)
        missing = [t for t in symbols if t not in got or got[t].dates[-1] < due]
        if missing and then is not None:
            got.update(await then(missing, until))
        return got
    return load


def relevant(it: Item, names: Mapping[str, Sequence[str]]) -> bool:
    """NC-47: an item counts only if it names the company. Feeds file articles about other
    companies under a ticker (a utility's earnings preview under GOOGL); filings, rating
    actions (subject-checked, ADR-016) and insider forms are the company's own."""
    if it.kind in ("8-K", "rating", "form4"):
        return True
    text = f"{it.headline} {it.text[:400]}"
    sym = it.ticker
    if re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", text):
        return True
    low = text.lower()
    return any(re.search(rf"\b{re.escape(n.strip().lower())}\b", low)
               for n in names.get(sym, ()) if n.strip())


class CatalystAgent:
    def __init__(self, cfg: Config, journal: SqlJournal, load_series: SeriesLoader,
                 llm: LLMClient | None, now: Callable[[], datetime],
                 calendar: MarketCalendar | None = None, macro: MacroCalendar | None = None
                 ) -> None:
        self.cfg = cfg
        self.ca = cfg.catalyst
        self.journal = journal
        self.load_series = load_series
        self.llm = llm
        self.now = now
        self.cal = calendar or MarketCalendar()
        self.macro = macro
        from trader.news.names import COMPANY_NAMES

        self.names = COMPANY_NAMES
        self.dropped_irrelevant = 0

    # -- inputs -----------------------------------------------------------------------------
    def items(self, tickers: Sequence[str], since: datetime, until: datetime) -> list[Item]:
        n = s.news_items.c
        rows = self.journal.query(select(
            n.item_id, n.ticker, n.published_at, n.headline, n.cleaned_text, n.source, n.kind,
            n.publisher, n.finbert_score, n.suspicious).where(and_(
                n.ticker.in_(list(tickers)), n.published_at >= since, n.published_at < until)))
        ids = [r["item_id"] for r in rows]
        vecs: dict[str, Any] = {}
        for i in range(0, len(ids), 2000):
            for r in self.journal.query(select(s.news_embeddings.c.item_id,
                                               s.news_embeddings.c.vec).where(
                    s.news_embeddings.c.item_id.in_(ids[i:i + 2000]))):
                vecs[r["item_id"]] = decode_vec(r["vec"])
        out = []
        self.dropped_irrelevant = 0
        for r in rows:
            if r["suspicious"]:
                continue                 # A3-4: instruction-like text never becomes a story
            kind = r["kind"] or ("8-K" if r["source"] == "edgar" else
                                 "rating" if r["source"] == "rss" else
                                 "post" if r["source"] in ("reddit", "stocktwits") else "news")
            if kind == "earnings" or kind == "post":
                continue                 # posts feed the social indicator, not stories
            it = Item(item_id=r["item_id"], ticker=r["ticker"],
                      published_at=r["published_at"], headline=r["headline"] or "",
                      text=r["cleaned_text"] or "", source=r["source"], kind=kind,
                      publisher=r["publisher"] or r["source"],
                      sentiment=float(r["finbert_score"] or 0.0), vec=vecs.get(r["item_id"]))
            if not relevant(it, self.names):
                self.dropped_irrelevant += 1
                continue
            out.append(it)
        return out

    def social(self, tickers: Sequence[str], since: datetime) -> dict[str, list[SocialDay]]:
        p = s.social_posts.c
        rows = self.journal.query(select(p.ticker, p.ts, p.author, p.score, p.tag, p.length,
                                         p.text_hash, p.item_id).where(and_(
            p.ticker.in_(list(tickers)), p.ts >= since)))
        posts = [Post(ticker=r["ticker"], day=et_date(r["ts"]), author=r["author"] or "",
                      text_hash=r["text_hash"] or r["item_id"], score=float(r["score"] or 0),
                      tag=r["tag"], length=int(r["length"] or 0)) for r in rows]
        counts: dict[tuple[str, date], int] = {}
        if "apewisdom" in self.ca.social_sources:
            for r in self.fund_rows(tickers, "apewisdom", since.date()):
                counts[(r["ticker"], r["date"])] = int((r["json"] or {}).get("mentions", 0))
        out: dict[str, list[SocialDay]] = defaultdict(list)
        for d in social_daily(posts, self.ca.max_posts_per_author, counts):
            out[d.ticker].append(d)
        return dict(out)

    def fund_rows(self, tickers: Sequence[str], kind: str, since: date) -> list[dict[str, Any]]:
        f = s.fundamentals_daily.c
        return self.journal.query(select(f.date, f.ticker, f.json).where(and_(
            f.ticker.in_(list(tickers)), f.kind == kind, f.date >= since)).order_by(f.date))

    def latest_fund(self, tickers: Sequence[str], asof: date) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = defaultdict(dict)
        for kind in ("earnings", "recommendation", "insider", "short_volume",
                     "earnings_calendar"):
            for r in self.fund_rows(tickers, kind, asof - timedelta(days=10)):
                if r["date"] <= asof:
                    out[r["ticker"]][kind] = r["json"]
        return dict(out)

    def iv(self, tickers: Sequence[str], asof: date) -> dict[str, dict[str, Any]]:
        o = s.options_iv_daily.c
        out: dict[str, dict[str, Any]] = {}
        for r in self.journal.query(select(o.ticker, o.date, o.atm_iv_30d, o.iv_rank,
                                           o.expected_move_30d).where(and_(
                o.ticker.in_(list(tickers)), o.date <= asof,
                o.date >= asof - timedelta(days=7))).order_by(o.date)):
            out[r["ticker"]] = dict(r)
        return out

    def actions(self, tickers: Sequence[str], since: datetime, until: datetime
                ) -> dict[str, list[dict[str, Any]]]:
        a = s.analyst_actions.c
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.journal.query(select(a.ts, a.ticker, a.action, a.pt_old, a.pt_new).where(
                and_(a.ticker.in_(list(tickers)), a.ts >= since, a.ts < until))):
            out[r["ticker"]].append(dict(r))
        return dict(out)

    def llm_types(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        st = s.news_stories.c
        out = {}
        for i in range(0, len(ids), 2000):
            for r in self.journal.query(select(st.story_id, st.ctype, st.direction).where(and_(
                    st.story_id.in_(list(ids[i:i + 2000])), st.type_source == "model"))):
                out[r["story_id"]] = (r["ctype"], r["direction"])
        return out

    def earnings_next(self, fund: Mapping[str, Mapping[str, Any]], asof: date
                      ) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for t, f in fund.items():
            d = (f.get("earnings_calendar") or {}).get("date")
            if d:
                nd = date.fromisoformat(str(d))
                out[t] = self.cal.trading_days_between(asof, nd) if nd > asof else None
        return out

    def macro_events(self, asof: date, sessions: int) -> list[tuple[int, str]]:
        if self.macro is None:
            return []
        out = []
        d, k = asof, 0
        while k < sessions:
            d += timedelta(days=1)
            if not self.cal.is_trading_day(d):
                continue
            k += 1
            for name, hit in (("FOMC", self.macro.is_fomc(d)), ("CPI", d in self.macro.cpi),
                              ("jobs report", d in self.macro.jobs)):
                if hit:
                    out.append((k, f"{name} {d.isoformat()}"))
        return out

    async def inputs(self, tickers: Sequence[str], asof: date, now: datetime, lookback_d: int
                     ) -> Inputs:
        since = now - timedelta(days=lookback_d)
        syms = list(dict.fromkeys([*tickers, "SPY", *{SECTOR[t] for t in tickers
                                                      if t in SECTOR}]))
        series = await self.load_series(syms, asof)
        spy = series.pop("SPY", None)
        sector = {t: series[SECTOR[t]] for t in tickers if t in SECTOR and SECTOR[t] in series}
        fund = self.latest_fund(tickers, asof)
        from trader.forecast.agent import load_json
        from trader.macro.agent import latest_label

        mult = (load_json(self.cfg.forecast.eval_file) or {}).get("event_multipliers") or {}
        try:
            reg = latest_label(self.journal, asof)
        except Exception:   # noqa: BLE001 - an old journal without the table
            reg = None
        return Inputs(series=series, spy=spy, sector=sector, iv=self.iv(tickers, asof),
                      event_mult=mult, macro_regime=reg,
                      actions=self.actions(tickers, since, now), fundamentals=fund,
                      social=self.social(tickers, since - timedelta(days=30)),
                      earnings_next=self.earnings_next(fund, asof),
                      macro=self.macro_events(asof, max(self.ca.horizons)))

    # -- stories ----------------------------------------------------------------------------
    async def type_all(self, stories: Sequence[Story], inp: Inputs, llm_budget: int
                       ) -> list[Typed]:
        """Rules for every story; Qwen for untyped stories that could matter, within a
        budget (NC-5). Model answers are stored, so each story is asked once."""
        known = self.llm_types([x.story_id for x in stories])
        inp.llm_types = dict(known)
        if self.llm is not None and llm_budget > 0:
            ask = [x for x in stories if x.story_id not in known and classify_rules(x) is None
                   and (len(x.items) >= 2 or x.novelty >= 0.5)]
            ask.sort(key=lambda x: (-len(x.items), x.first_seen))
            version = self.cfg.llm.prompt_versions.get("catalyst", "v1")
            for x in ask[:llm_budget]:
                try:
                    got = await classify_llm(self.llm, x, version)
                except Exception as exc:   # noqa: BLE001 - typing never blocks the outlook
                    log.warning("catalyst_llm_failed", error=str(exc))
                    break
                if got is not None:
                    inp.llm_types[x.story_id] = (got.ctype, got.direction)
        return [type_story(x, self.ca, inp) for x in stories]

    def store_stories(self, typed: Sequence[Typed]) -> None:
        version = self.cfg.llm.prompt_versions.get("catalyst", "v1")
        for t in typed:
            st = t.story
            self.journal.upsert(s.news_stories, {"story_id": st.story_id}, {
                "ticker": st.ticker, "first_seen": st.first_seen, "last_seen": st.last_seen,
                "ctype": t.ctype, "direction": t.direction, "type_source": t.type_source,
                "prompt_version": version if t.type_source == "model" else None,
                "novelty": st.novelty, "items": len(st.items), "sources": len(st.publishers),
                "sentiment": t.sentiment, "strength": t.strength.score,
                "headline": st.headline[:500], "item_ids": [i.item_id for i in st.items]})

    # -- evaluation ---------------------------------------------------------------------------
    def load_eval(self) -> dict[str, Any] | None:
        return report.load_json(self.ca.eval_file)

    def pool_from(self, data: Mapping[str, Any] | None, asof: date) -> Pool:
        """Past events whose outcome was known by ``asof`` (no look-ahead)."""
        pool = Pool()
        for d in (data or {}).get("event_list", []):
            o = EventObs.from_dict(d)
            hs = [h for h, e in o.end.items() if e < asof]
            if hs:
                pool.add(o, hs)
        return pool

    async def history(self, days: int, tickers: Sequence[str] | None = None
                      ) -> tuple[list[EventObs], Inputs]:
        """NC-30: stories and outcomes over the stored history (rules typing only for the
        bulk; stored model types are reused)."""
        now = self.now()
        tks = list(tickers or extended_tickers(self.cfg))
        since = now - timedelta(days=days)
        inp = await self.inputs(tks, et_date(now), now, days + 40)
        items = await asyncio.to_thread(self.items, tks, since - timedelta(days=35), now)
        stories = await asyncio.to_thread(build_stories, items, self.ca)
        typed = await self.type_all(stories, inp, 0)
        obs = event_obs([t for t in typed if t.story.first_seen >= since], inp,
                        self.ca.horizons)
        return obs, inp

    async def evaluate(self, days: int = 365) -> dict[str, Any]:
        """``trader catalyst-eval``: walk-forward grading, gates, and the event list the live
        outlooks use."""
        obs, _ = await self.history(days)
        ca = self.ca
        res = await asyncio.to_thread(ev.evaluate, obs, ca.horizons, k=ca.shrink_k,
                                      stance_p=ca.stance_p, min_graded=ca.gate_min_graded,
                                      coverage=ca.gate_coverage)
        res["generated"] = self.now().isoformat()
        res["event_list"] = [o.as_dict() for o in obs]
        report.write_latest(ca.eval_file, res)
        return res

    # -- live run -----------------------------------------------------------------------------
    async def run(self, tickers: Sequence[str] | None = None, asof: date | None = None,
                  sizer: Callable[[float, float], tuple[int, float, str]] | None = None,
                  write: bool = True, llm_budget: int = MAX_LLM_PER_RUN) -> list[Outlook]:
        """Outlooks for the live catalysts (NC-4) on ``tickers`` (default: the universe)."""
        now = self.now()
        day = asof or et_date(now)
        cut = now if asof is None else datetime.combine(day + timedelta(days=1),
                                                         datetime.min.time(), UTC)
        tks = [t.upper() for t in (tickers or self.cfg.universe.tickers)]
        inp = await self.inputs(tks, day, cut, self.ca.novelty_days + 5)
        items = await asyncio.to_thread(self.items, tks,
                                        cut - timedelta(days=self.ca.novelty_days + 5), cut)
        stories = await asyncio.to_thread(build_stories, items, self.ca)
        recent = [x for x in stories
                  if cut - x.last_seen <= timedelta(hours=self.ca.story_window_h)]
        typed = await self.type_all(recent, inp, llm_budget)
        if write:
            self.store_stories(typed)
        data = self.load_eval()
        gate = (data or {}).get("gate") or {"pass": False}
        adjustments = tuple((data or {}).get("adjustments") or ())
        pool = self.pool_from(data, day)
        if data is not None and data.get("tilt") is False:   # NC-49: tilt switched off
            from trader.catalyst.evaluate import NO_TILT_K

            ca_k = self.ca.model_copy(update={"shrink_k": NO_TILT_K})
        else:
            ca_k = self.ca
        outs = []
        for t in live(typed, self.ca, cut):
            o = outlook(t, pool, inp, ca_k, cut, adjustments=adjustments,
                        calibrated=bool(gate.get("pass")), sizer=sizer)
            if o is not None:
                outs.append(o)
        if write:
            self.store_outlooks(outs, now)
            track = (data or {}).get("horizons")
            report.write_latest(self.ca.latest_file, report.as_json(
                [o.as_dict() for o in outs], as_of=day.isoformat(), generated=now,
                source="trader-core", gate=gate, track=track, notes=self.notes(pool, inp)))
        return outs

    def notes(self, pool: Pool, inp: Inputs) -> list[str]:
        out = []
        if pool.n < 30:
            out.append(f"Only {pool.n} past events with known outcomes: run "
                       "`./run.sh backfill-catalysts 365` so the ranges have history behind "
                       "them. Until then they are the volatility range with no tilt.")
        if not any(inp.social.values()):
            out.append("No social data: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in "
                       ".env.news (docs/ACCOUNT_SETUP.md).")
        return out

    def store_outlooks(self, outs: Sequence[Outlook], now: datetime) -> None:
        for o in outs:
            d = o.as_dict()
            self.journal.upsert(s.catalyst_outlooks, {"outlook_id": o.outlook_id}, {
                "ts": now, "as_of": o.as_of, "ticker": o.ticker, "story_id": d["story_id"],
                "strength": d["strength"], "label": d["label"], "direction": d["direction"],
                "ctype": d["ctype"], "factors": d["factors"], "confluence": d["confluence"],
                "social": d["social"], "horizons": d["horizons"],
                "stance": d["view"]["stance"], "vehicle": d["view"]["vehicle"],
                "plan": d["view"]["plan"], "calibrated": d["calibrated"],
                "text": report.render_one(d)[:4000]})

    # -- grading ------------------------------------------------------------------------------
    async def grade(self) -> int:
        """NC-35: grade every stored outlook whose horizon has ended."""
        now = self.now()
        o = s.catalyst_outlooks.c
        rows = self.journal.query(select(o.outlook_id, o.as_of, o.ticker, o.horizons, o.stance)
                                  .where(o.as_of >= et_date(now) - timedelta(days=GRADE_LOOKBACK_D))
                                  .order_by(desc(o.as_of)))
        if not rows:
            return 0
        g = s.catalyst_grades.c
        since = et_date(now) - timedelta(days=GRADE_LOOKBACK_D)
        done = {(r["outlook_id"], r["horizon"]) for r in self.journal.query(
            select(g.outlook_id, g.horizon).where(g.as_of >= since))}
        tks = sorted({r["ticker"] for r in rows})
        series = await self.load_series([*tks, "SPY"], et_date(now))
        spy = series.get("SPY")
        n = 0
        for r in rows:
            sr = series.get(r["ticker"])
            if sr is None or r["as_of"] not in sr.dates:
                continue
            i0 = sr.dates.index(r["as_of"])
            for v in r["horizons"] or []:
                h = int(v["h"])
                if (r["outlook_id"], h) in done or i0 + h >= len(sr.c):
                    continue
                ret = float(sr.c[i0 + h] / sr.c[i0] - 1) * 100
                mk = 0.0
                if spy is not None and r["as_of"] in spy.dates and sr.dates[i0 + h] in spy.dates:
                    a, b = spy.dates.index(r["as_of"]), spy.dates.index(sr.dates[i0 + h])
                    mk = float(spy.c[b] / spy.c[a] - 1) * 100
                self.journal.insert(s.catalyst_grades, {
                    "outlook_id": r["outlook_id"], "as_of": r["as_of"], "ticker": r["ticker"],
                    "horizon": h, "p_up": v["p_up"], "p10": v["p10_pct"], "p90": v["p90_pct"],
                    "median": v["median_pct"], "stance": r["stance"], "realized": round(ret, 3),
                    "realized_excess": round(ret - mk, 3),
                    "in_range": v["p10_pct"] <= ret <= v["p90_pct"], "up": ret > 0,
                    "graded_at": now})
                n += 1
        return n

    def live_track(self) -> dict[str, Any]:
        """Graded live outlooks so far, per horizon (NC-35)."""
        g = s.catalyst_grades.c
        rows = self.journal.query(select(g.horizon, g.p_up, g.up, g.in_range))
        by: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by[int(r["horizon"])].append(r)
        return {str(h): {"graded": len(v),
                         "brier": round(sum((float(x["p_up"]) - (1.0 if x["up"] else 0.0)) ** 2
                                            for x in v) / len(v), 4),
                         "coverage": round(sum(1 for x in v if x["in_range"]) / len(v), 3)}
                for h, v in sorted(by.items()) if v}
