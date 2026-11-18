/**
 * Pure bitrate planner so it can be unit tested on the worker side.
 * Telegram bot uploads are capped at 50 MB; we aim a little lower.
 */

export const MIN_VIDEO_KBPS = 220;
export const MAX_VIDEO_KBPS = 2500;
export const AUDIO_KBPS = 64;

export interface EncodePlan {
  videoKbps: number;
  audioKbps: number;
  height: number;
  feasible: boolean;
  estimatedBytes: number;
}

export function planEncode(
  durationSeconds: number,
  sourceHeight: number,
  targetMiB = 45,
): EncodePlan {
  const targetBits = targetMiB * 1024 * 1024 * 8;
  const source = Number.isFinite(sourceHeight) && sourceHeight > 0 ? sourceHeight : 1280;

  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    const height = source >= 720 ? 720 : source;
    return {
      videoKbps: 800,
      audioKbps: AUDIO_KBPS,
      height,
      feasible: true,
      estimatedBytes: 0,
    };
  }

  const available = Math.floor(targetBits / durationSeconds / 1000) - AUDIO_KBPS;
  const feasible = available >= MIN_VIDEO_KBPS;
  const videoKbps = Math.min(MAX_VIDEO_KBPS, Math.max(MIN_VIDEO_KBPS, available));

  let height: number;
  if (videoKbps >= 1200) height = 720;
  else if (videoKbps >= 600) height = 480;
  else if (videoKbps >= 320) height = 360;
  else height = 240;

  const cappedHeight = Math.max(240, Math.min(height, source));
  const estimatedBytes = Math.round(
    ((videoKbps + AUDIO_KBPS) * 1000 * durationSeconds) / 8,
  );

  return {
    videoKbps,
    audioKbps: AUDIO_KBPS,
    height: cappedHeight,
    feasible,
    estimatedBytes,
  };
}
