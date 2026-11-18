import { spawn } from "node:child_process";
import { JobCancelled, type JobHandle } from "./handle.js";
import { planEncode, type EncodePlan } from "./plan.js";

export { planEncode };
export type { EncodePlan };

export interface EncodeOptions {
  input: string;
  output: string;
  plan: EncodePlan;
  duration: number;
  handle: JobHandle;
  onProgress?: (percent: number) => void;
}

/** H.264 + AAC MP4 so Telegram can play it inline. */
export async function encodeVideo(options: EncodeOptions): Promise<void> {
  const { plan, duration } = options;
  const maxRate = Math.round(plan.videoKbps * 1.2);
  const bufferSize = Math.round(plan.videoKbps * 2.4);
  const filter = `scale=-2:min(${plan.height}\\,ih)`;

  const args = [
    "-y",
    "-i",
    options.input,
    "-vf",
    filter,
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-profile:v",
    "main",
    "-pix_fmt",
    "yuv420p",
    "-b:v",
    `${plan.videoKbps}k`,
    "-maxrate",
    `${maxRate}k`,
    "-bufsize",
    `${bufferSize}k`,
    "-c:a",
    "aac",
    "-b:a",
    `${plan.audioKbps}k`,
    "-ac",
    "1",
    "-movflags",
    "+faststart",
    "-progress",
    "pipe:1",
    "-nostats",
    "-loglevel",
    "error",
    options.output,
  ];

  const child = spawn("ffmpeg", args, { stdio: ["ignore", "pipe", "pipe"] });
  options.handle.attach(child);

  let stderr = "";
  let pending = "";
  let lastPercent = -1;
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    pending += chunk;
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() ?? "";
    for (const line of lines) {
      const [key, value] = line.trim().split("=");
      if (key !== "out_time_us" && key !== "out_time_ms") continue;
      const microseconds = Number(value);
      if (!Number.isFinite(microseconds) || microseconds <= 0) continue;
      const seconds =
        key === "out_time_us" ? microseconds / 1_000_000 : microseconds / 1000;
      const percent =
        duration > 0
          ? Math.min(99, Math.max(0, Math.round((seconds / duration) * 100)))
          : 0;
      if (percent !== lastPercent) {
        lastPercent = percent;
        options.onProgress?.(percent);
      }
    }
  });
  child.stderr.on("data", (chunk: string) => {
    stderr = `${stderr}${chunk}`.slice(-4000);
  });

  const exitCode = await new Promise<number>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? -1));
  });

  if (options.handle.cancelled) throw new JobCancelled();
  if (exitCode !== 0) {
    const reason = stderr.trim().split(/\r?\n/).slice(-3).join(" ");
    throw new Error(`ffmpeg failed (exit ${exitCode}): ${reason || "unknown error"}`);
  }
}
