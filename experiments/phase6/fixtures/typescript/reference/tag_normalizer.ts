/** Reference Tag Normalizer implementation, isolated from Provider workspaces. */
export function normalizeTags(tags: string[]): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const raw of tags) {
    const value = raw
      .replace(/^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g, "")
      .replace(/[A-Z]/g, (character) => character.toLowerCase())
      .replace(/[ \t\r\n\f\v_]+/g, "-")
      .replace(/^-+|-+$/g, "");
    if (value.length > 0 && !seen.has(value)) {
      normalized.push(value);
      seen.add(value);
    }
  }
  return normalized;
}
