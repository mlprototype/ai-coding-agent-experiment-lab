Edit only `tag_normalizer.py`.

Implement:

`normalize_tags(tags: list[str]) -> list[str]`

Requirements:

- Strip leading and trailing whitespace from each value.
- Normalize each non-empty value with `str.casefold()`.
- Discard values that are empty after stripping.
- Remove duplicates after normalization.
- Preserve the first-occurrence order of normalized values.
- Do not mutate the input list.
- Do not modify `check.py`.
- Do not add dependencies or access the network.
