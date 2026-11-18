import { readdir, readFile } from "node:fs/promises";
import { Sandbox } from "@vercel/sandbox";

const source = new URL("../container/", import.meta.url);
const workdir = "/vercel/sandbox/media-worker";
const browsers = "/vercel/sandbox/media-browsers";

if (!process.env.VERCEL_OIDC_TOKEN) {
  throw new Error("Run vercel link and vercel env pull .env.vercel.local first.");
}

const sources = await readdir(new URL("src/", source));
const files = ["package.json", "package-lock.json", "tsconfig.json",
  ...sources.filter((name) => name.endsWith(".ts")).map((name) => `src/${name}`)];
const payload = await Promise.all(files.map(async (file) => ({
  path: `${workdir}/${file}`,
  content: await readFile(new URL(file, source)),
})));

const sandbox = await Sandbox.create({ timeout: 15 * 60_000 });

async function run(cmd, args, cwd = workdir) {
  console.log(`Running ${cmd} ${args.join(" ")}`);
  const result = await sandbox.runCommand({
    cmd, args, cwd, env: { PLAYWRIGHT_BROWSERS_PATH: browsers },
    stdout: process.stdout, stderr: process.stderr,
  });
  if (result.exitCode !== 0) throw new Error(`${cmd} exited with code ${result.exitCode}`);
}

let saved = false;
try {
  await run("mkdir", ["-p", `${workdir}/src`], "/vercel/sandbox");
  await sandbox.writeFiles(payload);
  await run("sudo", ["apt-get", "update"]);
  await run("sudo", ["apt-get", "install", "-y", "ffmpeg"]);
  await run("curl", [
    "--fail", "--silent", "--show-error", "--location",
    "--output", `${workdir}/yt-dlp`,
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
  ]);
  await run("sudo", ["install", "-m", "0755", `${workdir}/yt-dlp`, "/usr/local/bin/yt-dlp"]);
  await run("rm", [`${workdir}/yt-dlp`]);
  await run("npm", ["ci"]);
  await run("npx", ["playwright", "install", "--with-deps", "chromium"]);
  await run("npm", ["run", "build"]);
  await run("npm", ["prune", "--omit=dev"]);
  await run("yt-dlp", ["--version"]);
  await run("ffmpeg", ["-version"]);
  await run("ffprobe", ["-version"]);
  await run("node", ["--input-type=module", "-e",
    "import { chromium } from 'playwright'; const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] }); await browser.close();",
  ]);

  const snapshot = await sandbox.snapshot({ expiration: 0 });
  saved = true;
  console.log(`MEDIA_SANDBOX_SNAPSHOT_ID=${snapshot.snapshotId}`);
} finally {
  if (!saved) {
    await sandbox.stop().catch((error) => console.error("Could not stop setup sandbox", error));
  }
}
