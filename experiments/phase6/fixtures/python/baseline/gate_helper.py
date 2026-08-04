"""Dependency-free deterministic Gates for the Python Fixture."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from tag_normalizer import normalize_tags


def _acceptance() -> bool:
    actual = normalize_tags(
        [
            "  Hello World  ",
            "hello__world",
            "Alpha_beta",
            " --Trim-- ",
            "___",
            "ALPHA BETA",
            "x   y",
            "a- _b",
        ]
    )
    return actual == ["hello-world", "alpha-beta", "trim", "x-y", "a--b"]


def _regression() -> bool:
    return normalize_tags([]) == [] and normalize_tags(["plain-tag"]) == [
        "plain-tag"
    ]


def _lint() -> bool:
    content = Path("tag_normalizer.py").read_text(encoding="utf-8")
    lines = content.splitlines()
    return (
        "def normalize_tags(tags: list[str]) -> list[str]:" in content
        and "\t" not in content
        and all(line == line.rstrip() for line in lines)
    )


def _typecheck() -> bool:
    content = Path("tag_normalizer.py").read_text(encoding="utf-8")
    tree = ast.parse(content, filename="tag_normalizer.py")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    return len(functions) == 1 and functions[0].name == "normalize_tags"


GATES = {
    "acceptance": _acceptance,
    "regression": _regression,
    "lint": _lint,
    "typecheck": _typecheck,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in GATES:
        return 2
    return 0 if GATES[sys.argv[1]]() else 1


if __name__ == "__main__":
    raise SystemExit(main())
