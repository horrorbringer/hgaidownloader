import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export interface MediaInfo {
  duration: number;
  width: number;
  height: number;
  videoCodec: string;
}

/**
 * Same acceptance rule as the desktop app: a video stream must exist and the
 * container must report a real duration. A non-empty file is not enough.
 */
export async function probeMedia(file: string): Promise<MediaInfo> {
  const { stdout } = await execFileAsync(
    "ffprobe",
    [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=codec_name,width,height:format=duration",
      "-of",
      "json",
      file,
    ],
    { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 },
  );

  const data = JSON.parse(stdout) as {
    streams?: Array<{ codec_name?: string; width?: number; height?: number }>;
    format?: { duration?: string };
  };
  const stream = data.streams?.[0];
  const duration = Number(data.format?.duration ?? 0);
  if (!stream || !Number.isFinite(duration) || duration < 1) {
    throw new Error("The downloaded file is not a playable video");
  }
  return {
    duration,
    width: Number(stream.width ?? 0),
    height: Number(stream.height ?? 0),
    videoCodec: String(stream.codec_name ?? "unknown"),
  };
}
