"""Past-week/month recap and the next-session watchlist (UI-34, ``trader recap``).

Pure functions over daily bars and journal rows, so the same numbers appear in Telegram, the
console and the tests. Nothing here trades: the watchlist names levels to watch; the 4:15 PM
ideas (``trader plan``) remain the system's plans.

Per ticker: returns over 1, 5 and N sessions; strength against QQQ; ATR(14) in %; distance to
the 20-session high in ATR; EMA20/EMA50 position; volume against its 20-session average;
position in the N-session range; analyst actions and news tone in the window; next-session
levels (prior high/low/close, floor-trader pivot, 20-session high/low); and tags:

- ``leader`` / ``laggard``: N-session return at least 3 points better / worse than QQQ
- ``near 20d high``: close within 0.5 ATR of the 20-session high (breakout watch)
- ``oversold``: down 5% or more over 5 sessions and in the bottom 20% of the range
- ``upgraded`` / ``downgraded``: net analyst actions in the window
- ``earnings soon``: an earnings flag in the last journal update (gap risk)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from trader.models.messages import Bar

INDEXES = ("SPY", "QQQ", "SMH")


def _pct(a: float, b: float) -> float | None:
    return (a / b - 1) * 100 if b else None


def _ema(values: Sequence[float], n: int) -> float | None:
    if len(values) < n:
        return None
    e = sum(values[:n]) / n
    k = 2 / (n + 1)
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _atr(bars: Sequence[Bar], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(bars, bars[1:],
                                                                             strict=False)]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def ticker_metrics(bars: Sequence[Bar], qqq: Sequence[Bar], days: int) -> dict[str, Any] | None:
    """Metrics for one ticker from its daily bars (oldest first, last = the as-of session)."""
    if len(bars) < 2:
        return None
    last, prev = bars[-1], bars[-2]
    closes = [b.c for b in bars]
    win = bars[-(days + 1):] if len(bars) > days else bars

    def ret(n: int, series: Sequence[Bar]) -> float | None:
        return _pct(series[-1].c, series[-1 - n].c) if len(series) > n else None
    atr = _atr(bars)
    hi20 = max(b.h for b in bars[-20:])
    lo20 = min(b.l for b in bars[-20:])
    hi_n, lo_n = max(b.h for b in win), min(b.l for b in win)
    vol20 = [b.v for b in bars[-21:-1]]
    piv = (last.h + last.l + last.c) / 3
    m: dict[str, Any] = {
        "close": round(last.c, 2), "ret_1d": ret(1, bars), "ret_5d": ret(5, bars),
        f"ret_{days}d": ret(days, bars),
        "atr14": round(atr, 2) if atr else None,
        "atr_pct": round(atr / last.c * 100, 2) if atr else None,
        "high20": round(hi20, 2), "low20": round(lo20, 2),
        "atr_to_high20": round((hi20 - last.c) / atr, 2) if atr else None,
        "above_ema20": (last.c > e) if (e := _ema(closes, 20)) else None,
        "above_ema50": (last.c > e50) if (e50 := _ema(closes, 50)) else None,
        "volume_ratio": round(last.v / (sum(vol20) / len(vol20)), 2) if vol20 and sum(vol20)
        else None,
        "range_pos": round((last.c - lo_n) / (hi_n - lo_n), 2) if hi_n > lo_n else None,
        "levels": {"prior_high": round(last.h, 2), "prior_low": round(last.l, 2),
                   "prior_close": round(last.c, 2), "pivot": round(piv, 2),
                   "r1": round(2 * piv - last.l, 2), "s1": round(2 * piv - last.h, 2),
                   "high20": round(hi20, 2), "low20": round(lo20, 2)},
        "gap_prev_pct": _pct(last.o, prev.c),
    }
    q_ret = ret(days, qqq) if qqq else None
    mine = m[f"ret_{days}d"]
    m["rs_qqq"] = round(mine - q_ret, 2) if mine is not None and q_ret is not None else None
    for k in ("ret_1d", "ret_5d", f"ret_{days}d", "gap_prev_pct"):
        if m[k] is not None:
            m[k] = round(m[k], 2)
    return m


def tags_for(m: dict[str, Any], days: int, ratings: Counter[str], earnings_soon: bool
             ) -> list[str]:
    tags = []
    rs = m.get("rs_qqq")
    if rs is not None and rs >= 3:
        tags.append("leader")
    elif rs is not None and rs <= -3:
        tags.append("laggard")
    if m.get("atr_to_high20") is not None and m["atr_to_high20"] <= 0.5:
        tags.append("near 20d high")
    if (m.get("ret_5d") or 0) <= -5 and (m.get("range_pos") or 1) <= 0.2:
        tags.append("oversold")
    net = (ratings["upgrade"] + ratings["initiate"] + ratings["pt_raise"]
           - ratings["downgrade"] - ratings["pt_cut"])
    if net >= 2 or ratings["upgrade"]:
        tags.append("upgraded")
    if net <= -2 or ratings["downgrade"]:
        tags.append("downgraded")
    if earnings_soon:
        tags.append("earnings soon")
    return tags


def build_recap(daily: dict[str, list[Bar]], tickers: Sequence[str], days: int,
                ratings_rows: Sequence[dict[str, Any]], news_rows: Sequence[dict[str, Any]],
                earnings_soon: set[str]) -> list[dict[str, Any]]:
    """One row per ticker and index: {"ticker", "kind" (stock/index), metrics, tags,
    ratings counts, latest rating, news count and tone}."""
    qqq = daily.get("QQQ", [])
    rows: list[dict[str, Any]] = []
    for t in list(INDEXES) + [x for x in tickers if x not in INDEXES]:
        m = ticker_metrics(daily.get(t, []), qqq, days)
        if m is None:
            continue
        rr = [r for r in ratings_rows if r["ticker"] == t]
        rc: Counter[str] = Counter(r["action"] for r in rr)
        nn = [r for r in news_rows if r["ticker"] == t and r.get("finbert_score") is not None]
        latest = max(rr, key=lambda r: r["ts"]) if rr else None
        kind = "index" if t in INDEXES else "stock"
        rows.append({
            "ticker": t, "kind": kind, **m,
            "tags": tags_for(m, days, rc, t in earnings_soon) if kind == "stock" else [],
            "ratings": dict(rc),
            "latest_rating": (f"{latest['firm']}: {latest['action'].replace('_', ' ')}"
                              + (f" to {latest['to_rating']}" if latest.get("to_rating") else "")
                              + (f", PT ${latest['pt_new']:g}" if latest.get("pt_new") else ""))
            if latest else None,
            "news_count": len(nn),
            "news_tone": round(sum(r["finbert_score"] for r in nn) / len(nn), 2) if nn else None,
        })
    return rows


def _f(v: float | None, sign: bool = True, nd: int = 1) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"


def recap_text(rows: Sequence[dict[str, Any]], days: int, as_of: date, next_day: date,
               macro: Sequence[str] = (), top: int = 3) -> str:
    """Telegram message: the window in review, then the watchlist for the next session."""
    idx = [r for r in rows if r["kind"] == "index"]
    stocks = [r for r in rows if r["kind"] == "stock"]
    lines = [f"RECAP {days} sessions to {as_of:%a %b %d} · watchlist for {next_day:%a %b %d}"]
    for r in idx:
        lines.append(f"{r['ticker']} {r['close']:.2f} · 1d {_f(r['ret_1d'])}% · 5d "
                     f"{_f(r['ret_5d'])}% · {days}d {_f(r.get(f'ret_{days}d'))}%")
    ranked = sorted((r for r in stocks if r.get("rs_qqq") is not None),
                    key=lambda r: -r["rs_qqq"])
    leaders = [r for r in ranked if r["rs_qqq"] > 0][:top]
    laggards = [r for r in ranked[::-1] if r["rs_qqq"] < 0][:top]
    if leaders:
        lines.append(f"Leaders vs QQQ ({days}d, pts): " + ", ".join(
            f"{r['ticker']} {_f(r['rs_qqq'])}" for r in leaders))
    if laggards:
        lines.append(f"Laggards vs QQQ ({days}d, pts): " + ", ".join(
            f"{r['ticker']} {_f(r['rs_qqq'])}" for r in laggards))
    acts = sum(sum(r["ratings"].values()) for r in stocks)
    if acts:
        ups = sum(r["ratings"].get("upgrade", 0) for r in stocks)
        downs = sum(r["ratings"].get("downgrade", 0) for r in stocks)
        lines.append(f"Analyst actions: {acts} ({ups} upgrades, {downs} downgrades)")
        for r in sorted((r for r in stocks if r["latest_rating"]),
                        key=lambda r: -sum(r["ratings"].values()))[:top]:
            lines.append(f"  {r['ticker']}: {r['latest_rating']}")
    if macro:
        lines.append("Events: " + "; ".join(macro))
    watch = [r for r in stocks if r["tags"]]
    if watch:
        lines.append(f"Watch {next_day:%a}:")
        for r in sorted(watch, key=lambda r: -len(r["tags"]))[:8]:
            lv = r["levels"]
            lines.append(f"  {r['ticker']} {r['close']:.2f} [{', '.join(r['tags'])}] · above "
                         f"{lv['prior_high']:.2f} (prior high) / {lv['high20']:.2f} (20d high) · "
                         f"below {lv['prior_low']:.2f} (prior low) · "
                         f"ATR {_f(r['atr14'], False, 2)}")
    else:
        lines.append("No ticker stands out; trade only the 9:00 plan's setups.")
    lines.append("Levels to watch, not orders; the 9:00 game plan decides. Not advice.")
    return "\n".join(lines)


async def run_recap(cfg: Any, journal: Any, history: Any, cal: Any, macro_cal: Any, days: int,
                    now: Any) -> tuple[list[dict[str, Any]], str]:
    """Fetch daily bars, build the recap, journal it in ``market_recap`` and return
    (rows, Telegram text). Used by ``trader recap`` and the RECAP command."""
    from datetime import timedelta

    from sqlalchemy import select

    from trader.journal import schema as s
    from trader.timeutil import et_date, to_et

    today = et_date(now)
    as_of = today if cal.is_trading_day(today) and to_et(now).hour >= 16 \
        else cal.prev_trading_day(today)
    nxt = cal.next_trading_day(as_of)
    tickers = list(cfg.universe.tickers)
    daily: dict[str, list[Bar]] = {}
    for sym in list(INDEXES) + tickers:
        try:
            daily[sym] = [b for b in await history.daily_bars(
                sym, as_of - timedelta(days=days * 2 + 90), as_of) if et_date(b.ts) <= as_of]
        except Exception:   # noqa: BLE001, S112 - one symbol must not stop the recap
            continue
    since = now - timedelta(days=int(days * 1.5) + 2)
    ratings = journal.query(select(s.analyst_actions).where(s.analyst_actions.c.ts >= since))
    news = journal.query(select(s.news_items).where(s.news_items.c.published_at >= since))
    earn = {r["ticker"] for r in journal.query(select(s.event_flags).where(
        s.event_flags.c.date >= today - timedelta(days=1)))
        if r["flag"] in ("EARNINGS_TODAY", "EARNINGS_3D")}
    rows = build_recap(daily, tickers, days, ratings, news, earn)
    macro: list[str] = []
    if macro_cal is not None:
        if macro_cal.is_fomc(nxt):
            macro.append(f"FOMC {nxt:%a} (afternoon entries off)")
        if macro_cal.is_cpi_or_jobs(nxt):
            macro.append(f"CPI/jobs {nxt:%a} 8:30 (later morning entries)")
        if macro_cal.is_release_1000(nxt):
            macro.append(f"10:00 release {nxt:%a} (entries paused 9:57-10:05)")
    if rows:
        journal.insert_many(s.market_recap, [{"run_at": now, "as_of": as_of,
                                               "ticker": r["ticker"], "metrics_json": r}
                                              for r in rows])
    return rows, recap_text(rows, days, as_of, nxt, macro)
