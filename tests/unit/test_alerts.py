"""Lightweight Telegram price alerts (trader.alerts): parsing, engine, store, service, feed."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from trader.alerts.bot import parse_rule, split_command
from trader.alerts.config import AlertsConfig, SignalSettings, normalize_symbols
from trader.alerts.engine import AlertEngine, BarIn
from trader.alerts.feed import (
    AlpacaStream,
    AuthError,
    parse_ts,
    prev_close_from_snapshot,
)
from trader.alerts.service import AlertService
from trader.alerts.store import AlertStore, Rule
from trader.notify.telegram import split_text
from trader.timeutil import et_datetime

DAY = datetime(2026, 10, 7).date()   # a Wednesday
OPEN = et_datetime(DAY, datetime.strptime("09:30", "%H:%M").time())


def at(minute: int) -> datetime:
    return OPEN + timedelta(minutes=minute)


def bar(sym: str, minute: int, o: float, h: float, lo: float, c: float, v: float = 1000
        ) -> BarIn:
    return BarIn(sym, at(minute), o, h, lo, c, v)


def engine(**kw: Any) -> AlertEngine:
    sig = SignalSettings(**kw)
    e = AlertEngine(sig)
    e.start_session(OPEN, {"NVDA": 100.0, "AAPL": 200.0})
    return e


# --- parsing --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("NVDA above 140", ("NVDA", "above", 140.0, False, "")),
    ("nvda > 140", ("NVDA", "above", 140.0, False, "")),
    ("NVDA >140.5", ("NVDA", "above", 140.5, False, "")),
    ("NVDA below $120.5 repeat stop zone", ("NVDA", "below", 120.5, True, "stop zone")),
    ("AAPL up 3%", ("AAPL", "up_pct", 3.0, False, "")),
    ("AAPL down 2", ("AAPL", "down_pct", 2.0, False, "")),
    ("AAPL -2%", ("AAPL", "down_pct", 2.0, False, "")),
    ("AAPL +3%", ("AAPL", "up_pct", 3.0, False, "")),
    ("TSLA 250", ("TSLA", None, 250.0, False, "")),
])
def test_parse_rule(text: str, expected: tuple[Any, ...]) -> None:
    r = parse_rule(text.split())
    assert (r.symbol, r.kind, r.value, r.repeat, r.note) == expected


@pytest.mark.parametrize("text", ["NVDA", "NVDA above", "NVDA above abc", "NVDA up 500%",
                                  "!!! above 3"])
def test_parse_rule_errors(text: str) -> None:
    with pytest.raises(ValueError):
        parse_rule(text.split())


def test_split_command_and_symbols() -> None:
    assert split_command("/add@MyBot nvda > 1") == ("add", ["nvda", ">", "1"])
    assert normalize_symbols(["nvda", "$aapl", "NVDA", "brk.b", "bad symbol!"]) == \
        ["NVDA", "AAPL", "BRK.B"]


# --- price rules ----------------------------------------------------------------------

def test_one_shot_rule_fires_once_and_is_consumed() -> None:
    e = engine()
    e.set_rules([Rule(1, "NVDA", "above", 101.0)])
    assert e.on_trade("NVDA", 100.5, at(1)) == []
    alerts = e.on_trade("NVDA", 101.2, at(2))
    assert len(alerts) == 1 and alerts[0].consumed and alerts[0].rule_id == 1
    assert "crossed above 101.00" in alerts[0].text and "+1.20%" in alerts[0].text
    assert e.on_trade("NVDA", 99.0, at(3)) == [] and e.on_trade("NVDA", 102, at(4)) == []
    assert e.rule_symbols() == {"NVDA"} or not e._rules["NVDA"]


def test_repeat_rule_rearms_after_moving_back() -> None:
    e = engine()
    e.set_rules([Rule(2, "NVDA", "below", 95.0, repeat=True)])
    assert len(e.on_trade("NVDA", 94.9, at(1))) == 1
    assert e.on_trade("NVDA", 94.5, at(2)) == []          # still below: no spam
    assert e.on_trade("NVDA", 95.05, at(3)) == []         # inside the re-arm band
    assert e.on_trade("NVDA", 95.5, at(4)) == []          # re-armed
    assert len(e.on_trade("NVDA", 94.8, at(5))) == 1


def test_percent_rules_use_prior_close() -> None:
    e = engine()
    e.set_rules([Rule(3, "AAPL", "up_pct", 2.0), Rule(4, "AAPL", "down_pct", 1.0)])
    assert e.on_trade("AAPL", 203.0, at(1)) == []
    assert [a.rule_id for a in e.on_trade("AAPL", 204.1, at(2))] == [3]
    assert [a.rule_id for a in e.on_trade("AAPL", 197.9, at(3))] == [4]


def test_set_rules_keeps_armed_state() -> None:
    e = engine()
    r = Rule(5, "NVDA", "above", 101.0, repeat=True)
    e.set_rules([r])
    assert e.on_trade("NVDA", 101.5, at(1))
    e.set_rules([r, Rule(6, "AAPL", "above", 300.0)])   # e.g. another rule was added
    assert e.on_trade("NVDA", 101.6, at(2)) == []        # still fired, not re-armed


def test_bad_print_needs_confirmation() -> None:
    e = engine()
    e.set_rules([Rule(7, "NVDA", "above", 110.0)])
    e.on_trade("NVDA", 100.0, at(1))
    assert e.on_trade("NVDA", 120.0, at(2)) == []        # lone print 20% away: held
    assert e.on_trade("NVDA", 100.1, at(3)) == []
    assert e.state["NVDA"].last == 100.1
    e.on_trade("NVDA", 120.0, at(4))
    assert len(e.on_trade("NVDA", 120.3, at(5))) == 1    # confirmed by the next print


# --- scanner signals ------------------------------------------------------------------

def kinds(alerts: list[Any]) -> list[str]:
    return [a.kind for a in alerts]


def test_gap_orb_and_hod() -> None:
    e = engine(opening_range_min=5, move_step_pct=0, vwap_cross=False, volume_spike=False)
    assert kinds(e.on_bar(bar("NVDA", 0, 103, 104, 102.5, 103.5))) == ["gap"]
    for m in range(1, 5):
        assert e.on_bar(bar("NVDA", m, 103.5, 104.2, 103, 103.8)) == []
    a = e.on_bar(bar("NVDA", 5, 103.8, 104.6, 103.7, 104.5))   # breaks the OR high 104.2
    assert kinds(a) == ["orb_up"]                    # HOD folded into the ORB alert
    assert "opening-range breakout" in a[0].text
    assert kinds(e.on_bar(bar("NVDA", 6, 104.5, 104.7, 104.4, 104.6))) == ["hod"]  # ORB once
    assert e.on_bar(bar("NVDA", 7, 104.6, 104.8, 104.5, 104.7)) == []   # HOD cooldown
    a = e.on_bar(bar("NVDA", 8, 104.6, 104.6, 102.0, 102.1))
    assert kinds(a) == ["orb_down"]


def test_vwap_cross_and_volume_spike() -> None:
    e = engine(move_step_pct=0, orb=False, new_high_low=False, gap=False)
    for m in range(12):
        e.on_bar(bar("AAPL", m, 200, 200.5, 199.5, 200.2, 1000))
    a = e.on_bar(bar("AAPL", 12, 200.2, 200.3, 198.0, 198.5, 6000))
    assert set(kinds(a)) == {"vwap_down", "volume"}
    assert "volume spike 6.0x" in next(x.text for x in a if x.kind == "volume")
    a = e.on_bar(bar("AAPL", 13, 198.5, 201, 198.5, 200.9, 1000))
    assert kinds(a) == ["vwap_up"]


def test_move_steps_once_per_level() -> None:
    e = engine(move_step_pct=2, orb=False, new_high_low=False, vwap_cross=False,
               volume_spike=False, gap=False)
    assert e.on_bar(bar("NVDA", 0, 100, 101, 100, 101)) == []
    assert kinds(e.on_bar(bar("NVDA", 1, 101, 102.2, 101, 102.1))) == ["move_1"]
    assert e.on_bar(bar("NVDA", 2, 102, 102.5, 101, 101.5)) == []
    assert e.on_bar(bar("NVDA", 3, 101.5, 102.5, 101, 102.3)) == []   # level 1 again
    assert kinds(e.on_bar(bar("NVDA", 4, 102, 104.5, 102, 104.2))) == ["move_2"]
    assert kinds(e.on_bar(bar("NVDA", 5, 104, 104, 97, 97.5))) == ["move_-1"]


def test_quiet_warmup_and_extended_hours() -> None:
    e = engine(opening_range_min=1)
    assert e.on_bar(bar("NVDA", -10, 99, 99, 99, 99.5)) == []      # pre-market: price only
    assert e.state["NVDA"].last == 99.5 and e.state["NVDA"].open is None
    assert e.on_bar(bar("NVDA", 0, 103, 104, 103, 104), quiet=True) == []
    assert "gap" in e.state["NVDA"].fired          # remembered: not re-sent after warm-up
    assert e.on_bar(bar("NVDA", 1, 104, 104, 104, 104)) == []
    assert "104.00" in e.quote_line("NVDA") and "+4.00%" in e.quote_line("NVDA")


# --- store ----------------------------------------------------------------------------

def test_store_roundtrip(tmp_path: Any) -> None:
    st = AlertStore(tmp_path / "a.sqlite")
    r = st.add_rule("NVDA", "above", 140, note="breakout")
    assert st.rules() == [r] and "breakout" in r.describe()
    assert st.watchlist() is None
    st.set_watchlist(["AMD", "NVDA"])
    st.close()
    st = AlertStore(tmp_path / "a.sqlite")
    assert st.watchlist() == ["AMD", "NVDA"] and st.rules()[0].symbol == "NVDA"
    assert st.delete_rule(r.id) and not st.delete_rule(r.id)
    with pytest.raises(ValueError):
        st.add_rule("NVDA", "sideways", 1)


# --- service --------------------------------------------------------------------------

class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    async def send(self, text: str, *, kind: str, chat_id: str | None = None) -> bool:
        self.sent.append((text, chat_id))
        return True


class FakeRest:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices

    async def snapshots(self, symbols: Any) -> dict[str, dict[str, Any]]:
        return {s: {"latestTrade": {"p": self.prices[s]},
                    "dailyBar": {"t": "2026-10-07T13:30:00Z", "c": self.prices[s]},
                    "prevDailyBar": {"c": self.prices[s] / 1.01}}
                for s in symbols if s in self.prices}

    async def minute_bars(self, symbols: Any, start: datetime, end: datetime | None = None
                          ) -> dict[str, list[dict[str, Any]]]:
        return {}


class FakeStream:
    def __init__(self) -> None:
        self.bars: set[str] = set()
        self.trades: set[str] = set()
        self.connected = False
        self.last_message = None

    async def set_symbols(self, bars: Any, trades: Any = ()) -> None:
        self.bars, self.trades = set(bars), set(trades)


def service(tmp_path: Any) -> tuple[AlertService, FakeTelegram, FakeStream]:
    tg, stream = FakeTelegram(), FakeStream()
    cfg = AlertsConfig(watchlist=["NVDA", "AAPL"])
    svc = AlertService(cfg, AlertStore(tmp_path / "s.sqlite"),
                       FakeRest({"NVDA": 100.0, "AAPL": 200.0, "AMD": 150.0}),
                       stream, tg, {"42"}, now=lambda: at(30))   # type: ignore[arg-type]
    return svc, tg, stream


async def test_commands_add_list_delete(tmp_path: Any) -> None:
    svc, tg, stream = service(tmp_path)
    await svc.handle_message("telegram:42", "/add NVDA 105")
    assert "Added #1 NVDA ≥ 105" in tg.sent[-1][0] and tg.sent[-1][1] == "42"
    assert stream.trades == {"NVDA"}                      # trades only where a rule needs it
    await svc.handle_message("telegram:42", "AMD < 140 repeat")   # shorthand, new symbol
    assert "#2 AMD ≤ 140 (repeat)" in tg.sent[-1][0]
    assert stream.trades == {"NVDA", "AMD"} and "AMD" in stream.bars
    await svc.handle_message("telegram:42", "/list")
    assert "#1 NVDA" in tg.sent[-1][0] and "#2 AMD" in tg.sent[-1][0]
    await svc.handle_message("telegram:42", "/del 2 9")
    assert "Deleted #2" in tg.sent[-1][0] and "not found: 9" in tg.sent[-1][0]
    assert stream.trades == {"NVDA"}
    await svc.handle_message("telegram:42", "/add NVDA above 90")
    assert "already past that level" in tg.sent[-1][0]
    await svc.handle_message("telegram:42", "/nonsense")
    assert "Unknown command" in tg.sent[-1][0]


async def test_rejects_other_chats(tmp_path: Any) -> None:
    svc, tg, _ = service(tmp_path)
    await svc.handle_message("telegram:666", "/clear")
    assert tg.sent == []


async def test_watchlist_and_price(tmp_path: Any) -> None:
    svc, tg, stream = service(tmp_path)
    await svc.handle_message("telegram:42", "/watch amd")
    assert svc.watchlist() == ["NVDA", "AAPL", "AMD"] and "AMD" in stream.bars
    await svc.handle_message("telegram:42", "/unwatch nvda")
    assert svc.watchlist() == ["AAPL", "AMD"]
    await svc.handle_message("telegram:42", "/price AMD")
    assert tg.sent[-1][0].startswith("AMD 150.00 +1.00%")


async def test_frames_dispatch_and_consume_rule(tmp_path: Any) -> None:
    svc, tg, stream = service(tmp_path)
    svc.engine.start_session(OPEN, {"NVDA": 100.0, "AAPL": 200.0})
    svc.session_close = OPEN + timedelta(hours=6, minutes=30)
    svc.store.add_rule("NVDA", "above", 101)
    await svc.rules_changed()
    await svc.on_messages([{"T": "t", "S": "NVDA", "p": 100.5, "t": "2026-10-07T14:00:00Z"},
                           {"T": "t", "S": "NVDA", "p": 101.3, "t": "2026-10-07T14:00:01Z"}])
    assert "NVDA crossed above 101.00" in tg.sent[-1][0]
    assert svc.store.rules() == [] and stream.trades == set()
    # mute silences, but the alert is still logged
    await svc.handle_message("telegram:42", "/mute 10")
    n = len(tg.sent)
    await svc.on_messages([{"T": "b", "S": "AAPL", "t": "2026-10-07T13:30:00Z", "o": 206,
                            "h": 207, "l": 205, "c": 206.5, "v": 1000}])   # gap + move

    assert len(tg.sent) == n
    assert any(k == "gap" for _, _, k in svc.store.alerts_since(OPEN - timedelta(hours=1)))
    await svc.handle_message("telegram:42", "/status")
    assert "muted" in tg.sent[-1][0] and "alerts today 3" in tg.sent[-1][0]


def test_prev_close_before_and_during_session() -> None:
    snap = {"dailyBar": {"t": "2026-10-06T04:00:00Z", "c": 99.0},
            "prevDailyBar": {"c": 98.0}}
    assert prev_close_from_snapshot(snap, "2026-10-07") == 99.0   # pre-open: yesterday's bar
    assert prev_close_from_snapshot(snap, "2026-10-06") == 98.0


def test_parse_ts_nanoseconds() -> None:
    assert parse_ts("2026-10-07T13:31:00.123456789Z") == \
        datetime(2026, 10, 7, 13, 31, 0, 123456, tzinfo=UTC)
    assert parse_ts("2026-10-07T09:31:00-04:00") == datetime(2026, 10, 7, 13, 31, tzinfo=UTC)


def test_split_text_on_lines() -> None:
    parts = split_text("\n".join(["x" * 30] * 10), limit=100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts).count("x") == 300
    assert split_text("") == [""]


# --- stream ---------------------------------------------------------------------------

class FakeWS:
    def __init__(self, frames: list[list[dict[str, Any]]]) -> None:
        self.frames = [json.dumps(f) for f in frames]
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3600)
        return "[]"


async def test_stream_auth_subscribe_and_stop() -> None:
    ws = FakeWS([[{"T": "success", "msg": "connected"}],
                 [{"T": "success", "msg": "authenticated"}],
                 [{"T": "t", "S": "NVDA", "p": 1.0}]])
    s = AlpacaStream("k", "s", connect=lambda *a, **kw: ws)
    await s.set_symbols(["NVDA", "AAPL"], ["NVDA"])
    got: list[dict[str, Any]] = []

    async def handler(msgs: list[dict[str, Any]]) -> None:
        got.extend(msgs)
        await s.set_symbols(["NVDA"], [])      # live re-subscription
        s.stop()

    await asyncio.wait_for(s.run(handler), 5)
    assert ws.sent[0]["action"] == "auth"
    assert ws.sent[1] == {"action": "subscribe", "bars": ["AAPL", "NVDA"], "trades": ["NVDA"]}
    assert ws.sent[2] == {"action": "unsubscribe", "bars": ["AAPL"], "trades": ["NVDA"]}
    assert got == [{"T": "t", "S": "NVDA", "p": 1.0}]


async def test_stream_bad_keys_raise() -> None:
    ws = FakeWS([[{"T": "success", "msg": "connected"}],
                 [{"T": "error", "code": 402, "msg": "auth failed"}]])
    s = AlpacaStream("k", "s", connect=lambda *a, **kw: ws)
    with pytest.raises(AuthError):
        await asyncio.wait_for(s.run(lambda m: asyncio.sleep(0)), 5)


async def test_run_session_streams_then_sends_summary(tmp_path: Any) -> None:
    tg = FakeTelegram()
    close = OPEN + timedelta(hours=6, minutes=30)
    clock = {"now": close - timedelta(seconds=0.3)}

    class OneFrameStream(FakeStream):
        stopped = asyncio.Event()

        async def run(self, handler: Any) -> None:
            self.connected = True
            await handler([{"T": "b", "S": "NVDA", "t": "2026-10-07T13:30:00Z", "o": 104,
                            "h": 105, "l": 103.5, "c": 104.5, "v": 1000}])
            await self.stopped.wait()

        def stop(self) -> None:
            self.connected = False
            self.stopped.set()

    cfg = AlertsConfig(watchlist=["NVDA"], disconnect_after_close_min=0)
    svc = AlertService(cfg, AlertStore(tmp_path / "r.sqlite"), FakeRest({"NVDA": 100.0}),
                       OneFrameStream(), tg, {"42"}, now=lambda: clock["now"])  # type: ignore[arg-type]
    await asyncio.wait_for(svc.run_session(OPEN, close), 5)
    texts = [t for t, _ in tg.sent]
    assert any("NVDA gapped" in t for t in texts)
    assert texts[-1].startswith("🏁 Close") and "NVDA" in texts[-1]
    assert not svc.in_session


def test_default_watchlist_is_sp500_top20_plus_etfs() -> None:
    syms = AlertsConfig().symbols()
    assert len(syms) == 22 and syms[-2:] == ["SPY", "QQQ"]
    assert {"NVDA", "AAPL", "MSFT", "BRK.B", "JPM"} <= set(syms)
