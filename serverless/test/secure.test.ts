import { describe, expect, it } from "vitest";
import {
  escapeHtml,
  formatBytes,
  hmacToken,
  parseIdList,
  safeEqual,
  truncate,
} from "../src/lib/secure";

describe("safeEqual", () => {
  it("compares equal and different strings", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
    expect(safeEqual("", "")).toBe(true);
  });
});

describe("hmacToken", () => {
  it("is deterministic and changes with the job id", async () => {
    const first = await hmacToken("secret", "job-1");
    const second = await hmacToken("secret", "job-1");
    const other = await hmacToken("secret", "job-2");
    expect(first).toBe(second);
    expect(first).not.toBe(other);
    expect(first).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("formatters", () => {
  it("escapes Telegram HTML", () => {
    expect(escapeHtml('<b>&"</b>')).toBe("&lt;b&gt;&amp;&quot;&lt;/b&gt;");
  });

  it("formats byte sizes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(45 * 1024 * 1024)).toBe("45.0 MB");
  });

  it("truncates long text", () => {
    expect(truncate("abcdef", 4)).toBe("abc…");
    expect(truncate("abc", 4)).toBe("abc");
  });

  it("parses id lists", () => {
    expect(parseIdList("1, 2, nope, 0, 3")).toEqual([1, 2, 3]);
    expect(parseIdList(undefined)).toEqual([]);
  });
});
