"""trader-core's after-close technical scan (SCANNER_REQUIREMENTS TS-14)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from tests.replay.helpers import Harness, et
from trader.backtest.engine import InMemoryHistory
from trader.config import default_config
from trader.models.messages import Bar

DAY = date(2026, 10, 16)


def daily(sym: str, end: date, n: int, seed: int) -> list[Bar]:
    rng = np.random.default_rng(seed)
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.018, n)))
    out = []
    for k, day in enumerate(days):
        o = c[k - 1] if k else c[0]
        out.append(Bar(symbol=sym, ts=et(day, 9, 30), timeframe="1Day", o=float(o),
                       h=float(max(o, c[k]) * 1.006), l=float(min(o, c[k]) * 0.994),
                       c=float(c[k]), v=int(rng.integers(500_000, 3_000_000))))
    return out


@pytest.mark.rule("TS-14")
async def test_after_close_scan_is_sent(tmp_path: Path) -> None:
    cfg = default_config()
    cfg = cfg.model_copy(update={"scanner": cfg.scanner.model_copy(
        update={"eval_file": str(tmp_path / "none.json")})})
    h = Harness(cfg, DAY, tmp_path)
    syms = [*cfg.universe.tickers, "SPY"]
    h.engine.history = InMemoryHistory({s: daily(s, DAY, 320, k) for k, s in enumerate(syms)})
    assert await h.engine.job_technical_scan(et(DAY, 16, 20)) is True
    sent = [text for kind, text in h.notifier.sent if kind == "scan"]
    assert len(sent) == 1
    assert f"Technical scan | {DAY.isoformat()}" in sent[0]
    assert "SWING IDEAS" in sent[0] and "DAY WATCHLIST" in sent[0]
    assert "not loaded (run scan-eval)" in sent[0]


@pytest.mark.rule("TS-14")
async def test_after_close_scan_waits_for_the_final_bar(tmp_path: Path) -> None:
    cfg = default_config()
    h = Harness(cfg, DAY, tmp_path)
    yesterday = DAY - timedelta(days=1)
    h.engine.history = InMemoryHistory({s: daily(s, yesterday, 320, 1)
                                        for s in [*cfg.universe.tickers, "SPY"]})
    assert await h.engine.job_technical_scan(et(DAY, 16, 20)) is False     # retried later
    assert not [t for k, t in h.notifier.sent if k == "scan"]
