import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";
import type { JobEvent, JobInput, JobKind, JobStatus } from "./jobs";
import {
  escapeHtml,
  formatBytes,
  hmacToken,
  truncate,
} from "./lib/secure";
import { editMessageText, sendMessage } from "./telegram";

const CONTAINER_PORT = 8080;
const EDIT_INTERVAL_MS = 8_000;
const STALE_JOB_MS = 5 * 60_000;
const JOB_TTL_MS = 24 * 60 * 60_000;
const UPDATE_TTL_MS = 2 * 24 * 60 * 60_000;
const CONTAINER_INSTANCE = "standard-2";
const INACTIVITY_TIMEOUT_MS = 30 * 60_000;
const ACTIVE_STATUSES = "('starting','running','cancelling')";

interface JobRow {
  id: string;
  kind: JobKind;
  url: string;
  chat_id: number | null;
  message_id: number | null;
  title: string | null;
  status: JobStatus;
  total: number;
  done: number;
  error: string | null;
  callback_base: string;
  created_at: number;
  updated_at: number;
  last_event_at: number;
}

interface EpisodeRow {
  job_id: string;
  number: number;
  label: string | null;
  state: string;
  percent: number | null;
  detail: string | null;
  bytes: number | null;
  file_id: string | null;
  error: string | null;
}

interface ContainerApi {
  readonly running: boolean;
  readonly images: Record<string, string>;
  start(options: Record<string, unknown>): void;
  setInactivityTimeout(durationMs: number): Promise<void>;
  getTcpPort(port: number): {
    fetch(input: string, init?: RequestInit): Promise<Response>;
  };
  destroy(reason?: unknown): Promise<void>;
}

