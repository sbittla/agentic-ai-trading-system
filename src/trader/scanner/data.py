"""Load scanner inputs from the Parquet bar store (TS-16)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, time
from pathlib import Path

import pandas as pd

from trader.scanner.evaluate import OHLC
from trader.scanner.techniques import Series

ET = "America/New_York"


def _frame(root: Path, kind: str, symbol: str) -> pd.DataFrame | None:
    path = root / kind / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(ET)
    return df.sort_values("ts")


def load_daily(root: Path, symbols: Iterable[str], kind: str = "daily") -> dict[str, Series]:
    out: dict[str, Series] = {}
    for sym in symbols:
        df = _frame(root, kind, sym)
        if df is None:
            continue
        rows = [(ts.date(), float(o), float(h), float(lo), float(c), float(v))
                for ts, o, h, lo, c, v in zip(df["ts"], df["o"], df["h"], df["l"], df["c"],
                                              df["v"], strict=True)]
        out[sym] = Series.from_rows(sym, rows)
    return out


class MinuteBars:
    """Regular-session (9:30-16:00 ET) minute bars per symbol and day, loaded lazily."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, dict[date, list[OHLC]]] = {}

    def _load(self, sym: str) -> dict[date, list[OHLC]]:
        days: dict[date, list[OHLC]] = defaultdict(list)
        df = _frame(self.root, "minute", sym)
        if df is not None:
            t = df["ts"].dt.time
            df = df[(t >= time(9, 30)) & (t < time(16, 0))]
            for ts, o, h, lo, c in zip(df["ts"], df["o"], df["h"], df["l"], df["c"],
                                       strict=True):
                days[ts.date()].append((float(o), float(h), float(lo), float(c)))
        return dict(days)

    def __call__(self, sym: str, day: date) -> Sequence[OHLC] | None:
        if sym not in self._cache:
            self._cache[sym] = self._load(sym)
        bars = self._cache[sym].get(day)
        return bars if bars and len(bars) >= 300 else None   # skip partial sessions
