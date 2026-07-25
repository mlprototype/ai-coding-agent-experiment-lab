"""Dependency-free synthetic check used only to exercise the Phase 2 runner."""

from __future__ import annotations

import sys
from pathlib import Path

ALLOWED_GATES = {"acceptance", "regression", "lint", "typecheck"}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED_GATES:
        print("expected one known gate name", file=sys.stderr)
        return 2

    fixture_root = Path(__file__).resolve().parent
    if Path.cwd().resolve() != fixture_root:
        print("gate did not run from the fixture workspace", file=sys.stderr)
        return 3
    if (fixture_root / "fixture.txt").read_text(encoding="utf-8") != "runner smoke\n":
        print("fixture content mismatch", file=sys.stderr)
        return 4

    print(f"{sys.argv[1]} gate passed in {fixture_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