export class Runner extends DurableObject<Env> {
  private activeJobId: string | null = null;
  private pumping = false;
  private lastEditAt = new Map<string, number>();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.migrate();
      this.activeJobId = (await ctx.storage.get<string>("activeJobId")) ?? null;
      await this.armAlarm();
    });
  }

  // ---------------------------------------------------------------- storage

  private migrate(): void {
    const sql = this.sql;
    sql.exec(`CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      url TEXT NOT NULL,
      chat_id INTEGER,
      message_id INTEGER,
      title TEXT,
      status TEXT NOT NULL DEFAULT 'queued',
      total INTEGER NOT NULL DEFAULT 0,
      done INTEGER NOT NULL DEFAULT 0,
      error TEXT,
      callback_base TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      last_event_at INTEGER NOT NULL
    )`);
    sql.exec(`CREATE TABLE IF NOT EXISTS job_episodes (
      job_id TEXT NOT NULL,
      number INTEGER NOT NULL,
      label TEXT,
      state TEXT NOT NULL DEFAULT 'waiting',
      percent INTEGER,
      detail TEXT,
      bytes INTEGER,
      file_id TEXT,
      error TEXT,
      PRIMARY KEY (job_id, number)
    )`);
    sql.exec(`CREATE TABLE IF NOT EXISTS seen_updates (
      update_id INTEGER PRIMARY KEY,
      seen_at INTEGER NOT NULL
    )`);
    sql.exec(
      `CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at)`,
    );
  }

  private get sql() {
    return this.ctx.storage.sql;
  }

  private jobRow(jobId: string): JobRow | null {
    const rows = this.sql
      .exec(`SELECT * FROM jobs WHERE id = ?`, jobId)
      .toArray() as unknown as JobRow[];
    return rows[0] ?? null;
  }

  private episodeRows(jobId: string): EpisodeRow[] {
    return this.sql
      .exec(
        `SELECT * FROM job_episodes WHERE job_id = ? ORDER BY number`,
        jobId,
      )
      .toArray() as unknown as EpisodeRow[];
  }

  /**
   * One short alarm while work exists (watchdog), otherwise a single cleanup
   * alarm a day out so an idle bot does not wake its Durable Object every minute.
   */
  private async armAlarm(): Promise<void> {
    const pending = this.sql
      .exec(
        `SELECT COUNT(*) AS count FROM jobs
         WHERE status IN ('queued','starting','running','cancelling')`,
      )
      .one() as { count: number };
    const desired = pending.count ? Date.now() + 60_000 : Date.now() + JOB_TTL_MS;
    const current = await this.ctx.storage.getAlarm();
    if (current === null || current > desired) {
      await this.ctx.storage.setAlarm(desired);
    }
  }

  private async setActive(jobId: string | null): Promise<void> {
    this.activeJobId = jobId;
    if (jobId) await this.ctx.storage.put("activeJobId", jobId);
    else await this.ctx.storage.delete("activeJobId");
  }

  // ----------------------------------------------------------- public RPCs

  async seenUpdate(updateId: number): Promise<boolean> {
    if (!Number.isFinite(updateId)) return false;
    const existing = this.sql
      .exec(`SELECT 1 FROM seen_updates WHERE update_id = ?`, updateId)
      .toArray();
    if (existing.length) return true;
    this.sql.exec(
      `INSERT INTO seen_updates (update_id, seen_at) VALUES (?, ?)`,
      updateId,
      Date.now(),
    );
    await this.armAlarm();
    return false;
  }

  async enqueue(input: JobInput): Promise<{ queued: number }> {
    const now = Date.now();
    this.sql.exec(
      `INSERT INTO jobs (id, kind, url, chat_id, message_id, status, callback_base,
        created_at, updated_at, last_event_at)
       VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)`,
      input.jobId,
      input.kind,
      input.url,
      input.chatId,
      input.messageId,
      input.callbackBase,
      now,
      now,
      now,
    );
    for (const number of input.episodes ?? []) {
      this.sql.exec(
        `INSERT OR IGNORE INTO job_episodes (job_id, number, label, state)
         VALUES (?, ?, ?, 'waiting')`,
        input.jobId,
        number,
        `第${number}集`,
      );
    }
    this.sql.exec(
      `UPDATE jobs SET total = (SELECT COUNT(*) FROM job_episodes WHERE job_id = ?)
       WHERE id = ?`,
      input.jobId,
      input.jobId,
    );
    await this.armAlarm();
    this.ctx.waitUntil(this.pump());
    const queued = (
      this.sql
        .exec(`SELECT COUNT(*) AS count FROM jobs WHERE status = 'queued'`)
        .one() as { count: number }
    ).count;
    return { queued };
  }

  async jobView(jobId: string): Promise<Record<string, unknown> | null> {
    const job = this.jobRow(jobId);
    if (!job) return null;
    const episodes = this.episodeRows(jobId).map((row) => ({
      number: row.number,
      label: row.label,
      state: row.state,
      percent: row.percent,
      detail: row.detail,
      bytes: row.bytes,
      fileId: row.file_id,
      error: row.error,
    }));
    return {
      jobId: job.id,
      kind: job.kind,
      url: job.url,
      title: job.title,
      status: job.status,
      error: job.error,
      done: job.done,
      total: job.total,
      createdAt: job.created_at,
      updatedAt: job.updated_at,
      episodes,
    };
  }

  async statusText(chatId: number): Promise<string> {
    const active = this.sql
      .exec(
        `SELECT * FROM jobs WHERE chat_id = ? AND status IN ${ACTIVE_STATUSES}
         ORDER BY created_at LIMIT 1`,
        chatId,
      )
      .toArray() as unknown as JobRow[];
    const queued = (
      this.sql
        .exec(
          `SELECT COUNT(*) AS count FROM jobs
           WHERE chat_id = ? AND status = 'queued'`,
          chatId,
        )
        .one() as { count: number }
    ).count;
    if (!active.length) {
      return queued
        ? `Nothing is running. ${queued} job(s) queued.`
        : "Nothing is running.";
    }
    const job = active[0];
    const rows = this.episodeRows(job.id);
    const finished = rows.filter(
      (row) => row.state === "done" || row.state === "failed",
    ).length;
    const lines = [
      `<b>${escapeHtml(job.title ?? "Working…")}</b>`,
      `Status: ${job.status}`,
      `${finished}/${rows.length || "?"} episodes finished`,
    ];
    if (queued) lines.push(`${queued} job(s) queued`);
    return lines.join("\n");
  }

  async cancelChat(chatId: number): Promise<string> {
    const active = this.sql
      .exec(
        `SELECT * FROM jobs WHERE chat_id = ? AND status IN ${ACTIVE_STATUSES}
         ORDER BY created_at LIMIT 1`,
        chatId,
      )
      .toArray() as unknown as JobRow[];
    if (active.length) {
      const job = active[0];
      if (job.status !== "cancelling") {
        this.sql.exec(
          `UPDATE jobs SET status = 'cancelling', updated_at = ? WHERE id = ?`,
          Date.now(),
          job.id,
        );
        await this.requestContainerCancel(job.id);
        await this.render(job.id, this.progressText(job.id, "Cancelling…"), true);
      }
      return "Cancelling the current job…";
    }
    const queued = this.sql
      .exec(
        `SELECT * FROM jobs WHERE chat_id = ? AND status = 'queued'
         ORDER BY created_at`,
        chatId,
      )
      .toArray() as unknown as JobRow[];
    for (const job of queued) {
      this.sql.exec(
        `UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?`,
        Date.now(),
        job.id,
      );
      await this.render(job.id, "🚫 Cancelled before it started.", true);
    }
    return queued.length
      ? `Cancelled ${queued.length} queued job(s).`
      : "Nothing to cancel.";
  }

  async cancelJob(jobId: string): Promise<boolean> {
    const job = this.jobRow(jobId);
    if (!job) return false;
    if (job.status === "queued") {
      this.sql.exec(
        `UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?`,
        Date.now(),
        jobId,
      );
      await this.render(jobId, "🚫 Cancelled before it started.", true);
      return true;
    }
    if (job.status === "starting" || job.status === "running") {
      this.sql.exec(
        `UPDATE jobs SET status = 'cancelling', updated_at = ? WHERE id = ?`,
        Date.now(),
        jobId,
      );
      await this.requestContainerCancel(jobId);
      await this.render(jobId, this.progressText(jobId, "Cancelling…"), true);
      return true;
    }
    return false;
  }

  async handleEvent(jobId: string, event: JobEvent): Promise<void> {
    const job = this.jobRow(jobId);
    if (!job) return;
    if (job.status === "done" || job.status === "failed" || job.status === "cancelled") {
      return;
    }
    const now = Date.now();
    this.sql.exec(
      `UPDATE jobs SET last_event_at = ?, updated_at = ? WHERE id = ?`,
      now,
      now,
      jobId,
    );

    switch (event.type) {
      case "stage": {
        await this.armAlarm();
        return;
      }
      case "resolved": {
        const title = event.title ?? "Series";
        this.sql.exec(`UPDATE jobs SET title = ? WHERE id = ?`, title, jobId);
        for (const episode of event.episodes ?? []) {
          this.sql.exec(
            `UPDATE job_episodes SET label = ? WHERE job_id = ? AND number = ?`,
            episode.label,
            jobId,
            episode.number,
          );
        }
        const text = this.resolvedText(jobId, event);
        if (job.kind === "resolve") {
          this.sql.exec(
            `UPDATE jobs SET total = ? WHERE id = ?`,
            event.episodes?.length ?? 0,
            jobId,
          );
          await this.render(jobId, text, true);
        } else {
          await this.render(jobId, text, false);
        }
        break;
      }
      case "episode": {
        if (event.number == null) return;
        this.sql.exec(
          `INSERT OR IGNORE INTO job_episodes (job_id, number, label, state)
           VALUES (?, ?, ?, 'waiting')`,
          jobId,
          event.number,
          event.label ?? `第${event.number}集`,
        );
        this.sql.exec(
          `UPDATE job_episodes SET state = ?, percent = ?, detail = ?, bytes = ?,
             file_id = ?, error = ? WHERE job_id = ? AND number = ?`,
          event.state ?? "downloading",
          event.percent ?? null,
          event.detail ?? null,
          event.bytes ?? null,
          event.fileId ?? null,
          event.error ?? null,
          jobId,
          event.number,
        );
        this.sql.exec(
          `UPDATE jobs SET done = (
             SELECT COUNT(*) FROM job_episodes
             WHERE job_id = ? AND state IN ('done')
           ), total = (
             SELECT COUNT(*) FROM job_episodes WHERE job_id = ?
           ) WHERE id = ?`,
          jobId,
          jobId,
          jobId,
        );
        if (job.kind === "download") {
          await this.render(jobId, this.progressText(jobId), false);
        }
        await this.armAlarm();
        break;
      }
      case "done": {
        const cancelled = Boolean(event.cancelled);
        const ok = Boolean(event.ok) && !cancelled;
        const status: JobStatus = cancelled ? "cancelled" : ok ? "done" : "failed";
        this.sql.exec(
          `UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?`,
          status,
          event.error ?? null,
          Date.now(),
          jobId,
        );
        if (!ok) {
          this.sql.exec(
            `UPDATE job_episodes SET state = 'failed', error = COALESCE(error, ?)
             WHERE job_id = ? AND state IN ('waiting','downloading','encoding','uploading')`,
            event.error ?? "Job stopped",
            jobId,
          );
        }
        const text = this.finalText(jobId, status, event);
        await this.render(jobId, text, true);
        if (this.activeJobId === jobId) await this.setActive(null);
        this.lastEditAt.delete(jobId);
        this.ctx.waitUntil(this.pump());
        break;
      }
    }
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    const stale = this.sql
      .exec(
        `SELECT * FROM jobs WHERE status IN ${ACTIVE_STATUSES}
         AND last_event_at < ?`,
        now - STALE_JOB_MS,
      )
      .toArray() as unknown as JobRow[];
    for (const job of stale) {
      this.sql.exec(
        `UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`,
        "The worker stopped responding (instance restarted)",
        now,
        job.id,
      );
      this.sql.exec(
        `UPDATE job_episodes SET state = 'failed', error = COALESCE(error, ?)
         WHERE job_id = ? AND state IN ('waiting','downloading','encoding','uploading')`,
        "Worker stopped responding",
        job.id,
      );
      await this.render(
        job.id,
        this.progressText(
          job.id,
          "❌ The worker stopped responding. Send /dl again to retry.",
        ),
        true,
      );
    }
    if (stale.length) {
      this.activeJobId = null;
      await this.ctx.storage.delete("activeJobId").catch(() => undefined);
      try {
        await this.containerApi()?.destroy("stale job");
      } catch {
        // Container may already be gone.
      }
    }

    this.sql.exec(`DELETE FROM jobs WHERE created_at < ?`, now - JOB_TTL_MS);
    this.sql.exec(
      `DELETE FROM job_episodes WHERE job_id NOT IN (SELECT id FROM jobs)`,
    );
    this.sql.exec(`DELETE FROM seen_updates WHERE seen_at < ?`, now - UPDATE_TTL_MS);

    await this.pump();
    await this.armAlarm();
  }

  // --------------------------------------------------------------- pipeline

  private containerApi(): ContainerApi | null {
    const container = (this.ctx as unknown as { container?: ContainerApi })
      .container;
    return container ?? null;
  }

  private async pump(): Promise<void> {
    if (this.pumping) return;
    this.pumping = true;
    try {
      for (;;) {
        const active = this.sql
          .exec(
            `SELECT id FROM jobs WHERE status IN ${ACTIVE_STATUSES} LIMIT 1`,
          )
          .toArray() as unknown as Array<{ id: string }>;
        if (active.length) {
          await this.setActive(active[0].id);
          return;
        }
        const queued = this.sql
          .exec(
            `SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1`,
          )
          .toArray() as unknown as JobRow[];
        const next = queued[0];
        if (!next) {
          await this.setActive(null);
          return;
        }

        await this.setActive(next.id);
        this.sql.exec(
          `UPDATE jobs SET status = 'starting', updated_at = ? WHERE id = ?`,
          Date.now(),
          next.id,
        );
        try {
          if (!this.env.TELEGRAM_BOT_TOKEN) {
            throw new Error("TELEGRAM_BOT_TOKEN is not configured");
          }
          const container = await this.startContainer();
          const token = await hmacToken(this.env.INTERNAL_SECRET, next.id);
          const callbackUrl = `${next.callback_base.replace(/\/$/, "")}/internal/jobs/${encodeURIComponent(next.id)}/event`;
          const payload = {
            jobId: next.id,
            kind: next.kind,
            url: next.url,
            episodes: this.requestedEpisodes(next.id, next.kind),
            chatId: next.chat_id,
            callbackUrl,
            jobToken: token,
            botToken: this.env.TELEGRAM_BOT_TOKEN,
          };
          const response = await container.getTcpPort(CONTAINER_PORT).fetch(
            "http://container/jobs",
            {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify(payload),
            },
          );
          if (!response.ok) {
            throw new Error(
              `worker rejected the job (HTTP ${response.status}): ${truncate(
                await response.text().catch(() => ""),
                200,
              )}`,
            );
          }
          this.sql.exec(
            `UPDATE jobs SET status = 'running', updated_at = ?, last_event_at = ?
             WHERE id = ? AND status = 'starting'`,
            Date.now(),
            Date.now(),
            next.id,
          );
          const current = this.jobRow(next.id);
          if (current?.status === "cancelling") {
            await this.requestContainerCancel(next.id);
          }
          return;
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          this.sql.exec(
            `UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`,
            message,
            Date.now(),
            next.id,
          );
          await this.render(
            next.id,
            `❌ Could not start the download worker: ${escapeHtml(truncate(message, 300))}`,
            true,
          );
          await this.setActive(null);
          try {
            await this.containerApi()?.destroy("job start failed");
          } catch {
            // The container may never have started.
          }
          // Loop and try the next queued job.
        }
      }
    } finally {
      this.pumping = false;
    }
  }

  private requestedEpisodes(jobId: string, kind: JobKind): number[] | null {
    if (kind !== "download") return null;
    const rows = this.episodeRows(jobId);
    return rows.length ? rows.map((row) => row.number) : null;
  }

  private async startContainer(): Promise<ContainerApi> {
    const container = this.containerApi();
    if (!container) throw new Error("Container binding is missing");
    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: CONTAINER_INSTANCE,
        enableInternet: true,
        env: { PORT: String(CONTAINER_PORT) },
      });
    }
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    for (let attempt = 0; attempt < 180; attempt += 1) {
      if (!container.running) throw new Error("The container stopped during startup");
      try {
        const response = await container
          .getTcpPort(CONTAINER_PORT)
          .fetch("http://container/health");
        if (response.ok) return container;
      } catch {
        // Not listening yet.
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error("The container did not become ready in time");
  }

  private async requestContainerCancel(jobId: string): Promise<void> {
    try {
      const container = this.containerApi();
      if (!container?.running) return;
      await container
        .getTcpPort(CONTAINER_PORT)
        .fetch(`http://container/jobs/${encodeURIComponent(jobId)}/cancel`, {
          method: "POST",
        });
    } catch {
      // The watchdog will finish the job if the container is gone.
    }
  }

  // --------------------------------------------------------------- messages

  private async render(jobId: string, text: string, force: boolean): Promise<void> {
    const job = this.jobRow(jobId);
    if (!job || job.chat_id == null) return;
    const now = Date.now();
    const last = this.lastEditAt.get(jobId) ?? 0;
    if (!force && now - last < EDIT_INTERVAL_MS) return;
    this.lastEditAt.set(jobId, now);

    const body = truncate(text, 3900);
    if (job.message_id) {
      const response = await editMessageText(
        this.env.TELEGRAM_BOT_TOKEN,
        job.chat_id,
        job.message_id,
        body,
      );
      if (
        !response.ok &&
        response.error_code === 400 &&
        /message is not modified/i.test(response.description ?? "")
      ) {
        return;
      }
      if (!response.ok) {
        console.warn("editMessageText failed", response.error_code, response.description);
        if (response.error_code === 429) {
          this.lastEditAt.set(jobId, now + 10_000);
        }
      }
      return;
    }

    const messageId = await sendMessage(
      this.env.TELEGRAM_BOT_TOKEN,
      job.chat_id,
      body,
    );
    if (messageId) {
      this.sql.exec(`UPDATE jobs SET message_id = ? WHERE id = ?`, messageId, jobId);
    }
  }

  private resolvedText(jobId: string, event: JobEvent): string {
    const job = this.jobRow(jobId);
    const title = event.title ?? "Series";
    const episodes = event.episodes ?? [];
    const labels = episodes
      .slice(0, 60)
      .map((episode) => `${episode.number}`)
      .join(", ");
    const lines = [
      `<b>${escapeHtml(title)}</b>`,
      `${episodes.length} episodes found`,
    ];
    if (event.note) lines.push(escapeHtml(event.note));
    if (labels) lines.push(`Numbers: ${labels}${episodes.length > 60 ? ", …" : ""}`);
    if (job?.kind === "download") {
      lines.push("", "Downloading the episodes you asked for…");
    } else if (job) {
      lines.push(
        "",
        `Download with:`,
        `<code>/dl ${escapeHtml(job.url)} 1-10</code>`,
      );
    }
    return lines.join("\n");
  }

  private progressText(jobId: string, note?: string): string {
    const job = this.jobRow(jobId);
    const rows = this.episodeRows(jobId);
    const lines = [`<b>${escapeHtml(job?.title ?? "Working…")}</b>`];
    for (const row of rows.slice(0, 12)) {
      lines.push(
        `${escapeHtml(row.label ?? `第${row.number}集`)} · ${stateLine(row)}`,
      );
    }
    const finished = rows.filter(
      (row) => row.state === "done" || row.state === "failed",
    ).length;
    lines.push("");
    lines.push(`${finished}/${rows.length || "?"} finished`);
    if (note) lines.push(note);
    return lines.join("\n");
  }

  private finalText(jobId: string, status: JobStatus, event: JobEvent): string {
    const job = this.jobRow(jobId);
    const title = escapeHtml(job?.title ?? "Series");
    if (job?.kind === "resolve") {
      if (status === "done") {
        return [
          `<b>${title}</b>`,
          `${job.total} episodes found`,
          "",
          `Download with <code>/dl ${escapeHtml(job.url)} 1-10</code>`,
        ].join("\n");
      }
      if (status === "cancelled") return `<b>${title}</b>\n\n🚫 Cancelled`;
      return `<b>${title}</b>\n\n❌ ${escapeHtml(
        truncate(event.error ?? "Could not read the series page", 300),
      )}`;
    }
    const progress = this.progressText(jobId);
    if (status === "done") {
      const sizes = this.episodeRows(jobId)
        .filter((row) => row.state === "done" && row.bytes)
        .map((row) => formatBytes(row.bytes ?? 0));
      const total = sizes.length ? ` (${sizes.join(", ")})` : "";
      return `${progress}\n\n✅ Done${total}`;
    }
    if (status === "cancelled") {
      return `${progress}\n\n🚫 Cancelled`;
    }
    return `${progress}\n\n❌ ${escapeHtml(
      truncate(event.error ?? "The job failed", 300),
    )}`;
  }
}

function stateLine(row: EpisodeRow): string {
  switch (row.state) {
    case "done":
      return `✅ ${formatBytes(row.bytes ?? 0)}`;
    case "failed":
      return `❌ ${escapeHtml(truncate(row.error ?? "failed", 80))}`;
    case "downloading":
      return `⬇ ${row.percent ?? 0}%${row.detail ? ` · ${escapeHtml(row.detail)}` : ""}`;
    case "encoding":
      return `🎛 ${row.percent ?? 0}%`;
    case "uploading":
      return "⬆ uploading";
    case "waiting":
    default:
      return "⏳ waiting";
  }
}
