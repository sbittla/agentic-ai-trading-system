"""Model selection from hardware capacity (QW-1, QW-6; docs/HARDWARE.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trader import hardware as hw
from trader.cli import main

# the reference machine: RTX 4060 Laptop 8 GB (0.5 GB used at idle), 32 GB RAM, 24 threads
REFERENCE = hw.Hardware((hw.Gpu("NVIDIA GeForce RTX 4060 Laptop GPU", 8.0, 0.5),), 31.7, 24)


@pytest.mark.rule("QW-6")
def test_reference_machine_gets_8b_and_14b_is_refused() -> None:
    best, _ = hw.recommend(REFERENCE)
    assert best.tag == "qwen3:8b" == hw.DEFAULT_MODEL
    status = {f.profile.tag: f.status for f in hw.assess(REFERENCE)}
    assert status["qwen3:8b"] == "fits"
    assert status["qwen3:14b"] == "cpu-offload"       # would break the 60 s limit
    assert status["qwen3:32b"] == "no"


@pytest.mark.rule("QW-6")
@pytest.mark.parametrize("vram,expected", [(4.0, "qwen3:1.7b"), (6.0, "qwen3:4b"),
                                           (10.0, "qwen3:8b"), (12.0, "qwen3:14b"),
                                           (16.0, "qwen3:14b"), (24.0, "qwen3:32b")])
def test_recommendation_scales_with_vram(vram: float, expected: str) -> None:
    machine = hw.Hardware((hw.Gpu("gpu", vram, 0.5),), 64.0, 16)
    assert hw.recommend(machine)[0].tag == expected


def test_no_nvidia_gpu_falls_back_to_cpu_dev_model() -> None:
    best, why = hw.recommend(hw.Hardware((), 32.0, 8))
    assert best.tag == "qwen3:4b" and "development" in why
    assert hw.recommend(hw.Hardware((), 8.0, 4))[0].tag == "qwen3:1.7b"


def test_parse_nvidia_smi_and_largest_gpu() -> None:
    gpus = hw.parse_nvidia_smi("NVIDIA GeForce RTX 4060 Laptop GPU, 8188, 512\nbad line\n")
    assert gpus == (hw.Gpu("NVIDIA GeForce RTX 4060 Laptop GPU", 8.0, 0.5),)
    assert hw.Hardware(gpus + (hw.Gpu("big", 24.0, 0),), None, None).gpu.name == "big"


def _files(root: Path, env_model: str, cfg_model: str) -> None:
    (root / "docker").mkdir()
    (root / "config").mkdir()
    (root / "docker" / ".env").write_text(f"PG_DATABASE=trader\nOLLAMA_MODEL={env_model}\n")
    (root / "config" / "config.yaml").write_text(
        "mode:\n  model: keep-me\nllm:\n  provider: ollama\n"
        f"  model: {cfg_model}   # comment kept\n  temperature: 0\n")


@pytest.mark.rule("QW-6")
def test_select_model_apply_writes_both_files(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    _files(tmp_path, "qwen3:14b", "qwen3:8b")
    rc = main(["select-model", "--vram", "8", "--ram", "32", "--root", str(tmp_path),
               "--apply"])
    out = capsys.readouterr().out
    assert rc == 0 and "recommended: qwen3:8b" in out and "WARNING: the two differ" in out
    assert hw.configured_models(tmp_path) == ("qwen3:8b", "qwen3:8b")
    cfg = (tmp_path / "config" / "config.yaml").read_text()
    assert "  model: keep-me" in cfg and "model: qwen3:8b   # comment kept" in cfg


@pytest.mark.rule("QW-6")
def test_select_model_refuses_model_that_spills_to_cpu(tmp_path: Path) -> None:
    _files(tmp_path, "qwen3:8b", "qwen3:8b")
    args = ["select-model", "--vram", "8", "--ram", "32", "--root", str(tmp_path),
            "--model", "qwen3:14b", "--apply"]
    assert main(args) == 1
    assert hw.configured_models(tmp_path) == ("qwen3:8b", "qwen3:8b")
    assert main([*args, "--force"]) == 0
    assert hw.configured_models(tmp_path) == ("qwen3:14b", "qwen3:14b")


def test_example_files_agree_on_the_default_model() -> None:
    root = Path(__file__).resolve().parents[2]
    env = (root / "docker" / ".env.example").read_text(encoding="utf-8")
    cfg = (root / "config" / "config.example.yaml").read_text(encoding="utf-8")
    assert f"OLLAMA_MODEL={hw.DEFAULT_MODEL}" in env
    assert hw.set_yaml_llm_model(cfg, hw.DEFAULT_MODEL) == cfg.rstrip("\n") + "\n"
