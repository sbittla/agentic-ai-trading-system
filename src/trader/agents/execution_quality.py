"""Execution quality feedback (REQUIREMENTS EXQ-1).

Entry slippage per setup = (average fill - trigger) / R. When a setup's average over its last
``window`` trades is above ``switch_r``, it enters with a passive limit at the trigger; if it is
still above after ``window`` more trades, the setup returns to paper (like RC-3). The state is
rebuilt from the journal at start, so restarts never reset it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Mode = Literal["normal", "passive", "paper"]


@dataclass
class SlippageMonitor:
    switch_r: float = 0.1
    window: int = 10
    history: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    mode: dict[str, Mode] = field(default_factory=dict)
    since_switch: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def slip_r(avg_entry: Decimal, trigger: Decimal, r_per_share: Decimal) -> float | None:
        if r_per_share <= 0:
            return None
        return float((avg_entry - trigger) / r_per_share)

    def record(self, setup: str, slip_r: float | None) -> Mode | None:
        """Add one trade; returns the new mode when it changed."""
        if slip_r is None:
            return None
        h = self.history[setup]
        h.append(slip_r)
        cur = self.mode.get(setup, "normal")
        if cur == "paper":
            return None
        if cur == "passive":
            self.since_switch[setup] = self.since_switch.get(setup, 0) + 1
            if self.since_switch[setup] >= self.window:
                recent = h[-self.window:]
                if sum(recent) / len(recent) > self.switch_r:
                    self.mode[setup] = "paper"
                    return "paper"
            return None
        if len(h) >= self.window and sum(h[-self.window:]) / self.window > self.switch_r:
            self.mode[setup] = "passive"
            self.since_switch[setup] = 0
            return "passive"
        return None

    def mode_of(self, setup: str) -> Mode:
        return self.mode.get(setup, "normal")

    def avg(self, setup: str) -> float | None:
        h = self.history.get(setup) or []
        recent = h[-self.window:]
        return sum(recent) / len(recent) if recent else None

    def summary(self) -> str:
        parts = []
        for setup in sorted(self.history):
            a = self.avg(setup)
            if a is not None:
                parts.append(f"{setup} {a:+.2f}R ({self.mode_of(setup)})")
        return "Entry slippage (EXQ-1): " + (", ".join(parts) or "no trades yet")
