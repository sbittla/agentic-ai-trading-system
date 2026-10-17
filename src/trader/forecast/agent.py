"""Forecast runs and grading (FC-1..FC-6). Bars come from the same loaders as the catalyst
agent (bar store, then Alpaca); results go to ``forecast.latest_file`` and ``eval_file``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trader.calendar import MacroCalendar
from trader.config import Config
from trader.forecast import models as fm
from trader.scanner.techniques import Series
from trader.timeutil import et_date

SeriesLoader = Callable[[Sequence[str], date], Awaitable[dict[str, Series]]]


def _write(path: str, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def load_json(path: str) -> dict[str, Any] | None:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def macro_events(macro: MacroCalendar | None) -> dict[str, frozenset[date]]:
    if macro is None:
        return {}
    return {"fomc": macro.fomc, "cpi": macro.cpi, "jobs": macro.jobs}


class ForecastAgent:
    def __init__(self, cfg: Config, load_series: SeriesLoader, macro: MacroCalendar | None,
                 now: Callable[[], datetime]) -> None:
        self.cfg = cfg
        self.fc = cfg.forecast
        self.load_series = load_series
        self.macro = macro
        self.now = now

    async def _bars(self, tickers: Sequence[str]) -> tuple[dict[str, Series], Series | None,
                                                           dict[str, Series]]:
        syms = list(dict.fromkeys([*tickers, "SPY", *self.fc.sector_etfs]))
        got = await self.load_series(syms, et_date(self.now()))
        spy = got.get("SPY")
        etfs = {t: got[t] for t in self.fc.sector_etfs if t in got}
        return {t: got[t] for t in tickers if t in got}, spy, etfs

    async def run(self, tickers: Sequence[str] | None = None, write: bool = True
                  ) -> dict[str, Any]:
        """FC-1, FC-3, FC-4, FC-5 at the latest close."""
        tks = [t.upper() for t in (tickers or self.cfg.universe.tickers)]
        series, spy, etfs = await self._bars(tks)
        ev = load_json(self.fc.eval_file) or {}
        best = (ev.get("volatility") or {})
        ranks = fm.rank_sectors(etfs, spy)
        rank_of = {r["etf"]: r["rank"] for r in ranks}
        gaps = await asyncio.to_thread(fm.gap_stats, series, self.fc.gap_buckets_atr)
        events = macro_events(self.macro)
        out: dict[str, Any] = {}
        for t, s in series.items():
            method = (best.get(t) or {}).get("best", "ewma")
            sec = self.fc.sector_of.get(t)
            out[t] = {"last": round(float(s.c[-1]), 2), "asof": s.dates[-1].isoformat(),
                      "volatility": fm.vol_view(s, method),
                      "gaps": gaps.get(t, {}),
                      "events": fm.event_reactions(s, spy, events, self.fc.min_events),
                      "sector": sec, "sector_rank": rank_of.get(sec or ""),
                      "sectors": len(ranks)}
        payload = {"generated": self.now().isoformat(), "tickers": out, "sectors": ranks,
                   "gaps_all": gaps.get("ALL", {}), "graded": bool(ev)}
        if write:
            _write(self.fc.latest_file, payload)
        return payload

    async def evaluate(self, tickers: Sequence[str] | None = None) -> dict[str, Any]:
        """FC-2 and FC-5 walk-forward, plus FC-4 multipliers for the catalyst outlook."""
        tks = [t.upper() for t in (tickers or self.cfg.universe.tickers)]
        series, spy, etfs = await self._bars(tks)
        vol = {t: await asyncio.to_thread(fm.grade_vol, s) for t, s in series.items()}
        events = macro_events(self.macro)
        mult = {t: {k: v["multiplier"] for k, v in fm.event_reactions(
            s, spy, events, self.fc.min_events).items() if isinstance(v, dict)}
            for t, s in series.items()}
        res = {"generated": self.now().isoformat(), "volatility": vol,
               "event_multipliers": mult, "rotation": fm.grade_rotation(etfs, spy),
               "note": "Volatility graded one session ahead with QLIKE (lower is better) and "
                       "the share of moves inside the 80% range (target 0.80)."}
        _write(self.fc.eval_file, res)
        return res


def render(payload: dict[str, Any], ticker: str | None = None) -> str:
    lines = []
    for t, x in payload["tickers"].items():
        if ticker and t != ticker:
            continue
        v = x.get("volatility") or {}
        lines.append(f"{t} {x['last']} ({x['asof']}) · next session "
                     f"±{v.get('range_1d_pct', 0):.1f}% "
                     f"(80%: {v.get('range_1d', ['?', '?'])[0]}-{v.get('range_1d', ['?', '?'])[1]})"
                     f" · 5 sessions ±{v.get('range_5d_pct', 0):.1f}% · {v.get('method', '-')}")
        ev = {k: e for k, e in (x.get("events") or {}).items() if isinstance(e, dict)
              and e.get("n")}
        if ev:
            lines.append("  macro days: " + ", ".join(
                f"{k} {e['abs_pct']:.2f}% avg move (x{e['multiplier']:.1f}, n={e['n']})"
                for k, e in ev.items()))
        g = x.get("gaps") or {}
        big = {k: d for k, d in g.items() if d["n"] >= 8 and ("1-2" in k or ">2" in k)}
        if big:
            lines.append("  gaps: " + "; ".join(
                f"{k}: fill {d['fill_rate']:.0%}, go {d['go_rate']:.0%} (n={d['n']})"
                for k, d in big.items()))
        if x.get("sector"):
            lines.append(f"  sector {x['sector']} ranks {x.get('sector_rank') or '-'} of "
                         f"{x.get('sectors')}")
    if not ticker and payload.get("sectors"):
        lines.append("Sector leadership (6-1 month momentum + 3-month RS vs SPY): " + ", ".join(
            f"{r['rank']}. {r['etf']}" for r in payload["sectors"]))
    if not payload.get("graded"):
        lines.append("Not graded yet: run `./run.sh forecast-eval` (picks the best volatility "
                     "estimator per stock and measures macro-day reactions).")
    return "\n".join(lines) or "no bars for these tickers"
