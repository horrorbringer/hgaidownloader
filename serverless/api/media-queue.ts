import { handleCallback } from "@vercel/queue";
import { APIError, Sandbox } from "@vercel/sandbox";
import { hmacToken } from "../src/lib/secure";
import { VercelRunner } from "../src/vercel/runner";

function sandboxUnavailable(error: unknown) {
  return error instanceof APIError && [404, 410].includes(error.response.status);
}

async function stopSandbox(name: string) {
  try {
    const sandbox = await Sandbox.get({ name });
    await sandbox.stop();
  } catch (error) {
    if (!sandboxUnavailable(error)) throw error;
  }
}

export default {
  fetch: handleCallback<string>(async (jobId) => {
    const botToken = process.env.TELEGRAM_BOT_TOKEN ?? "";
    const runner = new VercelRunner(botToken);
    const job = await runner.read(jobId);
    if (!job) return;

    const name = `media-${jobId}`;
    if (job.status === "queued") {
      if (!await runner.claim(jobId)) throw new Error("Another media job is active");
      const started = await runner.change(jobId, (current) => {
        if (current.status !== "queued") return null;
        current.status = "running";
        return current;
      });
      if (!started) throw new Error("Job state changed before startup");

      try {
        const secret = process.env.INTERNAL_SECRET;
        const image = process.env.MEDIA_SANDBOX_IMAGE;
        const baseUrl = process.env.PUBLIC_BASE_URL;
        if (!botToken || !secret || !image || !baseUrl) {
          throw new Error("Set TELEGRAM_BOT_TOKEN, INTERNAL_SECRET, MEDIA_SANDBOX_IMAGE and PUBLIC_BASE_URL");
        }
        const sandbox = await Sandbox.create({
          name,
          image,
          persistent: false,
          timeout: 27 * 60_000,
          resources: { vcpus: 2 },
          signal: AbortSignal.timeout(90_000),
        });
        if ((await runner.read(jobId))?.status === "running") {
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
          const command = await sandbox.runCommand({
            cmd: "node",
            args: ["/app/dist/vercel-job.js"],
            env: {
              MEDIA_JOB: JSON.stringify(request),
              PLAYWRIGHT_BROWSERS_PATH: "/ms-playwright",
            },
            detached: true,
          });
          await runner.change(jobId, (current) => ({ ...current, commandId: command.cmdId }));
        }
      } catch (error) {
        console.error("Could not start media job", error);
        await stopSandbox(name);
        await runner.handleEvent(jobId, {
          type: "done",
          error: "The Vercel media worker could not start. Check function logs.",
        });
        await runner.release(jobId);
        return;
      }
      throw new Error("Media job started and is awaiting completion");
    }

    if (job.status === "running" && job.commandId) {
      try {
        const sandbox = await Sandbox.get({ name });
        const command = await sandbox.getCommand(job.commandId);
        if (command.exitCode === null) throw new Error("Media job is running");
      } catch (error) {
        if (!sandboxUnavailable(error)) throw error;
      }
    }

    await stopSandbox(name);
    if (job.status === "running" || job.status === "cancelling") {
      const latest = await runner.read(jobId);
      await runner.handleEvent(jobId, {
        type: "done",
        cancelled: latest?.status === "cancelling",
        error: latest?.status === "running" ? "The media worker stopped before reporting completion." : undefined,
      });
    }
    await runner.release(jobId);
  }, {
    retry: () => ({ afterSeconds: 60 }),
  }),
};
