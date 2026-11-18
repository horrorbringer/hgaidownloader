import { BotError } from "./errors";

export const MAX_EPISODES = 10;

/**
 * Parse "1,3-5" into [1,3,4,5]. An empty value means "let the worker decide"
 * (first MAX_EPISODES of the series).
 */
export function parseEpisodeRange(
  value: string | null | undefined,
  max = MAX_EPISODES,
): number[] | null {
  if (!value || !value.trim()) return null;
  const selected = new Set<number>();
  for (const part of value.split(",")) {
    const match = /^\s*(\d+)(?:\s*-\s*(\d+))?\s*$/.exec(part);
    if (!match) throw new BotError("Use episode numbers like 1,3-5.");
    const start = Number(match[1]);
    const end = Number(match[2] ?? match[1]);
    if (!Number.isInteger(start) || !Number.isInteger(end)) {
      throw new BotError("Use episode numbers like 1,3-5.");
    }
    if (start < 1 || end < start || end - start > 500) {
      throw new BotError("Episode range is invalid or too large.");
    }
    for (let number = start; number <= end; number += 1) {
      selected.add(number);
      if (selected.size > max) {
        throw new BotError(`Max ${max} episodes per request.`);
      }
    }
  }
  return [...selected].sort((a, b) => a - b);
}

/** Compact display form: [1,2,3,5,7] -> "1-3,5,7". */
export function formatRanges(numbers: number[]): string {
  if (!numbers.length) return "none";
  const sorted = [...numbers].sort((a, b) => a - b);
  const parts: string[] = [];
  let start = sorted[0] as number;
  let previous = start;
  for (const number of sorted.slice(1)) {
    if (number === previous + 1) {
      previous = number;
      continue;
    }
    parts.push(start === previous ? `${start}` : `${start}-${previous}`);
    start = number;
    previous = number;
  }
  parts.push(start === previous ? `${start}` : `${start}-${previous}`);
  return parts.join(",");
}
