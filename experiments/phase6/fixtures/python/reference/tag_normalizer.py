"""Reference Tag Normalizer implementation, isolated from Provider workspaces."""

from __future__ import annotations

import re

_SEPARATOR = re.compile(r"[ \t\r\n\f\v_]+")
_ASCII_UPPER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize and de-duplicate tags while preserving first-seen order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        value = raw.strip(" \t\r\n\f\v").translate(_ASCII_UPPER)
        value = _SEPARATOR.sub("-", value).strip("-")
        if value and value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized
