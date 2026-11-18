import { handleCallback } from "@vercel/queue";
import { Sandbox } from "@vercel/sandbox";
import { hmacToken } from "../src/lib/secure";
import { VercelRunner } from "../src/vercel/runner";

const POLL_INTERVAL = 5000;

export default {
  fetch: handleCallback<string>(async (jobId) => {
    const botToken = process.env.TELEGRAM_BOT_TOKEN;
    const secret = process.env.INTERNAL_SECRET;
    const image = process.env.MEDIA_SANDBOX_IMAGE;
    const baseUrl = process.env.PUBLIC_BASE_URL;
    const runner = new VercelRunner(botToken ?? "");
    const owner = crypto.randomUUID();
    if (!await runner.claim(owner)) throw new Error("Another media job is active");

    let sandbox: Sandbox | undefined;
    let started = false;
    let loaded = false;
    try {
      const job = await runner.change(jobId, (job) => {
        started = job.status === "queued";
        if (started) job.status = "running";
        return job.status === "running" || job.status === "cancelling" ? job : null;
      });
      loaded = true;
      if (!job) return;
      if (job.status === "cancelling") {
        await runner.handleEvent(jobId, { type: "done", cancelled: true });
        return;
      }
      if (!started) throw new Error("A previous invocation stopped while processing this job");
      if (!botToken || !secret || !image || !baseUrl) {
        throw new Error("Set TELEGRAM_BOT_TOKEN, INTERNAL_SECRET, MEDIA_SANDBOX_IMAGE and PUBLIC_BASE_URL");
      }
      sandbox = await Sandbox.create({
        image,
        persistent: false,
        timeout: 27 * 60_000,
        resources: { vcpus: 2 },
      });
      const request = {
        jobId,
        kind: job.kind,
        url: job.url,
        episodes: job.episodes,
        chatId: job.chatId,
        callbackUrl: new URL(`/internal/jobs/${jobId}/event`, baseUrl).href,
        jobToken: await hmacToken(secret, jobId),
        botToken,
      };
      if ((await runner.read(jobId))?.status === "cancelling") {
        await runner.handleEvent(jobId, { type: "done", cancelled: true });
        return;
      }
      const command = await sandbox.runCommand({
        cmd: "node",
        args: ["/app/dist/vercel-job.js"],
        env: {
          MEDIA_JOB: JSON.stringify(request),
          PLAYWRIGHT_BROWSERS_PATH: "/ms-playwright",
        },
        detached: true,
      });
      for (;;) {
        const status = (await runner.read(jobId))?.status;
        if (status === "cancelling") {
          await sandbox.stop();
          sandbox = undefined;
          await runner.handleEvent(jobId, { type: "done", cancelled: true });
          return;
        }
        if (status !== "running") return;
        const current = await sandbox.getCommand(command.cmdId);
        if (current.exitCode !== null) {
          const latest = (await runner.read(jobId))?.status;
          if (latest === "running") throw new Error("Media worker exited without reporting completion");
          if (latest !== "cancelling") return;
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));
      }
    } catch (error) {
      if (!loaded) throw error;
      console.error("Media job failed", error);
      await runner.handleEvent(jobId, { type: "done", error: "The Vercel media worker failed. Check function logs." });
    } finally {
      if (sandbox) await sandbox.stop();
      await runner.release(owner);
    }
  }),
};
