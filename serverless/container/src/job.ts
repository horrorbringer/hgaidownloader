import { mkdir, rm, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { downloadEpisode } from "./download.js";
import { encodeVideo } from "./encode.js";
import { JobCancelled, type JobHandle } from "./handle.js";
import { planEncode } from "./plan.js";
import {
  discoverEpisodes,
  findEpisodeStream,
  openSession,
  type Session,
} from "./resolve.js";
import { sendDocument, sendVideo } from "./send.js";
import { escapeHtml, formatBytes, truncate } from "./text.js";
import { probeMedia } from "./verify.js";

const MAX_EPISODES = 10;
const MAX_UPLOAD_BYTES = 47 * 1024 * 1024;
const PROGRESS_INTERVAL_MS = 4000;

export interface JobRequest {
  jobId: string;
  kind: "resolve" | "download";
  url: string;
  episodes: number[] | null;
  chatId: number | null;
  callbackUrl: string;
  jobToken: string;
  botToken: string;
}

export interface JobEvent {
  type: "stage" | "resolved" | "episode" | "done";
  title?: string;
  episodes?: Array<{ number: number; label: string }>;
  note?: string;
  number?: number;
  label?: string;
  state?: string;
  percent?: number;
  detail?: string;
  bytes?: number;
  fileId?: string;
  error?: string;
  ok?: boolean;
  cancelled?: boolean;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function createEmitter(request: JobRequest) {
  return async (event: JobEvent): Promise<void> => {
    const body = JSON.stringify(event);
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        const response = await fetch(request.callbackUrl, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "x-job-token": request.jobToken,
          },
          body,
          signal: AbortSignal.timeout(15_000),
        });
        if (response.ok || response.status < 500) return;
      } catch {
        // Retry with backoff.
      }
      await new Promise((resolve) => setTimeout(resolve, 500 * 2 ** attempt));
    }
    console.warn("progress callback failed", event.type, event.number ?? "");
  };
}

function throttler(intervalMs: number) {
  const last = new Map<string, number>();
  return (key: string, emit: () => void): void => {
    const now = Date.now();
    if (now - (last.get(key) ?? 0) < intervalMs) return;
    last.set(key, now);
    emit();
  };
}

/** Compress until the file fits Telegram's 50 MB bot upload limit. */
async function encodeToFit(
  options: {
    source: string;
    output: string;
    duration: number;
    sourceHeight: number;
    handle: JobHandle;
    emitProgress: (percent: number) => void;
  },
): Promise<number> {
  let targetMiB = 45;
  let size = 0;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const plan = planEncode(options.duration, options.sourceHeight, targetMiB);
    if (!plan.feasible) {
      throw new Error(
        `This episode is ${Math.round(options.duration / 60)} minutes long, too long to fit Telegram's 50 MB limit`,
      );
    }
    await encodeVideo({
      input: options.source,
      output: options.output,
      plan,
      duration: options.duration,
      handle: options.handle,
      onProgress: options.emitProgress,
    });
    size = (await stat(options.output)).size;
    if (size <= MAX_UPLOAD_BYTES) return size;
    const nextTarget = targetMiB - 10;
    if (
      nextTarget < 24 ||
      !planEncode(options.duration, options.sourceHeight, nextTarget).feasible
    ) {
      return size;
    }
    targetMiB = nextTarget;
  }
  return size;
}

