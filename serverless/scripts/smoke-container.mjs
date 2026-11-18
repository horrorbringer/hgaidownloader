/**
 * Local smoke test for the job container, without Cloudflare and without
 * Telegram. It serves a fake series page + MP4, then runs a resolve job and a
 * download job against the container HTTP API.
 *
 * Usage:
 *   npm run smoke:container
 *
 * Requires: Docker NOT needed, but Playwright Chromium, ffmpeg, ffprobe and
 * yt-dlp must be available on PATH (or installed via `npx playwright install`).
 */
import { spawn } from "node:child_process";
import { createServer as createHttpServer } from "node:http";
import { createServer as createEventServer } from "node:http";
import { existsSync, createReadStream, statSync, mkdirSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const ROOT = path.resolve(import.meta.dirname, "..");
const WORK = path.join(os.tmpdir(), "hg-smoke");
const SITE_PORT = 18081;
const RUNNER_PORT = 18080;
const EVENTS_PORT = 18082;
const SITE = `http://127.0.0.1:${SITE_PORT}`;
const VIDEO = path.join(WORK, "video.mp4");
const JOB_DIR = path.join(WORK, "jobs");

mkdirSync(WORK, { recursive: true });
mkdirSync(JOB_DIR, { recursive: true });

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit" });
    child.once("error", reject);
    child.once("close", (code) =>
      code === 0 ? resolve() : reject(new Error(`${command} exited ${code}`)),
    );
  });
}

async function ensureVideo() {
  if (existsSync(VIDEO)) return;
  console.log("generating test video…");
  await run("ffmpeg", [
    "-y",
    "-f",
    "lavfi",
    "-i",
    "testsrc=size=640x360:rate=25",
    "-f",
    "lavfi",
    "-i",
    "sine=frequency=440",
    "-t",
    "12",
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-shortest",
    VIDEO,
  ]);
}

const seriesPage = `<!doctype html><html><head><title>Smoke Series - Test</title></head>
<body>
<div data-web-play-ep-grid>
  <a data-ep-id="1" href="/ep-1">第1集</a>
  <a data-ep-id="2" href="/ep-2">第2集</a>
</div>
</body></html>`;

function episodePage(number) {
  return `<!doctype html><html><head><title>Smoke Series - 第${number}集</title></head>
<body>
<script id="videoInitialData" type="application/json">${JSON.stringify({
    url: `${SITE}/video.mp4`,
    drama_duration: 12,
  })}</script>
</body></html>`;
}

const siteServer = createHttpServer((request, response) => {
  const url = new URL(request.url ?? "/", SITE);
  if (url.pathname === "/series") {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(seriesPage);
    return;
  }
  const episode = /^\/ep-(\d+)$/.exec(url.pathname);
  if (episode) {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(episodePage(Number(episode[1])));
    return;
  }
  if (url.pathname === "/video.mp4") {
    const size = statSync(VIDEO).size;
    const range = request.headers.range;
    if (request.method === "HEAD") {
      response.writeHead(200, {
        "content-type": "video/mp4",
        "content-length": size,
        "accept-ranges": "bytes",
      });
      response.end();
      return;
    }
    if (range) {
      const match = /bytes=(\d+)-(\d*)/.exec(range);
      const start = Number(match?.[1] ?? 0);
      const end = match?.[2] ? Number(match[2]) : size - 1;
      response.writeHead(206, {
        "content-type": "video/mp4",
        "content-length": end - start + 1,
        "content-range": `bytes ${start}-${end}/${size}`,
        "accept-ranges": "bytes",
      });
      createReadStream(VIDEO, { start, end }).pipe(response);
      return;
    }
    response.writeHead(200, {
      "content-type": "video/mp4",
      "content-length": size,
      "accept-ranges": "bytes",
    });
    createReadStream(VIDEO).pipe(response);
    return;
  }
  response.writeHead(404);
  response.end("not found");
});

