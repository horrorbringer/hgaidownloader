import { spawn } from "node:child_process";
import { readdir, stat } from "node:fs/promises";
import path from "node:path";
import { JobCancelled, type JobHandle } from "./handle.js";

const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

const PROGRESS_PREFIX = "PROGRESS|";

export interface DownloadProgress {
  percent: number;
  downloadedBytes: number;
  totalBytes: number;
  speed: string;
  eta: string;
}

export interface DownloadOptions {
  url: string;
  referer: string;
  directory: string;
  handle: JobHandle;
  onProgress?: (progress: DownloadProgress) => void;
  timeoutMs?: number;
}

export function formatSpeed(bytesPerSecond: number): string {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "";
  if (bytesPerSecond >= 1024 * 1024) {
    return `${(bytesPerSecond / (1024 * 1024)).toFixed(1)} MB/s`;
  }
  return `${Math.max(1, Math.round(bytesPerSecond / 1024))} KB/s`;
}

function parseProgress(line: string): DownloadProgress | null {
  if (!line.startsWith(PROGRESS_PREFIX)) return null;
  const [downloaded, total, estimate, speed, eta] = line
    .slice(PROGRESS_PREFIX.length)
    .split("|");
  const downloadedBytes = Number(downloaded);
  if (!Number.isFinite(downloadedBytes)) return null;
  const totalBytes =
    Number(total) > 0 ? Number(total) : Number(estimate) > 0 ? Number(estimate) : 0;
  const percent =
    totalBytes > 0
      ? Math.min(99, Math.max(0, Math.floor((downloadedBytes / totalBytes) * 100)))
      : 0;
  return {
    percent,
    downloadedBytes,
    totalBytes,
    speed: formatSpeed(Number(speed)),
    eta: /^\d+$/.test(eta) ? `${eta}s` : "",
  };
}

export async function downloadEpisode(options: DownloadOptions): Promise<string> {
  const { directory, handle } = options;
  const template = path.join(directory, "source.%(ext)s");
  const args = [
    "--no-playlist",
    "--newline",
    "--no-warnings",
    "--no-cache-dir",
    "--concurrent-fragments",
    "8",
    "--retries",
    "5",
    "--fragment-retries",
    "5",
    "--socket-timeout",
    "20",
    "--merge-output-format",
    "mp4",
    "-f",
    "bv*+ba/b",
    "--referer",
    options.referer,
    "--user-agent",
    USER_AGENT,
    "--progress-template",
    `download:${PROGRESS_PREFIX}%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s`,
    "-o",
    template,
    options.url,
  ];

  const child = spawn("yt-dlp", args, { stdio: ["ignore", "pipe", "pipe"] });
  handle.attach(child);

  let stderr = "";
  let pending = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    pending += chunk;
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() ?? "";
    for (const line of lines) {
      const progress = parseProgress(line.trim());
      if (progress) options.onProgress?.(progress);
    }
  });
  child.stderr.on("data", (chunk: string) => {
    stderr = `${stderr}${chunk}`.slice(-4000);
  });

  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    try {
      child.kill("SIGTERM");
    } catch {
      // Already gone.
    }
  }, options.timeoutMs ?? 20 * 60_000);

  const exitCode = await new Promise<number>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? -1));
  }).finally(() => clearTimeout(timeout));

  if (handle.cancelled) throw new JobCancelled();
  if (timedOut) throw new Error("The download timed out");
  if (exitCode !== 0) {
    const reason = stderr.trim().split(/\r?\n/).slice(-3).join(" ");
    throw new Error(`yt-dlp failed (exit ${exitCode}): ${reason || "unknown error"}`);
  }

  const files = await readdir(directory);
  const candidates = files.filter(
    (name) =>
      name.startsWith("source.") &&
      !name.endsWith(".part") &&
      !name.endsWith(".ytdl") &&
      !name.endsWith(".temp"),
  );
  if (!candidates.length) throw new Error("yt-dlp finished without producing a file");

  let best: { file: string; size: number } | null = null;
  for (const name of candidates) {
    const file = path.join(directory, name);
    const info = await stat(file);
    if (!best || info.size > best.size) best = { file, size: info.size };
  }
  if (!best || best.size < 10_000) {
    throw new Error("The downloaded file is empty or too small");
  }
  return best.file;
}
