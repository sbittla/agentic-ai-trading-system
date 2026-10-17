"""Fail CI if any requirement ID in docs/TRACEABILITY.md has no test marked with it.

Rows whose Test column starts with "Manual" are checked by people, not CI.
Rows whose Test column starts with "Planned" are specified but not built yet: they are counted
and listed, not enforced, until the row names a real test.
Tests declare coverage with:  @pytest.mark.rule("A7-2")

Two more checks (SQ-4, SQ-5), when the matrix sits in a docs folder:
- every ID defined in the first column of a table in docs/*REQUIREMENTS*.md has a matrix row,
  so no requirement can be written and forgotten;
- every Planned ID is listed in some epic of docs/BACKLOG.md (directly or in a range such as
  SL-1..SL-13), so nothing specified is left out of the order of work.
Usage: python tools/check_traceability.py docs/TRACEABILITY.md tests
"""
import pathlib
import re
import sys

ROW_RE = re.compile(r"^\|\s*`([A-Z]{1,3}\d{0,2}-\d+)`\s*\|(?:[^|]*\|){2}\s*([^|]*)\|", re.MULTILINE)
MARK_RE = re.compile(r"""mark\.rule\(\s*["']([^"']+)["']""")
DEF_RE = re.compile(r"^\|\s*`([A-Z]{1,3}\d{0,2}-\d+)`\s*\|", re.MULTILINE)
TOKEN_RE = re.compile(r"\b([A-Z]{1,3})-(\d+)(?:\.\.(?:\1-)?(\d+))?\b")


def defined_ids(docs: pathlib.Path) -> dict[str, str]:
    """ID -> the requirements document whose table defines it."""
    out: dict[str, str] = {}
    for f in sorted(docs.glob("*REQUIREMENTS*.md")):
        for rid in DEF_RE.findall(f.read_text(encoding="utf-8")):
            out.setdefault(rid, f.name)
    return out


def backlog_ids(path: pathlib.Path) -> set[str]:
    """Every ID the backlog names, with ranges such as SL-1..SL-13 expanded."""
    out: set[str] = set()
    for prefix, a, b in TOKEN_RE.findall(path.read_text(encoding="utf-8")):
        lo, hi = int(a), int(b or a)
        out.update(f"{prefix}-{n}" for n in range(lo, hi + 1))
    return out


def main(matrix_path: str, tests_dir: str) -> int:
    matrix = pathlib.Path(matrix_path)
    text = matrix.read_text(encoding="utf-8")
    rows = ROW_RE.findall(text)
    planned = sorted(rid for rid, test in rows if test.strip().startswith("Planned"))
    ids = {rid for rid, test in rows if not test.strip().startswith(("Manual", "Planned"))}
    covered: set[str] = set()
    root = pathlib.Path(tests_dir)
    if root.exists():
        for f in root.rglob("test_*.py"):
            covered.update(MARK_RE.findall(f.read_text(encoding="utf-8")))
    missing = sorted(ids - covered)
    print(f"{len(ids)} automated requirement IDs, {len(ids & covered)} covered, "
          f"{len(missing)} missing; {len(planned)} planned (not built)")
    rc = 0
    if missing:
        print("Missing tests for:", ", ".join(missing))
        rc = 1
    listed = {rid for rid, _ in rows}
    unlisted = {rid: doc for rid, doc in defined_ids(matrix.parent).items()
                if rid not in listed}
    if unlisted:                                                        # SQ-4
        print("Defined in a requirements document but missing from the matrix:",
              ", ".join(f"{rid} ({doc})" for rid, doc in sorted(unlisted.items())))
        rc = 1
    backlog = matrix.parent / "BACKLOG.md"
    if backlog.exists() and planned:                                    # SQ-5
        orphans = sorted(set(planned) - backlog_ids(backlog))
        if orphans:
            print("Planned but in no backlog epic:", ", ".join(orphans))
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
