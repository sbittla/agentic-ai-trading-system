"""Macro agent MA-1..MA-9, forecasts FC-1..FC-6, technicals TS-20..TS-23
(MACRO_FORECAST_REQUIREMENTS.md)."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.config import Config, validate
from trader.forecast import models as fm
from trader.macro import regime as rg
from trader.macro.sources import parse_fred_csv, parse_rss
from trader.scanner import ta
from trader.scanner import techniques as T
from trader.scanner.techniques import Series

D0 = date(2024, 1, 2)


def sessions(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def walk(sym: str, n: int, seed: int, vol: float = 0.012, drift: float = 0.0,
         vol_regime: dict[int, float] | None = None) -> Series:
    rng = random.Random(seed)
    c, rows = 100.0, []
    for i, d in enumerate(sessions(n)):
        v = (vol_regime or {}).get(i // 50, vol)
        r = rng.gauss(drift, v)
        o = c * (1 + rng.gauss(0, v / 3))
        c = c * (1 + r)
        hi = max(o, c) * (1 + abs(rng.gauss(0, v / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, v / 2)))
        rows.append((d, o, hi, lo, c, 1e6 * (1 + rng.random())))
    return Series.from_rows(sym, rows)


def monthly(start: date, values: list[float]) -> list[tuple[date, float]]:
    out, y, m = [], start.year, start.month
    for v in values:
        out.append((date(y, m, 1), v))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def daily_obs(start: date, values: list[float]) -> list[tuple[date, float]]:
    return list(zip(sessions(len(values), start), values, strict=True))


# --- macro --------------------------------------------------------------------------------

@pytest.mark.rule("MA-1")
@pytest.mark.rule("MA-2")
def test_fred_csv_and_fed_rss_parsers() -> None:
    csv = "observation_date,T10Y2Y\n2026-10-14,0.52\n2026-10-15,.\n2026-10-16,0.55\n"
    assert parse_fred_csv(csv) == [(date(2026, 10, 14), 0.52), (date(2026, 10, 16), 0.55)]
    rss = ("<rss><channel><item><title><![CDATA[Federal Reserve issues FOMC statement]]>"
           "</title><link>https://www.federalreserve.gov/x.htm</link>"
           "<pubDate>Wed, 17 Sep 2025 18:00:00 GMT</pubDate><description>&lt;p&gt;Rates "
           "unchanged&lt;/p&gt;</description></item></channel></rss>")
    items = parse_rss(rss)
    assert items[0]["title"] == "Federal Reserve issues FOMC statement"
    assert items[0]["ts"] == datetime(2025, 9, 17, 18, 0, tzinfo=UTC)


@pytest.mark.rule("MA-3")
def test_release_surprise_is_vs_trend_not_consensus() -> None:
    obs = monthly(date(2025, 1, 1), [100, 100.2, 100.4, 100.6, 100.8, 101.0, 101.2, 102.0])
    r = rg.release_surprise(obs)
    assert r is not None and r["surprise_sd"] is None or r["surprise_sd"] > 3   # sd of 0
    noisy = monthly(date(2025, 1, 1), [100, 100.3, 100.4, 100.8, 100.9, 101.3, 101.4, 102.5])
    r2 = rg.release_surprise(noisy)
    assert r2 is not None and r2["surprise_sd"] > 2 and "not consensus" in r2["basis"]


@pytest.mark.rule("MA-4")
@pytest.mark.parametrize(("head", "theme", "tone"), [
    ("Fed holds rates steady, Powell signals patience", "FED", "neutral"),
    ("Hotter inflation data lifts Treasury yields", "INFLATION", "risk-off"),
    ("Jobless claims fall as labor market stays strong", "JOBS", "neutral"),
    ("Stocks rally after ceasefire deal reached", "GEOPOLITICS", "risk-on"),
    ("New tariffs on chip imports announced", "TRADE", "risk-off"),
    # real headlines from the October 16, 2026 smoke test
    ("Missiles hit Aden airport as Saudi-led coalition intercepts missile", "GEOPOLITICS",
     "risk-off"),
    ("Israeli settlers attack Palestinians during West Bank olive harvest", "GEOPOLITICS",
     "risk-off"),
    ("Wall Street's fear gauge is ripping. Traders see it going even higher", "OTHER",
     "risk-off"),
])
def test_macro_news_themes_and_tone(head: str, theme: str, tone: str) -> None:
    assert rg.theme(head) == theme
    assert rg.tone(head, 0.0) == tone


@pytest.mark.rule("MA-5")
@pytest.mark.rule("MA-6")
def test_regime_indicators_and_label() -> None:
    spy = walk("SPY", 320, 3, drift=-0.002)        # falling market
    asof = spy.dates[-1] + timedelta(days=1)
    stress = {"T10Y3M": daily_obs(D0, [-0.5] * 300), "VIXCLS": daily_obs(D0, [30.0] * 300),
              "VXVCLS": daily_obs(D0, [26.0] * 300),
              "BAMLH0A0HYM2": daily_obs(D0, [3.0] * 250 + [4.5] * 50)}
    reg = rg.regime(stress, spy, asof)
    keys = {i.key: i.signal for i in reg.indicators}
    assert keys["curve"] == -1 and keys["vix"] == -1 and keys["vix_term"] == -1
    assert keys["credit"] == -1 and keys["trend"] == -1
    assert reg.label == "RISK_OFF" and reg.score <= -25
    calm = {"T10Y3M": daily_obs(D0, [1.0] * 300), "VIXCLS": daily_obs(D0, [12.0] * 300),
            "BAMLH0A0HYM2": daily_obs(D0, [4.0] * 250 + [3.0] * 50)}
    up = walk("SPY", 320, 3, drift=0.002)
    assert rg.regime(calm, up, up.dates[-1] + timedelta(days=1)).label == "RISK_ON"


@pytest.mark.rule("MA-6")
def test_regime_uses_only_data_before_asof() -> None:
    spy = walk("SPY", 320, 4)
    obs = daily_obs(D0, [1.0] * 280 + [-1.0] * 20)    # the curve inverts late
    early = spy.dates[250]
    a = rg.regime({"T10Y3M": obs}, spy, early)
    b = rg.regime({"T10Y3M": obs[:250]}, spy, early)
    assert [vars(x) for x in a.indicators] == [vars(x) for x in b.indicators]


@pytest.mark.rule("MA-5")
def test_sahm_rule_and_inflation_trend() -> None:
    un = monthly(date(2024, 1, 1), [3.7] * 12 + [3.9, 4.2, 4.5, 4.6])
    assert rg.sahm(un) is not None and rg.sahm(un) >= 0.5
    core = monthly(date(2024, 1, 1), [100 * 1.002 ** k for k in range(12)]
                   + [100 * 1.002 ** 11 * 1.006 ** k for k in range(1, 5)])
    a3, y = rg.annualised_3m(core), rg.yoy(core)
    assert a3 is not None and y is not None and a3 > y + 0.5


@pytest.mark.rule("MA-9")
def test_regime_evaluation_reports_what_followed() -> None:
    spy = walk("SPY", 500, 5)
    vix = daily_obs(D0, [30.0 if (k // 100) % 2 else 12.0 for k in range(500)])
    res = rg.evaluate({"VIXCLS": vix}, spy, step=10)
    assert res["by_label"] and all(v["n"] > 0 for v in res["by_label"].values())
    assert "separates_volatility" in res


@pytest.mark.rule("MA-7")
@pytest.mark.rule("MA-8")
async def test_macro_agent_stores_regime_and_feeds_catalyst_panel(cfg: Config,
                                                                  tmp_path: Path) -> None:
    from trader.catalyst.context import macro_regime
    from trader.journal import schema as s
    from trader.journal.store import SqlJournal
    from trader.macro.agent import MacroAgent, latest_label, render

    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    j.insert_many(s.macro_series, [{"series_id": "VIXCLS", "date": d, "value": 32.0}
                                   for d in sessions(30, date(2026, 9, 1))])
    j.insert(s.macro_news, {"item_id": "a", "ts": datetime(2026, 10, 16, 13, tzinfo=UTC),
                            "source": "fed", "headline": "Fed issues FOMC statement",
                            "theme": "FED", "tone": "neutral", "sentiment": 0.0,
                            "suspicious": False})
    spy = walk("SPY", 300, 6, drift=-0.002)

    async def load(syms: list[str], until: date) -> dict[str, Series]:
        return {"SPY": spy}
    m = cfg.macro.model_copy(update={"latest_file": str(tmp_path / "m.json")})
    agent = MacroAgent(cfg.model_copy(update={"macro": m}), j, load,
                       lambda: datetime(2026, 10, 16, 21, tzinfo=UTC))
    payload = await agent.run()
    assert payload["regime"]["label"] == "RISK_OFF"
    assert (tmp_path / "m.json").exists()
    txt = render(payload)
    assert "RISK_OFF" in txt and "FOMC statement" in txt and "never an order" in txt
    reg = latest_label(j, date(2026, 10, 16))
    assert reg is not None and reg["label"] == "RISK_OFF"
    chk = macro_regime(reg, "positive")
    assert chk[0]["verdict"] == "against"


# --- forecasts ----------------------------------------------------------------------------

@pytest.mark.rule("FC-1")
@pytest.mark.rule("FC-2")
def test_volatility_estimators_graded_walk_forward() -> None:
    s = walk("X", 700, 7, vol_regime={k: (0.01 if k % 2 == 0 else 0.03) for k in range(20)})
    g = fm.grade_vol(s, start=300, step=2)
    assert g["best"] in fm.ESTIMATORS
    for m in fm.ESTIMATORS:
        e = g["estimators"][m]
        assert e["n"] > 100 and 0.5 < e["coverage_80"] < 0.98
    v = fm.vol_view(s, g["best"])
    assert v is not None and v["range_5d_pct"] > v["range_1d_pct"] > 0
    assert v["range_1d"][0] < float(s.c[-1]) < v["range_1d"][1]


@pytest.mark.rule("FC-1")
def test_forecast_variance_uses_only_past_bars() -> None:
    s = walk("X", 400, 8)
    cut = s.upto(300)
    for m in fm.ESTIMATORS:
        assert fm.forecast_var(s, 299, m) == pytest.approx(fm.forecast_var(cut, 299, m))


@pytest.mark.rule("FC-3")
def test_gap_fill_and_go_rates() -> None:
    days = sessions(40)
    rows = []
    for i, d in enumerate(days):
        if i == 20:
            rows.append((d, 104.0, 105.0, 99.5, 104.5, 1e6))   # gap up that filled and went
        else:
            rows.append((d, 100.0, 101.0, 99.0, 100.0, 1e6))
    st = fm.gap_stats({"X": Series.from_rows("X", rows)})
    key = next(k for k in st["X"] if k.startswith("up"))
    assert st["X"][key] == {"n": 1, "fill_rate": 1.0, "go_rate": 1.0}
    assert "ALL" in st


@pytest.mark.rule("FC-4")
def test_macro_event_multiplier_needs_enough_events() -> None:
    s = walk("X", 300, 9, vol=0.005)
    rows = list(zip(s.dates, s.o, s.h, s.l, s.c, s.v, strict=True))
    fomc = set(s.dates[20:300:30])
    bumped = []
    prev = None
    for d, o, h, lo, c, v in rows:
        if d in fomc and prev is not None:
            c = prev * 1.03                                   # 3% moves on FOMC days
            h, lo = max(h, c), min(lo, c)
        bumped.append((d, o, h, lo, c, v))
        prev = c
    s2 = Series.from_rows("X", bumped)
    r = fm.event_reactions(s2, None, {"fomc": fomc, "cpi": set()}, min_events=5)
    assert r["fomc"]["n"] >= 5 and r["fomc"]["multiplier"] > 2 and r["fomc"]["used"]
    assert r["cpi"]["multiplier"] == 1.0


@pytest.mark.rule("FC-4")
def test_catalyst_event_variance_uses_the_measured_multiplier(cfg: Config) -> None:
    from trader.catalyst.outlook import Pool
    from trader.catalyst.pipeline import Inputs, build_stories, outlook, type_story
    from trader.catalyst.stories import Item

    s = walk("NVDA", 260, 10)
    news = [Item("x", "NVDA", datetime.combine(s.dates[-1], datetime.min.time(), UTC)
                 + timedelta(hours=14), "Nvidia CEO steps down")]
    t = type_story(build_stories(news, cfg.catalyst)[0], cfg.catalyst, Inputs(series={"NVDA": s}))
    now = datetime.combine(s.dates[-1], datetime.min.time(), UTC) + timedelta(hours=21)
    base = Inputs(series={"NVDA": s}, macro=[(3, "FOMC 2026-10-28")])
    loud = Inputs(series={"NVDA": s}, macro=[(3, "FOMC 2026-10-28")],
                  event_mult={"NVDA": {"fomc": 4.0}})
    a = outlook(t, Pool(), base, cfg.catalyst, now)
    b = outlook(t, Pool(), loud, cfg.catalyst, now)
    assert a is not None and b is not None
    wa = {v.h: v.sigma_pct for v in a.views}
    wb = {v.h: v.sigma_pct for v in b.views}
    assert wa[1] == wb[1] and wb[5] > wa[5]          # only horizons that include the event


@pytest.mark.rule("FC-5")
def test_sector_rotation_rank_and_grade() -> None:
    spy = walk("SPY", 400, 11)
    etfs = {f"E{k}": walk(f"E{k}", 400, 20 + k, drift=0.0005 * (k - 3)) for k in range(7)}
    ranks = fm.rank_sectors(etfs, spy)
    assert [r["rank"] for r in ranks] == list(range(1, 8))
    g = fm.grade_rotation(etfs, spy)
    assert g["months"] > 5 and g["avg_top_minus_bottom_pct"] > 0   # persistent trends


@pytest.mark.rule("FC-6")
async def test_forecast_agent_writes_report(cfg: Config, tmp_path: Path) -> None:
    from trader.forecast.agent import ForecastAgent, render

    bars = {t: walk(t, 400, k) for k, t in enumerate(["NVDA", "SPY", "SMH", "XLK"])}

    async def load(syms: list[str], until: date) -> dict[str, Series]:
        return {t: bars[t] for t in syms if t in bars}
    f = cfg.forecast.model_copy(update={"latest_file": str(tmp_path / "f.json"),
                                        "eval_file": str(tmp_path / "fe.json")})
    agent = ForecastAgent(cfg.model_copy(update={"forecast": f}), load, None,
                          lambda: datetime(2026, 10, 16, 21, tzinfo=UTC))
    ev = await agent.evaluate(["NVDA"])
    assert ev["volatility"]["NVDA"]["best"] in fm.ESTIMATORS
    out = await agent.run(["NVDA"])
    assert out["tickers"]["NVDA"]["volatility"]["method"] == ev["volatility"]["NVDA"]["best"]
    assert "NVDA" in render(out) and (tmp_path / "f.json").exists()


# --- technicals ---------------------------------------------------------------------------

@pytest.mark.rule("TS-20")
def test_accumulation_distribution_indicators() -> None:
    n = 60
    h = np.full(n, 11.0)
    lo = np.full(n, 9.0)
    c = np.full(n, 10.9)             # closes near the high: money flowing in
    v = np.full(n, 1000.0)
    cmf = ta.cmf(h, lo, c, v, 20)
    assert math.isnan(cmf[18]) and cmf[-1] == pytest.approx(0.9)
    assert ta.ad_line(h, lo, c, v)[-1] == pytest.approx(0.9 * 1000 * n)
    assert ta.slope(np.arange(30, dtype=np.float64), 20)[-1] == pytest.approx(1.0)


@pytest.mark.rule("TS-20")
@pytest.mark.rule("TS-22")
def test_idea_context_carries_accumulation_and_distribution_days() -> None:
    s = walk("X", 320, 12, drift=0.001)
    spy = walk("SPY", 320, 13)
    x = T.Frame(s, spy, +1)
    i = len(s.c) - 1
    assert x.accumulation(i) in (-1, 0, 1)
    assert not math.isnan(x.spy_dist[i])
    assert x.spy_dist[i] == rg.distribution_days(spy, i)


def _base_series() -> Series:
    rows = []
    days = sessions(300)
    px = 50.0
    for k, d in enumerate(days):
        if k < 230:                       # uptrend into the base
            px *= 1.003
            rows.append((d, px * 0.995, px * 1.01, px * 0.99, px, 2e6))
        elif k < 299:                     # base: ranges shrinking, volume drying up
            w = 0.03 * (1 - (k - 230) / 80)
            vol = 1.6e6 - 1.2e6 * (k - 230) / 69    # drying up
            rows.append((d, px, px * (1 + w), px * (1 - w), px * (1 + w / 3 * (-1) ** k), vol))
        else:                             # breakout on volume
            rows.append((d, px * 1.01, px * 1.08, px * 1.005, px * 1.07, 5e6))
    return Series.from_rows("X", rows)


@pytest.mark.rule("TS-21")
def test_base_breakout_technique() -> None:
    s = _base_series()
    x = T.Frame(s, None, +1)
    cand = T.base_breakout(x, len(s.c) - 1)
    assert cand is not None and "base" in cand.why
    assert cand.stop < float(s.c[-2]) < cand.trigger
    assert T.base_breakout(x, len(s.c) - 2) is None          # no breakout the day before
    assert "BASE_BREAKDOWN" in T.technique_names()


@pytest.mark.rule("TS-23")
def test_new_technicals_have_no_look_ahead() -> None:
    s = _base_series()
    full = T.Frame(s, None, +1)
    cut = T.Frame(s.upto(280), None, +1)
    for i in (260, 279):
        assert full.accumulation(i) == cut.accumulation(i)
        assert (T.base_breakout(full, i) is None) == (T.base_breakout(cut, i) is None)


# --- config -------------------------------------------------------------------------------

@pytest.mark.rule("MA-8")
def test_macro_config_rules(cfg: Config) -> None:
    assert not [p for p in validate(cfg) if p.startswith("macro")]
    bad = cfg.model_copy(update={"macro": cfg.macro.model_copy(update={
        "risk_on_at": -5.0, "series": ("bad id",)})})
    assert len([p for p in validate(bad) if p.startswith("macro")]) == 2
