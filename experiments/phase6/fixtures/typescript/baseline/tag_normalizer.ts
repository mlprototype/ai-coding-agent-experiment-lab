/** Intentionally incomplete Tag Normalizer implementation. */
export function normalizeTags(tags: string[]): string[] {
  return tags.filter((tag) => tag.length > 0);
}
