"""Deterministic synthetic market days for offline replays and tests.

Not a substitute for recorded days (TEST_PLAN RP-1): it exists so the whole pipeline can be
exercised without keys or network. Day types (trend up/down, chop, gap up on news, gap down)
are seeded, so the same seed always produces identical bars.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, time, timedelta

from trader.calendar import MarketCalendar
from trader.models.messages import Bar, EventFlags
from trader.timeutil import et_datetime

BASE_PRICES = {"NVDA": 190.0, "AAPL": 250.0, "GOOGL": 240.0, "MSFT": 510.0, "AMZN": 230.0,
               "TSM": 290.0, "SPCX": 180.0, "META": 760.0, "AVGO": 340.0, "TSLA": 440.0,
               "SPY": 660.0, "QQQ": 600.0, "SMH": 310.0}
IEX_DAILY_VOL = {"NVDA": 4_000_000, "AAPL": 1_500_000, "GOOGL": 900_000, "MSFT": 700_000,
                 "AMZN": 1_200_000, "TSM": 500_000, "SPCX": 600_000, "META": 500_000,
                 "AVGO": 600_000, "TSLA": 2_500_000, "SPY": 3_000_000, "QQQ": 2_000_000,
                 "SMH": 300_000}
DAY_TYPES = ("trend_up", "chop", "trend_down", "gap_up_news", "chop", "trend_up", "gap_down")


@dataclass
class SyntheticData:
    daily: dict[str, list[Bar]] = field(default_factory=dict)
    minutes: dict[date, list[Bar]] = field(default_factory=dict)
    news: dict[date, list[EventFlags]] = field(default_factory=dict)
    day_types: dict[date, dict[str, str]] = field(default_factory=dict)
    seed_minutes: list[Bar] = field(default_factory=list)   # warm-up sessions (RVOL, ATR5)


def _profile(m: int) -> float:
    """U-shaped intraday volume profile (minute 0..389)."""
    x = m / 389
    return 0.6 + 2.2 * (1 - x) ** 6 + 1.4 * x ** 8


def generate(start: date, n_days: int, seed: int = 7, symbols: list[str] | None = None,
             history_days: int = 150, calendar: MarketCalendar | None = None,
             warmup_days: int = 20) -> SyntheticData:
    """``warmup_days`` sessions before ``start`` get minute bars too (returned in
    ``seed_minutes``) so time-of-day RVOL and ATR(5-min) have history on day one."""
    cal = calendar or MarketCalendar()
    if warmup_days:
        first = start if cal.is_trading_day(start) else cal.next_trading_day(start)
        w0 = first
        for _ in range(warmup_days):
            w0 = cal.prev_trading_day(w0)
        full = generate(w0, warmup_days + n_days, seed, symbols, history_days, cal, 0)
        warm = sorted(full.minutes)[:warmup_days]
        out = SyntheticData(daily=full.daily)
        for d in sorted(full.minutes)[warmup_days:]:
            out.minutes[d] = full.minutes[d]
            out.news[d] = full.news[d]
            out.day_types[d] = full.day_types[d]
        out.seed_minutes = [b for d in warm for b in full.minutes[d]
                            if b.ts >= et_datetime(d, time(9, 30)).astimezone(UTC)]
        return out
    rng = random.Random(seed)
    syms = symbols or list(BASE_PRICES)
    out = SyntheticData()
    # history sessions before start
    hist: list[date] = []
    d = start
    while len(hist) < history_days:
        d = cal.prev_trading_day(d)
        hist.append(d)
    hist.reverse()
    days: list[date] = []
    d = start if cal.is_trading_day(start) else cal.next_trading_day(start)
    while len(days) < n_days:
        days.append(d)
        d = cal.next_trading_day(d)
    price = {s: BASE_PRICES.get(s, 100.0) * (0.85 + 0.1 * rng.random()) for s in syms}
    for s in syms:
        out.daily[s] = []
    for hd in hist:   # daily random walk with a mild up-drift
        for s in syms:
            p0 = price[s]
            r = rng.gauss(0.0007, 0.017)
            c = p0 * (1 + r)
            h = max(p0, c) * (1 + abs(rng.gauss(0, 0.006)))
            lo = min(p0, c) * (1 - abs(rng.gauss(0, 0.006)))
            v = int(IEX_DAILY_VOL.get(s, 500_000) * (0.7 + 0.6 * rng.random()))
            out.daily[s].append(Bar(symbol=s, ts=et_datetime(hd, time(9, 30)).astimezone(UTC),
                                    timeframe="1Day", o=round(p0, 2), h=round(h, 2),
                                    l=round(lo, 2), c=round(c, 2), v=v))
            price[s] = c
    for dd in days:
        session = cal.session(dd)
        n_min = int((session.close - session.open).total_seconds() // 60)
        market = rng.choice(("trend_up", "chop", "trend_up", "trend_down", "chop"))
        types: dict[str, str] = {}
        bars: list[Bar] = []
        news: list[EventFlags] = []
        for s in syms:
            if s in ("SPY", "QQQ", "SMH"):
                kind = market
            else:
                kind = market if rng.random() < 0.45 else rng.choice(DAY_TYPES)
            types[s] = kind
            gap = {"gap_up_news": rng.uniform(0.012, 0.03), "gap_down": -rng.uniform(0.012,
                                                                                    0.025)
                   }.get(kind, rng.gauss(0.0, 0.004))
            prev = price[s]
            p = prev * (1 + gap)
            open_px = p
            sig = 0.0007
            daily_vol = IEX_DAILY_VOL.get(s, 500_000)
            vol_mult = {"trend_up": 2.4, "gap_up_news": 3.0, "trend_down": 1.8,
                        "gap_down": 2.0}.get(kind, 0.6)
            drift = {"trend_up": 0.00028, "gap_up_news": 0.00032, "trend_down": -0.00025,
                     "gap_down": -0.0001}.get(kind, 0.0)
            # pre-market bars (8:00-9:29), thin
            pm_p = prev
            for m in range(-90, 0, 3):
                ts = et_datetime(dd, time(9, 30)) + timedelta(minutes=m)
                step = (p - prev) / 30
                o = pm_p
                pm_p = pm_p + step + pm_p * rng.gauss(0, sig / 2)
                bars.append(Bar(symbol=s, ts=ts.astimezone(UTC), o=round(o, 2),
                                h=round(max(o, pm_p) * 1.0004, 2),
                                l=round(min(o, pm_p) * 0.9996, 2), c=round(pm_p, 2),
                                v=int(daily_vol * 0.0004 * (1 + rng.random())), source="iex"))
            prev_c = open_px
            for m in range(n_min):
                phase = 1.6 if m < 60 or m >= n_min - 60 else 0.5
                mr = -0.04 * (prev_c - open_px) / open_px if kind == "chop" else 0.0
                z = rng.gauss(0, 1)
                r = drift * phase + mr + sig * z
                o = prev_c
                c = o * (1 + r)
                h = max(o, c) * (1 + abs(rng.gauss(0, sig * 0.5)))
                lo = min(o, c) * (1 - abs(rng.gauss(0, sig * 0.5)))
                vol = daily_vol / 390 * _profile(m) * vol_mult * (1 + 0.6 * abs(z))
                ts = et_datetime(dd, time(9, 30)) + timedelta(minutes=m)
                bars.append(Bar(symbol=s, ts=ts.astimezone(UTC), o=round(o, 2), h=round(h, 2),
                                l=round(lo, 2), c=round(c, 2), v=max(1, int(vol)), source="iex"))
                prev_c = c
            day_bars = [b for b in bars if b.symbol == s and b.ts >= et_datetime(
                dd, time(9, 30)).astimezone(UTC)]
            out.daily[s].append(Bar(symbol=s, ts=et_datetime(dd, time(9, 30)).astimezone(UTC),
                                    timeframe="1Day", o=day_bars[0].o,
                                    h=max(b.h for b in day_bars), l=min(b.l for b in day_bars),
                                    c=day_bars[-1].c, v=sum(b.v for b in day_bars)))
            price[s] = prev_c
            if s not in ("SPY", "QQQ", "SMH"):
                flags: tuple[str, ...] = ()
                if rng.random() < 0.03:
                    flags = ("EARNINGS_3D",)
                catalyst = (f"{s} announces a major product and supply agreement"
                            if kind == "gap_up_news" else "none")
                news.append(EventFlags(
                    ticker=s, updated_at=et_datetime(dd, time(7, 15)).astimezone(UTC),
                    flags=flags, summary=catalyst if catalyst != "none" else "",
                    catalyst=catalyst, tone="positive" if kind == "gap_up_news" else "neutral",
                    sentiment=0.6 if kind == "gap_up_news" else 0.0))
        bars.sort(key=lambda b: (b.ts, b.symbol))
        out.minutes[dd] = bars
        out.news[dd] = news
        out.day_types[dd] = types
    return out