export async function runJob(
  request: JobRequest,
  handle: JobHandle,
): Promise<void> {
  const directory = path.join(
    process.env.JOB_DIR ?? path.join(os.tmpdir(), "hg-jobs"),
    request.jobId,
  );
  const emit = createEmitter(request);
  const throttle = throttler(PROGRESS_INTERVAL_MS);
  const allowedHosts = process.env.ALLOWED_HOSTS ?? "huangguoai.com";

  let session: Session | null = null;
  try {
    await mkdir(directory, { recursive: true });

    await emit({ type: "stage", state: "resolving", detail: "Opening the series page" });
    session = await openSession(request.url, allowedHosts);
    const resolved = await discoverEpisodes(session);
    const found = resolved.episodes.length;

    const note =
      request.episodes?.length || found <= MAX_EPISODES
        ? undefined
        : `Only the first ${MAX_EPISODES} of ${found} episodes. Send /dl with a range for another group.`;

    await emit({
      type: "resolved",
      title: resolved.title,
      episodes: resolved.episodes
        .slice(0, 300)
        .map((episode) => ({ number: episode.number, label: episode.label })),
      note,
    });

    if (request.kind === "resolve") {
      await emit({ type: "done", ok: true });
      return;
    }

    const byNumber = new Map(
      resolved.episodes.map((episode) => [episode.number, episode]),
    );
    const requested = request.episodes?.length
      ? request.episodes
      : resolved.episodes.slice(0, MAX_EPISODES).map((episode) => episode.number);

    for (const number of requested.filter((value) => !byNumber.has(value))) {
      await emit({
        type: "episode",
        number,
        label: `第${number}集`,
        state: "failed",
        error: "Not listed on this page",
      });
    }

    const targets = requested
      .map((number) => byNumber.get(number))
      .filter((episode): episode is NonNullable<typeof episode> => Boolean(episode));
    if (!targets.length) {
      throw new Error("None of the requested episodes are listed on this page");
    }

    const seen = new Set<string>();
    let failed = 0;

    for (const episode of targets) {
      if (handle.cancelled) throw new JobCancelled();
      const label = episode.label || `第${episode.number}集`;
      const number = episode.number;
      const output = path.join(directory, `episode-${number}.mp4`);
      let source: string | null = null;

      await emit({
        type: "episode",
        number,
        label,
        state: "downloading",
        percent: 0,
      });

      try {
        const stream = await findEpisodeStream(session, episode, seen);
        console.log(`[${request.jobId}] ${label}: ${stream.url}`);

        source = await downloadEpisode({
          url: stream.url,
          referer: stream.referer,
          directory,
          handle,
          onProgress: (progress) =>
            throttle(`download-${number}`, () =>
              void emit({
                type: "episode",
                number,
                label,
                state: "downloading",
                percent: progress.percent,
                detail: progress.speed,
              }),
            ),
        });

        const info = await probeMedia(source);
        await emit({
          type: "episode",
          number,
          label,
          state: "encoding",
          percent: 0,
        });

        const size = await encodeToFit({
          source,
          output,
          duration: info.duration,
          sourceHeight: info.height,
          handle,
          emitProgress: (percent) =>
            throttle(`encode-${number}`, () =>
              void emit({
                type: "episode",
                number,
                label,
                state: "encoding",
                percent,
              }),
            ),
        });

        await emit({
          type: "episode",
          number,
          label,
          state: "uploading",
          percent: 100,
        });

        const caption = `<b>${escapeHtml(resolved.title)}</b>\n${escapeHtml(label)} · ${formatBytes(size)}`;
        const sendOptions = {
          token: request.botToken,
          chatId: request.chatId ?? 0,
          filePath: output,
          fileName: `episode-${number}.mp4`,
          caption,
          duration: info.duration,
        };
        if (!sendOptions.chatId) throw new Error("No Telegram chat for this job");

        let result = await sendVideo(sendOptions);
        if (!result.ok) {
          console.warn("sendVideo failed, trying sendDocument", result.description);
          const fallback = await sendDocument(sendOptions);
          if (fallback.ok) result = fallback;
        }
        if (!result.ok) {
          throw new Error(
            `Telegram refused the upload: ${truncate(result.description ?? "unknown error", 200)}`,
          );
        }

        await emit({
          type: "episode",
          number,
          label,
          state: "done",
          bytes: size,
          fileId: result.fileId,
        });
      } catch (error) {
        if (handle.cancelled || error instanceof JobCancelled) throw new JobCancelled();
        failed += 1;
        const reason = truncate(messageOf(error), 200);
        console.error(`[${request.jobId}] ${label} failed: ${reason}`);
        await emit({
          type: "episode",
          number,
          label,
          state: "failed",
          error: reason,
        });
      } finally {
        if (source) await rm(source, { force: true }).catch(() => undefined);
        await rm(output, { force: true }).catch(() => undefined);
      }
    }

    await emit({
      type: "done",
      ok: failed === 0,
      error:
        failed > 0 ? `${failed} of ${targets.length} episode(s) failed` : undefined,
    });
  } catch (error) {
    if (handle.cancelled || error instanceof JobCancelled) {
      await emit({ type: "done", ok: false, cancelled: true });
    } else {
      const reason = truncate(messageOf(error), 300);
      console.error(`[${request.jobId}] job failed: ${reason}`);
      await emit({ type: "done", ok: false, error: reason });
    }
  } finally {
    await session?.close();
    await rm(directory, { recursive: true, force: true }).catch(() => undefined);
  }
}
