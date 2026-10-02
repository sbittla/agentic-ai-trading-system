"""Parquet bar store and the recorded-day library (A2-3, M2, TEST_PLAN RP-1).

- ``BarStore``: 2 years of daily and 60 days of 1-min bars per symbol (``data/bars``).
- ``ParquetHistory``: HistoricalData fake reading the store (replay/backtest).
- ``DayRecorder``: writes each live day's bars, quotes, statuses and news flags to
  ``recordings/YYYY-MM-DD/`` so it can be replayed later.
- ``load_recorded_day``: reads a recorded day back.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd

from trader.models.messages import Bar, EventFlags, Quote, TradingStatus
from trader.timeutil import et_date

COLS = ["symbol", "ts", "o", "h", "l", "c", "v"]


def bars_to_df(bars: Iterable[Bar]) -> pd.DataFrame:
    rows = [{"symbol": b.symbol, "ts": b.ts.astimezone(UTC), "o": b.o, "h": b.h, "l": b.l,
             "c": b.c, "v": b.v} for b in bars]
    return pd.DataFrame(rows, columns=COLS)


def df_to_bars(df: pd.DataFrame, timeframe: str = "1Min") -> list[Bar]:
    out = []
    for row in df.itertuples(index=False):
        r = cast(Any, row)
        ts = pd.Timestamp(r.ts)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        out.append(Bar(symbol=str(r.symbol), ts=ts.to_pydatetime(),
                       timeframe=timeframe,   # type: ignore[arg-type]
                       o=float(r.o), h=float(r.h), l=float(r.l), c=float(r.c), v=int(r.v)))
    return out


def resolve_bars_path(configured: str, base: Path | None = None) -> Path:
    """The bar folder for this host. ``TRADER_BARS_PATH`` wins; a container path such as
    ``/app/data/bars`` maps to ``<repo>/data/bars`` when run outside the container."""
    import os

    path = os.environ.get("TRADER_BARS_PATH") or configured
    if path.startswith("/app/") and (os.name == "nt" or not Path("/app").is_dir()):
        path = path[len("/app/"):]
    p = Path(path)
    return p if p.is_absolute() else (base or Path.cwd()) / p


class BarStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, kind: str, symbol: str) -> Path:
        return self.root / kind / f"{symbol}.parquet"

    def write(self, kind: str, symbol: str, bars: list[Bar], keep_days: int | None = None
              ) -> None:
        path = self._path(kind, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = bars_to_df(bars)
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new]).drop_duplicates(["symbol", "ts"],
                                                                         keep="last")
        new = new.sort_values("ts")
        if keep_days is not None and len(new):
            cutoff = pd.Timestamp(datetime.now(UTC) - timedelta(days=keep_days))
            new = new[pd.to_datetime(new["ts"], utc=True) >= cutoff]
        new.to_parquet(path, index=False)

    def read(self, kind: str, symbol: str) -> list[Bar]:
        path = self._path(kind, symbol)
        if not path.exists():
            return []
        return df_to_bars(pd.read_parquet(path), "1Day" if kind == "daily" else "1Min")

    def symbols(self, kind: str) -> list[str]:
        d = self.root / kind
        return sorted(p.stem for p in d.glob("*.parquet")) if d.exists() else []


class ParquetHistory:
    def __init__(self, store: BarStore) -> None:
        self.store = store
        self._cache: dict[tuple[str, str], list[Bar]] = {}

    def _get(self, kind: str, symbol: str) -> list[Bar]:
        key = (kind, symbol)
        if key not in self._cache:
            self._cache[key] = self.store.read(kind, symbol)
        return self._cache[key]

    async def daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return [b for b in self._get("daily", symbol) if start <= et_date(b.ts) < end]

    async def minute_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        return [b for b in self._get("minute", symbol) if start <= b.ts < end]


class DayRecorder:
    """Records a live day for the replay library."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.bars: list[Bar] = []
        self.quotes: list[Quote] = []
        self.statuses: list[TradingStatus] = []
        self.news: list[EventFlags] = []
        self.day: date | None = None

    def add_bar(self, b: Bar) -> None:
        self.day = self.day or et_date(b.ts)
        self.bars.append(b)

    def add_quote(self, q: Quote) -> None:
        if len(self.quotes) < 2_000_000:
            self.quotes.append(q)

    def add_status(self, s: TradingStatus) -> None:
        self.statuses.append(s)

    def add_news(self, n: EventFlags) -> None:
        self.news.append(n)

    def flush(self) -> Path | None:
        if self.day is None:
            return None
        d = self.root / self.day.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        bars_to_df(self.bars).to_parquet(d / "bars_1m.parquet", index=False)
        if self.quotes:
            pd.DataFrame([q.model_dump() for q in self.quotes]).to_parquet(
                d / "quotes.parquet", index=False)
        (d / "statuses.jsonl").write_text("\n".join(s.model_dump_json() for s in self.statuses),
                                          encoding="utf-8")
        (d / "news.jsonl").write_text("\n".join(n.model_dump_json() for n in self.news),
                                      encoding="utf-8")
        self.bars, self.quotes, self.statuses, self.news, self.day = [], [], [], [], None
        return d


def load_recorded_day(path: str | Path) -> dict[str, Any]:
    d = Path(path)
    bars = df_to_bars(pd.read_parquet(d / "bars_1m.parquet"))
    bars.sort(key=lambda b: (b.ts, b.symbol))
    news = []
    nf = d / "news.jsonl"
    if nf.exists():
        news = [EventFlags.model_validate_json(x) for x in nf.read_text(
            encoding="utf-8").splitlines() if x.strip()]
    statuses = []
    sf = d / "statuses.jsonl"
    if sf.exists():
        statuses = [TradingStatus.model_validate_json(x) for x in sf.read_text(
            encoding="utf-8").splitlines() if x.strip()]
    return {"day": date.fromisoformat(d.name), "bars": bars, "news": news,
            "statuses": statuses}


def dump_json(obj: Any, path: str | Path) -> None:
    Path(path).write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
