"""Walk-forward backtest (BACKTEST_ENGINE §5, TEST_PLAN BT-1).

Rolling windows: tune only CONFIG_REFERENCE tunables on a coarse grid in-sample, run the
chosen values out-of-sample, report only the concatenated out-of-sample results and flag any
parameter whose best value changes every window (unstable, likely overfit).
"""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path
from typing import Any

from trader.backtest.engine import run_replay
from trader.config import Config
from trader.llm.fakes import RulesLLM
from trader.metrics import summarize
from trader.models.messages import Bar, EventFlags

DEFAULT_GRID: dict[str, list[float]] = {"filters.min_rvol": [1.5, 2.0],
                                        "entries.max_chase_atr5": [0.15, 0.25]}


def with_params(cfg: Config, params: dict[str, float]) -> Config:
    out = cfg
    for key, value in params.items():
        section, field = key.split(".")
        sec = getattr(out, section).model_copy(update={field: value})
        out = out.model_copy(update={section: sec})
    return out


async def walk_forward(cfg: Config, minutes: dict[date, list[Bar]], daily: dict[str, list[Bar]],
                       out_dir: str | Path, *, in_sample: int, out_sample: int,
                       step: int | None = None, grid: dict[str, list[float]] | None = None,
                       news: dict[date, list[EventFlags]] | None = None,
                       seed_minutes: list[Bar] | None = None, min_trades: int = 3
                       ) -> dict[str, Any]:
    days = sorted(minutes)
    grid = grid or DEFAULT_GRID
    step = step or out_sample
    keys = list(grid)
    combos = [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*grid.values())]
    out = Path(out_dir)
    windows: list[dict[str, Any]] = []
    oos_trades: list[dict[str, Any]] = []
    i = 0
    while i + in_sample + out_sample <= len(days):
        ins = days[i:i + in_sample]
        oos = days[i + in_sample:i + in_sample + out_sample]
        best: tuple[float, dict[str, float]] | None = None
        for k, params in enumerate(combos):
            res = await run_replay(with_params(cfg, params), minutes={d: minutes[d] for d in ins},
                                   daily=daily, llm=RulesLLM(), news=news,
                                   seed_minutes=seed_minutes or [],
                                   out_dir=out / f"w{len(windows)}_is_{k}")
            m = res.metrics
            score = m["expectancy_r"] if m["trades"] >= min_trades else float("-inf")
            if best is None or score > best[0]:
                best = (score, params)
        if best is None:
            break
        res = await run_replay(with_params(cfg, best[1]), minutes={d: minutes[d] for d in oos},
                               daily=daily, llm=RulesLLM(), news=news,
                               seed_minutes=seed_minutes or [],
                               out_dir=out / f"w{len(windows)}_oos")
        oos_trades += res.trades
        windows.append({"in_sample": [ins[0].isoformat(), ins[-1].isoformat()],
                        "out_of_sample": [oos[0].isoformat(), oos[-1].isoformat()],
                        "chosen": best[1], "in_sample_expectancy": best[0],
                        "oos": {k: res.metrics[k] for k in ("trades", "expectancy_r",
                                                            "profit_factor", "pnl",
                                                            "max_drawdown_pct")}})
        i += step
    unstable = []
    if len(windows) >= 3:
        for key in keys:
            vals = [w["chosen"][key] for w in windows]
            if all(a != b for a, b in zip(vals, vals[1:], strict=False)):
                unstable.append(key)
    total = summarize(oos_trades)
    passed = (total["expectancy_r"] > 0.1 and total["profit_factor"] >= 1.3
              and total["trades"] >= 100)
    return {"windows": windows, "oos": total, "unstable_params": unstable,
            "pass_bar": {"expectancy_gt_0_1R": total["expectancy_r"] > 0.1,
                         "profit_factor_ge_1_3": total["profit_factor"] >= 1.3,
                         "trades_ge_100": total["trades"] >= 100, "pass": passed}}
