import { Redis } from "@upstash/redis";
import { send } from "@vercel/queue";
import type { JobEvent, JobInput, JobRunner, JobStatus } from "../jobs";
import { escapeHtml, truncate } from "../lib/secure";
import { editMessageText } from "../telegram";

export const JOB_TOPIC = "series-download";
const JOB_TTL = 86_400;
const ACTIVE = "series:active";

interface StoredJob extends JobInput {
  status: JobStatus;
  createdAt: number;
  updatedAt: number;
  title?: string;
  error?: string;
  episodesView: JobEvent[];
  resolvedCount?: number;
  lastEdit?: number;
}

const jobKey = (id: string) => `series:job:${id}`;
const chatKey = (id: number | null) => `series:chat:${id}`;
const terminal = (status: JobStatus) => ["done", "failed", "cancelled"].includes(status);

export class VercelRunner implements JobRunner {
  private client?: Redis;

  constructor(private readonly botToken: string) {}

  private get redis() {
    return this.client ??= Redis.fromEnv({ automaticDeserialization: false });
  }

  async read(id: string): Promise<StoredJob | null> {
    const value = await this.redis.get<string>(jobKey(id));
    return value === null ? null : JSON.parse(value) as StoredJob;
  }

  async change(id: string, update: (job: StoredJob) => StoredJob | null) {
    const redis = this.redis;
    const key = jobKey(id);
    for (let attempt = 0; attempt < 10; attempt++) {
      const current = await redis.get<string>(key);
      if (current === null) return null;
      const next = update(JSON.parse(current) as StoredJob);
      if (!next) return null;
      next.updatedAt = Date.now();
      const saved = await redis.eval<string[], number>(
        "if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]) return 1",
        [key], [current, JSON.stringify(next), String(JOB_TTL)],
      );
      if (saved) return next;
    }
    throw new Error("Could not update the job after concurrent writes");
  }

  async claim(owner: string) {
    return Boolean(await this.redis.set(ACTIVE, owner, { nx: true, ex: 1860 }));
  }

  async release(owner: string) {
    await this.redis.eval(
      "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
      [ACTIVE], [owner],
    );
  }

  async seenUpdate(id: number) {
    if (!Number.isSafeInteger(id)) return true;
    return !await this.redis.set(`series:update:${id}`, "1", { nx: true, ex: 172800 });
  }

  async enqueue(input: JobInput) {
    const now = Date.now();
    const job: StoredJob = {
      ...input, status: "queued", episodesView: [], createdAt: now, updatedAt: now,
    };
    const redis = this.redis;
    const index = chatKey(job.chatId);
    await redis.multi()
      .set(jobKey(job.jobId), JSON.stringify(job), { ex: JOB_TTL })
      .zadd(index, { score: now, member: job.jobId })
      .zremrangebyscore(index, 0, now - JOB_TTL * 1000)
      .expire(index, JOB_TTL)
      .exec();
    try {
      await send(JOB_TOPIC, job.jobId, { idempotencyKey: job.jobId });
    } catch (error) {
      await this.change(job.jobId, (current) => {
        if (current.status !== "queued") return null;
        current.status = "failed";
        current.error = "Could not publish the job to Vercel Queues";
        return current;
      });
      throw error;
    }
    return { queued: 1 };
  }

  async jobView(id: string) {
    const job = await this.read(id);
    if (!job) return null;
    return {
      jobId: job.jobId, kind: job.kind, url: job.url, title: job.title ?? null,
      status: job.status, error: job.error ?? null, createdAt: job.createdAt,
      updatedAt: job.updatedAt, total: job.resolvedCount ?? 0,
      done: job.episodesView.filter((episode) => episode.state === "done").length,
      episodes: job.episodesView.map(({ type, ...episode }) => episode),
    };
  }

  private async chatJobs(chatId: number) {
    const ids = await this.redis.zrange<string[]>(chatKey(chatId), 0, -1);
    const jobs = await Promise.all(ids.map((id) => this.read(id)));
    return jobs.filter((job): job is StoredJob => job !== null && !terminal(job.status));
  }

  async statusText(chatId: number) {
    const jobs = await this.chatJobs(chatId);
    if (!jobs.length) return "No active or queued jobs.";
    return jobs.slice(0, 10).map((job) =>
      `${escapeHtml(job.title ?? job.kind)} · ${job.status}`
    ).join("\n");
  }

  async cancelChat(chatId: number) {
    const jobs = await this.chatJobs(chatId);
    const active = jobs.find((job) => job.status !== "queued");
    if (active) {
      await this.cancelJob(active.jobId);
      return "Cancelling the current job…";
    }
    const cancelled = await Promise.all(jobs.map((job) => this.cancelJob(job.jobId)));
    const count = cancelled.filter(Boolean).length;
    return count ? `Cancelled ${count} queued job(s).` : "Nothing to cancel.";
  }

  async cancelJob(id: string) {
    const job = await this.change(id, (job) => {
      if (terminal(job.status) || job.status === "cancelling") return null;
      job.status = job.status === "queued" ? "cancelled" : "cancelling";
      return job;
    });
    if (job?.status === "cancelled" && job.chatId !== null && job.messageId !== null) {
      await editMessageText(this.botToken, job.chatId, job.messageId, "Cancelled before it started.")
        .catch((error) => console.error("Telegram cancellation update failed", error));
    }
    return Boolean(job);
  }

  async handleEvent(id: string, event: JobEvent) {
    const now = Date.now();
    const job = await this.change(id, (job) => {
      if (terminal(job.status) || job.status === "queued") return null;
      if (event.type === "done") {
        job.status = event.cancelled ? "cancelled" : event.ok ? "done" : "failed";
        job.error = event.error;
      } else if (event.type === "resolved") {
        job.title = event.title;
        job.resolvedCount = event.episodes?.length;
        job.episodesView = (event.episodes ?? []).map((episode) => ({
          ...episode, type: "episode", state: "pending",
        }));
      } else if (event.type === "episode" && typeof event.number === "number") {
        const index = job.episodesView.findIndex((episode) => episode.number === event.number);
        if (index < 0) job.episodesView.push(event);
        else job.episodesView[index] = { ...job.episodesView[index], ...event };
      }
      if (event.type === "done" || event.type === "resolved" || now - (job.lastEdit ?? 0) >= 8000) {
        job.lastEdit = now;
      }
      return job;
    });
    if (!job || job.lastEdit !== now || job.chatId === null || job.messageId === null) return;

    let message = event.detail ?? event.state ?? job.status;
    if (event.type === "resolved" || (event.type === "done" && event.ok && job.kind === "resolve")) {
      message = [job.title ?? "Episodes", ...job.episodesView.map((episode) =>
        `${episode.number}. ${episode.label ?? "Episode"}`
      )].join("\n");
    } else if (event.type === "done") {
      message = event.summary ?? event.error ?? job.status;
    } else if (event.number !== undefined) {
      message = `Episode ${event.number} · ${message}${event.percent === undefined ? "" : ` (${event.percent}%)`}`;
    }
    await editMessageText(this.botToken, job.chatId, job.messageId, escapeHtml(truncate(message, 700)))
      .catch((error) => console.error("Telegram progress update failed", error));
  }
}
