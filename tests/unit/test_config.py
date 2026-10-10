"""Config loader and validation (CONFIG_REFERENCE CF-4, M0 done criterion, SR-5, SEC-8)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trader import config as config_mod
from trader.config import ConfigError

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "config.example.yaml"


def _with(tmp_path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(data)
    (tmp_path / "config").mkdir(exist_ok=True)
    p = tmp_path / "config" / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp_path / "config" / "macro_calendar_2026_2027.yaml").write_text(
        (ROOT / "config" / "macro_calendar_2026_2027.yaml").read_text(encoding="utf-8"),
        encoding="utf-8")
    return p


def test_example_config_is_valid() -> None:
    loaded = config_mod.load(EXAMPLE)
    assert loaded.config.universe.tickers[0] == "NVDA"
    assert len(loaded.sha256) == 64


@pytest.mark.rule("SR-5")
def test_bad_risk_value_fails_with_rule_name(tmp_path: Path) -> None:
    """M0 done-when: risk_per_trade_pct: 2 fails validate-config naming the rule."""
    p = _with(tmp_path, lambda d: d["risk"].__setitem__("risk_per_trade_pct", 2))
    with pytest.raises(ConfigError) as e:
        config_mod.load(p)
    assert any("risk.risk_per_trade_pct <= 1.0" in x for x in e.value.problems)


@pytest.mark.parametrize("mutate,rule", [
    (lambda d: d["windows"]["morning"].__setitem__("entries_from", "09:32"),
     "windows.morning.entries_from >= 09:35"),
    (lambda d: d["windows"]["afternoon"].__setitem__("swing_entries_until", "15:55"),
     "swing_entries_until <= 15:50"),
    (lambda d: d["risk"].__setitem__("daily_loss_limit_pct", 4),
     "risk.daily_loss_limit_pct <= 3.0"),
    (lambda d: d["risk"].__setitem__("max_leverage", 2), "risk.max_leverage <= 1.0"),
    (lambda d: d["filters"].__setitem__("min_rvol", 0.5), "filters.min_rvol >= 1.0"),
    (lambda d: d["entries"].__setitem__("min_reward_risk", 1.0), "entries.min_reward_risk"),
    (lambda d: d["entries"].__setitem__("min_confidence", 0.3), "entries.min_confidence"),
    (lambda d: d["exits"]["day"].__setitem__("t1_fraction", 1.0), "t1_fraction"),
    (lambda d: d["wash_sale"].__setitem__("lock_days", 20), "wash_sale.lock_days >= 31"),
    (lambda d: d["data"].__setitem__("sec_user_agent", "no email here"), "sec_user_agent"),
    (lambda d: d["llm"].__setitem__("temperature", 0.7), "llm.temperature = 0"),
    (lambda d: d["llm"]["prompt_versions"].__setitem__("a06", "v99"), "prompt file missing"),
    (lambda d: d["notifications"].__setitem__("approval_timeout_sec", 300),
     "approval_timeout_sec <= 120"),
    (lambda d: d["services"]["notifier_poll_sec"].__setitem__("approval", 10),
     "notifier_poll_sec.approval <= 5"),
    (lambda d: d["universe"].__setitem__("tickers", [f"T{i}" for i in range(11)]),
     "1-10 tickers"),
    (lambda d: d["universe"]["tickers"].append("GOOG"), "one share class"),
])
@pytest.mark.rule("TW-1")
def test_each_validation_rule(tmp_path: Path, mutate, rule: str) -> None:  # type: ignore[no-untyped-def]
    p = _with(tmp_path, mutate)
    with pytest.raises(ConfigError) as e:
        config_mod.load(p)
    assert any(rule in x for x in e.value.problems), e.value.problems


def test_auto_mode_needs_gate(tmp_path: Path) -> None:
    p = _with(tmp_path, lambda d: d["mode"].__setitem__("day_trades", "auto"))
    with pytest.raises(ConfigError):
        config_mod.load(p, gates_passed=set())
    assert config_mod.load(p, gates_passed={"auto_day"})


@pytest.mark.rule("SR-5")
def test_config_is_frozen() -> None:
    """DV-3 / A7-3: no agent can change a limit at runtime."""
    cfg = config_mod.load(EXAMPLE).config
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        cfg.risk.risk_per_trade_pct = 5   # type: ignore[misc]


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    p = _with(tmp_path, lambda d: d["risk"].__setitem__("yolo_mode", True))
    with pytest.raises(ConfigError):
        config_mod.load(p)


def test_cli_validate_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trader.cli import main

    p = _with(tmp_path, lambda d: d["risk"].__setitem__("risk_per_trade_pct", 2))
    assert main(["validate-config", "--config", str(p)]) == 1
    assert "risk.risk_per_trade_pct <= 1.0" in capsys.readouterr().out
    assert main(["validate-config", "--config", str(EXAMPLE)]) == 0
