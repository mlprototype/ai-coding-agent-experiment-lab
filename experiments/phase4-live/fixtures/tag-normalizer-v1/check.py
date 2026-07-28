import inspect
import sys
from collections.abc import Callable
from typing import get_type_hints

import tag_normalizer


def check_acceptance() -> None:
    tags = [
        "  Alpha ",
        "BETA",
        "",
        " alpha",
        "Straße",
        " STRASSE ",
        "  ",
        "Beta",
        "Gamma",
    ]
    expected = ["alpha", "beta", "strasse", "gamma"]
    actual = tag_normalizer.normalize_tags(tags)
    if actual != expected:
        raise AssertionError(f"unexpected normalized tags: {actual!r}")


def check_regression() -> None:
    normalize_tags = getattr(tag_normalizer, "normalize_tags", None)
    if not callable(normalize_tags):
        raise AssertionError("public normalize_tags function is missing")
    tags = [" Alpha ", "BETA", "alpha"]
    original = tags.copy()
    normalize_tags(tags)
    if tags != original:
        raise AssertionError("normalize_tags mutated its input list")


def check_typecheck() -> None:
    normalize_tags = tag_normalizer.normalize_tags
    signature = inspect.signature(normalize_tags)
    if list(signature.parameters) != ["tags"]:
        raise AssertionError("normalize_tags must have exactly one parameter named tags")
    hints = get_type_hints(normalize_tags)
    if hints.get("tags") != list[str]:
        raise AssertionError("tags must be annotated as list[str]")
    if hints.get("return") != list[str]:
        raise AssertionError("return type must be annotated as list[str]")


CHECKS: dict[str, Callable[[], None]] = {
    "acceptance": check_acceptance,
    "regression": check_regression,
    "typecheck": check_typecheck,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in CHECKS:
        return 2
    CHECKS[argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
