export const DEFAULT_LOG_BUFFER_LIMIT = 5_000;

export function boundedLogLines(
  lines: readonly string[],
  limit = DEFAULT_LOG_BUFFER_LIMIT,
): string[] {
  if (!Number.isInteger(limit) || limit < 1)
    throw new RangeError("Log buffer limit must be positive");
  return lines.slice(Math.max(0, lines.length - limit));
}
