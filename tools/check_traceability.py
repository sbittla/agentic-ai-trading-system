"""Fail CI if any requirement ID in docs/TRACEABILITY.md has no test marked with it.

Rows whose Test column starts with "Manual" are checked by people, not CI.
Tests declare coverage with:  @pytest.mark.rule("A7-2")
Usage: python tools/check_traceability.py docs/TRACEABILITY.md tests
"""
import pathlib
import re
import sys

ROW_RE = re.compile(r"^\|\s*`([A-Z]{1,3}\d{0,2}-\d+)`\s*\|(?:[^|]*\|){2}\s*([^|]*)\|", re.MULTILINE)
MARK_RE = re.compile(r"""mark\.rule\(\s*["']([^"']+)["']""")


def main(matrix_path: str, tests_dir: str) -> int:
    text = pathlib.Path(matrix_path).read_text(encoding="utf-8")
    ids = {rid for rid, test in ROW_RE.findall(text) if not test.strip().startswith("Manual")}
    covered: set[str] = set()
    root = pathlib.Path(tests_dir)
    if root.exists():
        for f in root.rglob("test_*.py"):
            covered.update(MARK_RE.findall(f.read_text(encoding="utf-8")))
    missing = sorted(ids - covered)
    print(f"{len(ids)} automated requirement IDs, {len(ids & covered)} covered, {len(missing)} missing")
    if missing:
        print("Missing tests for:", ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
