"""Intentionally incomplete Tag Normalizer implementation."""


def normalize_tags(tags: list[str]) -> list[str]:
    """Return non-empty tags; Acceptance requires additional normalization."""
    return [tag for tag in tags if tag]
