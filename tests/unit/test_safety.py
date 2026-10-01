"""Account and safety SR-1..SR-5, plus the journal/bus restart property (M1)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trader import config as config_mod
from trader.bus import EventBus
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import LimitHit

SRC = Path(__file__).resolve().parents[2] / "src" / "trader"


@pytest.mark.rule("SR-1")
def test_removed_pdt_fields_never_used() -> None:
    for f in SRC.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not attrs & {"daytrade_count", "daytrading_buying_power"}, f


@pytest.mark.rule("SR-2")
def test_leverage_config_capped() -> None:
    cfg = config_mod.default_config()
    assert cfg.risk.max_leverage <= 1.0
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_text(Path(SRC.parents[1] / "config" / "config.example.yaml").read_text(
            encoding="utf-8").replace("max_leverage: 1.0", "max_leverage: 2.0"))


@pytest.mark.rule("SR-3")
def test_only_adapters_import_vendor_clients() -> None:
    """IF-6 / SR-3: alpaca trading classes only in brokers/alpaca_adapter.py; the Ollama HTTP
    client only in llm/ollama_client.py; agents import neither."""
    for f in SRC.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(SRC).as_posix()
        if "alpaca.trading" in text:
            assert rel == "brokers/alpaca_adapter.py", rel
        if rel.startswith("agents/"):
            for banned in ("import httpx", "from alpaca", "import alpaca", "from twilio",
                           "import praw"):
                assert banned not in text, (rel, banned)


@pytest.mark.rule("SR-3")
def test_live_keys_only_in_live_mode() -> None:
    text = (SRC / "services" / "core_main.py").read_text(encoding="utf-8")
    i_live = text.index('if env == "live"')
    assert text.index("ALPACA_LIVE_KEY") > i_live


@pytest.mark.rule("SR-4")
def test_events_journaled_before_delivery(tmp_path: Path) -> None:
    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    bus = EventBus(j, lambda: datetime.now(UTC))
    seen: list[str] = []

    def handler(ev: LimitHit) -> None:
        seen.append(ev.rule)
        assert len(j.rows(s.events)) >= 1   # already written when delivered

    bus.subscribe(LimitHit, handler)   # type: ignore[arg-type]
    bus.publish(LimitHit(rule="LL-3", reason_code="HALT_LL3", detail="x",
                         ts=datetime.now(UTC)))
    import asyncio
    asyncio.run(bus.drain())
    assert seen == ["LL-3"]


@pytest.mark.rule("SR-4")
def test_1000_events_survive_restart(tmp_path: Path) -> None:
    """M1 done-when: publish 1,000 events, restart, rebuilt state matches."""
    db = tmp_path / "j.db"
    t0 = datetime(2026, 10, 16, 14, tzinfo=UTC)
    j = SqlJournal(f"sqlite:///{db.as_posix()}")
    bus = EventBus(j, lambda: t0)
    sent = []
    for i in range(1000):
        ev = LimitHit(rule=f"LL-{i % 11 + 1}", reason_code="X", detail=str(i),
                      ts=t0 + timedelta(seconds=i))
        bus.publish(ev)
        sent.append(ev)
    j.dispose()
    j2 = SqlJournal(f"sqlite:///{db.as_posix()}")
    rebuilt = EventBus(j2, lambda: t0).replay({"LimitHit": LimitHit})
    assert rebuilt == sent


@pytest.mark.rule("SR-5")
def test_config_change_applies_next_trading_day(tmp_path: Path) -> None:
    from trader.services.core_main import effective_config

    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    ex = Path(SRC.parents[1] / "config" / "config.example.yaml").read_text(encoding="utf-8")
    morning = config_mod.from_text(ex)
    now = datetime(2026, 10, 16, 13, tzinfo=UTC)
    assert effective_config(morning, j, now).sha256 == morning.sha256
    edited = config_mod.from_text(ex.replace("max_trades_per_day: 3", "max_trades_per_day: 2"))
    used = effective_config(edited, j, now + timedelta(hours=2))
    assert used.sha256 == morning.sha256 and used.config.risk.max_trades_per_day == 3
    tomorrow = effective_config(edited, j, now + timedelta(days=3))
    assert tomorrow.config.risk.max_trades_per_day == 2
