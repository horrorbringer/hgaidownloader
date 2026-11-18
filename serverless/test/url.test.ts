import { describe, expect, it } from "vitest";
import { validateSourceUrl } from "../src/lib/url";

const HOSTS = "huangguoai.com";

describe("validateSourceUrl", () => {
  it("accepts the allowed host over https", () => {
    const url = validateSourceUrl("https://huangguoai.com/video/123", HOSTS);
    expect(url.hostname).toBe("huangguoai.com");
    expect(url.protocol).toBe("https:");
  });

  it("accepts subdomains and adds a missing scheme", () => {
    expect(validateSourceUrl("www.huangguoai.com/video/1", HOSTS).protocol).toBe(
      "https:",
    );
    expect(() => validateSourceUrl("m.huangguoai.com/video/1", HOSTS)).not.toThrow();
  });

  it("rejects unrelated hosts and lookalikes", () => {
    expect(() => validateSourceUrl("https://evil.com/video/1", HOSTS)).toThrow(
      /Only these hosts/,
    );
    expect(() =>
      validateSourceUrl("https://huangguoai.com.evil.com/video/1", HOSTS),
    ).toThrow(/Only these hosts/);
  });

  it("rejects credentials, odd ports and other schemes", () => {
    expect(() =>
      validateSourceUrl("https://user:pass@huangguoai.com/v", HOSTS),
    ).toThrow(/username or password/);
    expect(() =>
      validateSourceUrl("https://huangguoai.com:8443/v", HOSTS),
    ).toThrow(/default HTTP/);
    expect(() => validateSourceUrl("file:///etc/passwd", HOSTS)).toThrow(
      /http\(s\)/,
    );
  });
});
