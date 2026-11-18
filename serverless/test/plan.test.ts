import { describe, expect, it } from "vitest";
import { planEncode } from "../container/src/plan";

describe("planEncode", () => {
  it("keeps high quality for short episodes", () => {
    const plan = planEncode(120, 1280);
    expect(plan.videoKbps).toBe(2500);
    expect(plan.height).toBe(720);
    expect(plan.feasible).toBe(true);
    expect(plan.estimatedBytes).toBeLessThan(45 * 1024 * 1024);
  });

  it("steps down the resolution as duration grows", () => {
    expect(planEncode(300, 1280).height).toBe(480);
    expect(planEncode(600, 1280).height).toBe(360);
    expect(planEncode(1200, 1280).height).toBe(240);
  });

  it("never upscales the source", () => {
    expect(planEncode(120, 480).height).toBe(480);
    expect(planEncode(300, 360).height).toBe(360);
  });

  it("flags episodes that cannot fit in 45 MiB", () => {
    expect(planEncode(1800, 1280).feasible).toBe(false);
    expect(planEncode(1500, 1280).feasible).toBe(false);
    expect(planEncode(1200, 1280).feasible).toBe(true);
  });

  it("always produces a usable plan for unknown durations", () => {
    const plan = planEncode(0, 1280);
    expect(plan.videoKbps).toBeGreaterThan(0);
    expect(plan.feasible).toBe(true);
  });

  it("respects a smaller target", () => {
    const normal = planEncode(600, 1280, 45);
    const smaller = planEncode(600, 1280, 30);
    expect(smaller.videoKbps).toBeLessThan(normal.videoKbps);
  });
});