const events = new Map();
const eventServer = createEventServer((request, response) => {
  let body = "";
  request.setEncoding("utf8");
  request.on("data", (chunk) => (body += chunk));
  request.on("end", () => {
    try {
      const event = JSON.parse(body);
      const jobId = new URL(request.url ?? "/", "http://events").pathname
        .split("/")[3];
      const list = events.get(jobId) ?? [];
      list.push(event);
      events.set(jobId, list);
      console.log(`  ← ${jobId.slice(0, 8)} ${event.type}`, JSON.stringify(event).slice(0, 220));
    } catch {
      // Ignore malformed events.
    }
    response.writeHead(200, { "content-type": "application/json" });
    response.end('{"ok":true}');
  });
});

async function waitForHealth(url, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Not up yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`timed out waiting for ${url}`);
}

function waitForDone(jobId, timeoutMs = 240_000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      const list = events.get(jobId) ?? [];
      if (list.some((event) => event.type === "done")) {
        clearInterval(timer);
        resolve(list);
      } else if (Date.now() > deadline) {
        clearInterval(timer);
        reject(new Error(`job ${jobId} did not finish in time`));
      }
    }, 250);
  });
}

async function submit(kind, episodes, botToken) {
  const jobId = `smoke-${kind}-${Date.now()}`;
  const body = {
    jobId,
    kind,
    url: `${SITE}/series`,
    episodes,
    chatId: 1,
    callbackUrl: `http://127.0.0.1:${EVENTS_PORT}/internal/jobs/${jobId}/event`,
    jobToken: "smoke-token",
    botToken,
  };
  const response = await fetch(`http://127.0.0.1:${RUNNER_PORT}/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status !== 202) {
    throw new Error(`container rejected job: ${response.status} ${await response.text()}`);
  }
  return jobId;
}

const runner = spawn(process.execPath, ["dist/server.js"], {
  cwd: path.join(ROOT, "container"),
  env: {
    ...process.env,
    PORT: String(RUNNER_PORT),
    ALLOWED_HOSTS: "127.0.0.1",
    JOB_DIR,
  },
  stdio: ["ignore", "pipe", "pipe"],
});
runner.stdout.on("data", (chunk) => process.stdout.write(`[runner] ${chunk}`));
runner.stderr.on("data", (chunk) => process.stderr.write(`[runner] ${chunk}`));

let failed = false;
try {
  await ensureVideo();
  await new Promise((resolve) => siteServer.listen(SITE_PORT, "127.0.0.1", resolve));
  await new Promise((resolve) => eventServer.listen(EVENTS_PORT, "127.0.0.1", resolve));
  await waitForHealth(`http://127.0.0.1:${RUNNER_PORT}/health`);
  console.log("container is healthy\n");

  console.log("1) resolve job");
  const resolveId = await submit("resolve", null, "123:invalid");
  const resolveEvents = await waitForDone(resolveId);
  const resolved = resolveEvents.find((event) => event.type === "resolved");
  const resolveDone = resolveEvents.find((event) => event.type === "done");
  if (!resolved || resolved.episodes?.length !== 2) {
    throw new Error("resolve did not find 2 episodes");
  }
  if (!resolveDone?.ok) throw new Error("resolve job failed");
  console.log("   resolve ok\n");

  console.log("2) download job (expects to reach Telegram upload)");
  const downloadId = await submit("download", [1], "123:invalid");
  const downloadEvents = await waitForDone(downloadId);
  const states = new Set(
    downloadEvents
      .filter((event) => event.type === "episode")
      .map((event) => event.state),
  );
  for (const required of ["downloading", "encoding", "uploading"]) {
    if (!states.has(required)) throw new Error(`missing state: ${required}`);
  }
  const done = downloadEvents.find((event) => event.type === "done");
  console.log(`   states: ${[...states].join(", ")}`);
  console.log(`   final: ${done?.ok ? "ok" : done?.error ?? "failed"}`);
  if (!done || (done.ok && done.error)) throw new Error("unexpected final event");
  console.log("\nSMOKE PASSED");
} catch (error) {
  failed = true;
  console.error("\nSMOKE FAILED:", error);
} finally {
  runner.kill("SIGTERM");
  siteServer.close();
  eventServer.close();
  setTimeout(() => process.exit(failed ? 1 : 0), 500).unref();
}
