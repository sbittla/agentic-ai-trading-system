"""A18 Macro agent (MACRO_FORECAST_REQUIREMENTS.md §2).

``collect`` runs in news-sentiment (internet, FinBERT, the news role writes ``macro_series``
and ``macro_news``); ``MacroAgent`` runs in trader-core and the CLI (regime, report,
``macro_daily``). Advisory: the regime is shown and used as a check, never as an order.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select

from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.macro import regime as rg
from trader.macro.sources import MacroFeeds
from trader.scanner.techniques import Series
from trader.timeutil import et_date

log = structlog.get_logger(__name__)
SeriesLoader = Callable[[Sequence[str], date], Awaitable[dict[str, Series]]]
NAMES = {"CPIAUCSL": "CPI", "CPILFESL": "Core CPI", "PCEPILFE": "Core PCE",
         "UNRATE": "Unemployment rate", "PAYEMS": "Payrolls", "ICSA": "Initial claims",
         "INDPRO": "Industrial production", "RSAFS": "Retail sales",
         "UMCSENT": "Consumer sentiment", "DFF": "Fed funds", "DGS2": "2-year Treasury",
         "DGS10": "10-year Treasury", "T10Y2Y": "Curve 10y-2y", "T10Y3M": "Curve 10y-3m",
         "BAMLH0A0HYM2": "High-yield spread", "VIXCLS": "VIX", "VXVCLS": "VIX 3-month",
         "DTWEXBGS": "Dollar index", "DCOILWTICO": "WTI oil"}


# --- news-sentiment side ---------------------------------------------------------------------

async def collect(journal: SqlJournal, feeds: MacroFeeds, series: Sequence[str],
                  scorer: Any, *, history: bool = False, categories: Sequence[str] = ("general",)
                  ) -> dict[str, int]:
    """MA-2 / MA-4: new observations for every series, then Fed and economy headlines."""
    from trader.agents.a03_news import clean_text

    m = s.macro_series.c
    have = {r["series_id"]: r["d"] for r in journal.query(
        select(m.series_id, func.max(m.date).label("d")).group_by(m.series_id))}
    counts: dict[str, int] = {}
    start = None if history else min(have.values(), default=None)
    got = await feeds.fred(series, None if history or start is None else
                           start - timedelta(days=400))
    for sid, obs in got.items():
        last = have.get(sid)
        rows = [{"series_id": sid, "date": d, "value": v} for d, v in obs
                if last is None or d > last]
        if rows:
            for i in range(0, len(rows), 2000):
                journal.insert_many(s.macro_series, rows[i:i + 2000])
        counts[sid] = len(rows)
    news = await feeds.fed_news() + await feeds.general_news(categories)
    ids = [x["item_id"] for x in news]
    known = {r["item_id"] for r in journal.query(select(s.macro_news.c.item_id).where(
        s.macro_news.c.item_id.in_(ids)))} if ids else set()
    fresh = [x for x in news if x["item_id"] not in known]
    cleaned = [clean_text(f"{x['title']}. {x.get('text', '')}") for x in fresh]
    scores = scorer.score([c for c, _ in cleaned]) if fresh else []
    rows = []
    for x, (c, sus), sc in zip(fresh, cleaned, scores, strict=True):
        head = clean_text(x["title"])[0][:500]
        rows.append({"item_id": x["item_id"], "ts": x["ts"], "source": x["source"],
                     "headline": head, "cleaned_text": c, "theme": rg.theme(head, c),
                     "tone": rg.tone(head, sc), "sentiment": sc, "suspicious": sus})
    if rows:
        journal.insert_many(s.macro_news, rows)
    counts["news"] = len(rows)
    return counts


# --- trader-core side ----------------------------------------------------------------------

class MacroAgent:
    def __init__(self, cfg: Config, journal: SqlJournal, load_series: SeriesLoader,
                 now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.journal = journal
        self.load_series = load_series
        self.now = now

    def series(self, since: date | None = None) -> dict[str, list[tuple[date, float]]]:
        m = s.macro_series.c
        q = select(m.series_id, m.date, m.value).order_by(m.series_id, m.date)
        if since is not None:
            q = q.where(m.date >= since)
        out: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for r in self.journal.query(q):
            out[r["series_id"]].append((r["date"], float(r["value"])))
        return dict(out)

    def news(self, days: int) -> list[dict[str, Any]]:
        n = s.macro_news.c
        rows = self.journal.query(select(n.ts, n.source, n.headline, n.theme, n.tone,
                                         n.sentiment, n.suspicious)
                                  .where(n.ts >= self.now() - timedelta(days=days))
                                  .order_by(n.ts.desc()))
        return [dict(r) for r in rows if not r["suspicious"]]

    async def run(self, asof: date | None = None, write: bool = True) -> dict[str, Any]:
        """MA-6..MA-8: today's regime from data published before ``asof``."""
        day = asof or et_date(self.now()) + timedelta(days=1)   # include today's closes
        series = self.series(day - timedelta(days=800))
        spy = (await self.load_series(["SPY"], day)).get("SPY")
        reg = rg.regime(series, spy, day, self.cfg.macro.risk_on_at, self.cfg.macro.risk_off_at)
        news = self.news(self.cfg.macro.news_days)
        themes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for x in news:
            themes[x["theme"]][x["tone"]] += 1
        payload = {"generated": self.now().isoformat(), "regime": reg.as_dict(),
                   "series_last": {sid: {"name": NAMES.get(sid, sid), "date": o[-1][0].isoformat(),
                                         "value": o[-1][1]}
                                   for sid, o in series.items() if o},
                   "news": [{**x, "ts": x["ts"].isoformat()} for x in news[:40]],
                   "themes": {k: dict(v) for k, v in themes.items()},
                   "notes": [] if series else ["No macro data yet: run `./run.sh macro-backfill`."]}
        if write:
            self.journal.upsert(s.macro_daily, {"date": et_date(self.now())}, {
                "score": reg.score, "label": reg.label,
                "indicators": [vars(i) for i in reg.indicators], "releases": reg.releases})
            p = Path(self.cfg.macro.latest_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
            tmp.replace(p)
        return payload

    async def evaluate(self) -> dict[str, Any]:
        series = self.series()
        spy = (await self.load_series(["SPY"], et_date(self.now()))).get("SPY")
        if spy is None:
            return {"error": "no SPY bars"}
        return rg.evaluate(series, spy, risk_on_at=self.cfg.macro.risk_on_at,
                           risk_off_at=self.cfg.macro.risk_off_at)


def latest_label(journal: SqlJournal, asof: date) -> dict[str, Any] | None:
    """The newest stored regime on or before ``asof`` (for the catalyst panel, MA-7)."""
    d = s.macro_daily.c
    rows = journal.query(select(d.date, d.score, d.label).where(d.date <= asof)
                         .order_by(d.date.desc()).limit(1))
    return dict(rows[0]) if rows else None


def render(payload: dict[str, Any]) -> str:
    """Telegram/CLI text (MA-8)."""
    r = payload["regime"]
    lines = [f"Macro regime {r['asof']}: {r['label']} (score {r['score']:+.0f})"]
    for i in r["indicators"]:
        mark = {1: "+", -1: "-", 0: "·"}[i["signal"]]
        lines.append(f"  {mark} {i['name']}: {i['detail']}")
    if r["releases"]:
        lines.append("Latest releases (surprise vs trend, not consensus):")
        for sid, x in r["releases"].items():
            sd = x.get("surprise_sd")
            lines.append(f"  {NAMES.get(sid, sid)} {x['date']}: {x['value']:g}"
                         + (f" ({sd:+.1f} sd vs trend)" if sd is not None else ""))
    news = payload.get("news") or []
    if news:
        lines.append("Headlines:")
        for x in news[:6]:
            lines.append(f"  [{x['theme']}, {x['tone']}] {x['headline'][:120]}")
    for n in payload.get("notes", []):
        lines.append(n)
    lines.append("Advisory: the regime is a check next to each idea, never an order.")
    return "\n".join(lines)
