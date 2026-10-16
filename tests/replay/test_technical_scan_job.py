"""trader-core's after-close technical scan (SCANNER_REQUIREMENTS TS-14)."""

from __future__ import annotations

import asyncio
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


@pytest.mark.rule("TS-18")
@pytest.mark.rule("TS-19")
async def test_live_state_feeds_the_checks_and_the_console_file(tmp_path: Path) -> None:
    import json
    from decimal import Decimal

    from trader.models.messages import Command

    cfg = default_config()
    latest = tmp_path / "technical_scan.json"
    cfg = cfg.model_copy(update={"scanner": cfg.scanner.model_copy(update={
        "eval_file": str(tmp_path / "none.json"), "latest_file": str(latest),
        "min_score": 0})})
    h = Harness(cfg, DAY, tmp_path)
    syms = [*cfg.universe.tickers, "SPY", "SMH"]
    h.engine.history = InMemoryHistory({s: daily(s, DAY, 320, k) for k, s in enumerate(syms)})
    h.engine.equity = h.engine.buying_power = Decimal("25000")
    now = et(DAY, 16, 20)
    h.engine.compliance.add_lock("AAPL", "WASH", now - timedelta(days=1),
                                 now + timedelta(days=20), "test lock")
    ctx = h.engine._scan_context(DAY, now, None, None)
    assert ctx.locks is not None and ctx.locks["AAPL"] and not ctx.locks["MSFT"]
    assert ctx.held == frozenset() and ctx.sizer is not None
    qty, risk_usd, why = ctx.sizer(100.0, 96.0)
    assert qty == 31 and why == "risk"                           # RiskManager.size, 0.5%
    assert await h.engine.job_technical_scan(now) is True
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["source"] == "trader-core" and data["asof"] == DAY.isoformat()
    for d in data["swing"]:
        keys = {c["key"]: c for c in d["checks"]}
        assert keys["locks"]["ok"] is not None and keys["held"]["ok"] is not None
        if d["symbol"] == "AAPL" and d["side"] == "long":
            assert keys["locks"]["ok"] is False and d["verdict"] == "blocked"
    # SCAN from the console or Telegram scans the newest stored session (TS-19)
    latest.unlink()
    reply = await h.engine.handle_command(Command(name="SCAN", sender="console",
                                                  received_at=now, source="console"))
    assert reply.startswith("SCAN started")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if latest.exists():
            break
    assert latest.exists()
