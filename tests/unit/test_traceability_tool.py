"""SQ-4, SQ-5: the traceability tool's two-way and backlog checks (tools/check_traceability.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _tool():   # noqa: ANN202
    spec = importlib.util.spec_from_file_location("check_traceability",
                                                  ROOT / "tools/check_traceability.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(d: Path, matrix: str, req: str, backlog: str | None) -> None:
    (d / "docs").mkdir()
    (d / "tests").mkdir()
    (d / "tests/test_x.py").write_text('@pytest.mark.rule("AB-1")\n', encoding="utf-8")
    (d / "docs/TRACEABILITY.md").write_text(matrix, encoding="utf-8")
    (d / "docs/X_REQUIREMENTS.md").write_text(req, encoding="utf-8")
    if backlog is not None:
        (d / "docs/BACKLOG.md").write_text(backlog, encoding="utf-8")


ROW = "| `{}` | text | owner | {} | — |\n"


@pytest.mark.rule("SQ-4")
def test_a_requirement_missing_from_the_matrix_fails(tmp_path: Path, capsys) -> None:   # noqa: ANN001
    tool = _tool()
    _write(tmp_path, ROW.format("AB-1", "tests/test_x.py"),
           "| `AB-1` | P0 | built |\n| `AB-2` | P1 | forgotten |\n", None)
    assert tool.main(str(tmp_path / "docs/TRACEABILITY.md"), str(tmp_path / "tests")) == 1
    assert "AB-2 (X_REQUIREMENTS.md)" in capsys.readouterr().out
    assert tool.defined_ids(tmp_path / "docs") == {"AB-1": "X_REQUIREMENTS.md",
                                                    "AB-2": "X_REQUIREMENTS.md"}


@pytest.mark.rule("SQ-5")
def test_planned_ids_must_be_in_a_backlog_epic(tmp_path: Path, capsys) -> None:   # noqa: ANN001
    tool = _tool()
    matrix = ROW.format("AB-1", "tests/test_x.py") + "".join(
        ROW.format(f"CD-{n}", "Planned") for n in range(1, 6))
    req = "| `AB-1` | P0 | x |\n" + "".join(f"| `CD-{n}` | P1 | x |\n" for n in range(1, 6))
    _write(tmp_path, matrix, req, "| B1 | epic | CD-1..CD-3, CD-5 | — |\n")
    assert tool.main(str(tmp_path / "docs/TRACEABILITY.md"), str(tmp_path / "tests")) == 1
    assert "Planned but in no backlog epic: CD-4" in capsys.readouterr().out
    (tmp_path / "docs/BACKLOG.md").write_text("| B1 | epic | CD-1..CD-5 | — |\n",
                                              encoding="utf-8")
    assert tool.main(str(tmp_path / "docs/TRACEABILITY.md"), str(tmp_path / "tests")) == 0
    assert tool.backlog_ids(tmp_path / "docs/BACKLOG.md") >= {"CD-1", "CD-3", "CD-5"}
    assert tool.backlog_ids(tmp_path / "docs/BACKLOG.md") & {"CD-6"} == set()
