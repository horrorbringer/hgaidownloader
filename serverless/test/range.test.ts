import { describe, expect, it } from "vitest";
import { formatRanges, parseEpisodeRange } from "../src/lib/range";

describe("parseEpisodeRange", () => {
  it("returns null for an empty value", () => {
    expect(parseEpisodeRange("")).toBeNull();
    expect(parseEpisodeRange("   ")).toBeNull();
    expect(parseEpisodeRange(null)).toBeNull();
  });

  it("parses single numbers and ranges", () => {
    expect(parseEpisodeRange("1,3-5")).toEqual([1, 3, 4, 5]);
    expect(parseEpisodeRange(" 7 ")).toEqual([7]);
    expect(parseEpisodeRange("2-2")).toEqual([2]);
  });

  it("deduplicates and sorts", () => {
    expect(parseEpisodeRange("5,1,3-4,3")).toEqual([1, 3, 4, 5]);
  });

  it("rejects too many episodes", () => {
    expect(() => parseEpisodeRange("1-11")).toThrow(/Max 10 episodes/);
    expect(() => parseEpisodeRange("1-5,6-10,11")).toThrow(/Max 10 episodes/);
  });

  it("rejects malformed and invalid ranges", () => {
    expect(() => parseEpisodeRange("abc")).toThrow(/like 1,3-5/);
    expect(() => parseEpisodeRange("5-1")).toThrow(/invalid/);
    expect(() => parseEpisodeRange("0")).toThrow(/invalid/);
    expect(() => parseEpisodeRange("1-500")).toThrow(/too large|Max 10/);
  });
});

describe("formatRanges", () => {
  it("compresses consecutive numbers", () => {
    expect(formatRanges([1, 2, 3, 5, 7, 8])).toBe("1-3,5,7-8");
    expect(formatRanges([4])).toBe("4");
    expect(formatRanges([])).toBe("none");
  });
});
