import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { createJobHandle, type JobHandle } from "./handle.js";
import { runJob, type JobRequest } from "./job.js";

const jobs = new Map<string, JobHandle>();
const MAX_BODY_BYTES = 1024 * 1024;

function json(response: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  response.end(payload);
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new Error("request body too large");
    chunks.push(buffer);
  }
  if (!chunks.length) return null;
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function isJobRequest(value: unknown): value is JobRequest {
  if (!value || typeof value !== "object") return false;
  const job = value as Record<string, unknown>;
  return (
    typeof job.jobId === "string" &&
    job.jobId.length > 0 &&
    (job.kind === "resolve" || job.kind === "download") &&
    typeof job.url === "string" &&
    typeof job.callbackUrl === "string" &&
    typeof job.jobToken === "string" &&
    typeof job.botToken === "string" &&
    (job.episodes === null ||
      (Array.isArray(job.episodes) &&
        job.episodes.every((number) => Number.isInteger(number) && number > 0)))
  );
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", "http://container");

    if (request.method === "GET" && url.pathname === "/health") {
      return json(response, 200, { ok: true, jobs: jobs.size });
    }

    if (request.method === "POST" && url.pathname === "/jobs") {
      const body = await readJson(request);
      if (!isJobRequest(body)) return json(response, 400, { error: "invalid job" });
      if (jobs.has(body.jobId)) {
        return json(response, 202, { accepted: true, duplicate: true });
      }
      const handle = createJobHandle();
      jobs.set(body.jobId, handle);
      void runJob(body, handle)
        .catch((error) => console.error("job crashed", error))
        .finally(() => jobs.delete(body.jobId));
      return json(response, 202, { accepted: true });
    }

    const cancel = /^\/jobs\/([^/]+)\/cancel$/.exec(url.pathname);
    if (request.method === "POST" && cancel) {
      const jobId = decodeURIComponent(cancel[1]);
      const handle = jobs.get(jobId);
      if (!handle) return json(response, 404, { error: "unknown job" });
      handle.cancel();
      return json(response, 202, { ok: true });
    }

    return json(response, 404, { error: "not found" });
  } catch (error) {
    console.error("request failed", error);
    return json(response, 500, {
      error: error instanceof Error ? error.message : String(error),
    });
  }
});

const port = Number(process.env.PORT ?? 8080);
server.listen(port, "0.0.0.0", () => {
  console.log(`job runner listening on ${port}`);
});

function shutdown(signal: string): void {
  console.log(`${signal} received, cancelling jobs`);
  for (const handle of jobs.values()) handle.cancel();
  server.close(() => process.exit(0));
  const force = setTimeout(() => process.exit(0), 14_000);
  force.unref();
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
