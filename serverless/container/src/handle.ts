import type { ChildProcess } from "node:child_process";

export class JobCancelled extends Error {
  constructor(message = "Job cancelled") {
    super(message);
    this.name = "JobCancelled";
  }
}

export interface JobHandle {
  readonly cancelled: boolean;
  attach(child: ChildProcess): void;
  onCancel(callback: () => void): void;
  cancel(): void;
}

/**
 * Tracks child processes for one job so /cancel can stop them quickly.
 * yt-dlp and ffmpeg receive SIGTERM first, then SIGKILL after a grace period.
 */
export function createJobHandle(): JobHandle {
  const children = new Set<ChildProcess>();
  const callbacks: Array<() => void> = [];
  let cancelled = false;

  const terminate = (child: ChildProcess): void => {
    if (child.exitCode !== null || child.killed) return;
    try {
      child.kill("SIGTERM");
    } catch {
      return;
    }
    const timer = setTimeout(() => {
      try {
        if (child.exitCode === null) child.kill("SIGKILL");
      } catch {
        // Already gone.
      }
    }, 5000);
    timer.unref();
  };

  return {
    get cancelled() {
      return cancelled;
    },
    attach(child) {
      children.add(child);
      child.once("exit", () => children.delete(child));
      if (cancelled) terminate(child);
    },
    onCancel(callback) {
      callbacks.push(callback);
      if (cancelled) callback();
    },
    cancel() {
      if (cancelled) return;
      cancelled = true;
      for (const child of children) terminate(child);
      for (const callback of callbacks) {
        try {
          callback();
        } catch {
          // Cancellation callbacks must never throw.
        }
      }
    },
  };
}
