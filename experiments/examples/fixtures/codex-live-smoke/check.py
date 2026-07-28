"""Dependency-free checks for the synthetic Phase 3 Live smoke fixture."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    task = Path("task.txt")
    content = task.read_text(encoding="utf-8")
    if mode == "acceptance":
        return 0 if content == "status=COMPLETE\n" else 1
    if mode == "regression":
        return 0 if task.is_file() and Path("check.py").is_file() else 1
    if mode == "lint":
        return 0 if content.endswith("\n") and content.strip() == content.rstrip() else 1
    if mode == "typecheck":
        return 0 if content in {"status=TODO\n", "status=COMPLETE\n"} else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
