# Telegram bot + API (serverless)

Minimal serverless version of Series Downloader: a Telegram bot and JSON API that
download, compress, and send episodes to your chat.

Built only on the parts of the desktop app that matter:

- episode-list detection and per-episode stream sniffing (Playwright)
- yt-dlp download options from `download_core.py`
- ffprobe playability check

Everything else (Tkinter UI, library DB, existing-media reuse, cleanup, updater,
checkpoints) is intentionally not here.

## How it works

```
Telegram ──webhook──> Worker (src/) ──RPC──> Runner Durable Object
                        │  /telegram/webhook      │  queue + progress + messages
                        │  /v1/*                  │
                        │                         ▼
                        └── progress <────── Container (container/)
                                               Chromium + yt-dlp + ffmpeg
                                               download → verify → compress → sendVideo
```

- One `Runner` Durable Object (`global`) processes **one job at a time**, so costs
  stay predictable and only one 200–300 MB file is ever on disk.
- The container reports progress to `/internal/jobs/:id/event`, signed with an
  HMAC job token. The Worker edits the Telegram message at most every 8 seconds.
- Videos are compressed to fit Telegram's **50 MB bot upload limit** (45 MiB
  target, 480p/360p/240p chosen from duration). No R2, no file links.

## Requirements

- Cloudflare account on the **Workers Paid** plan (`$5/mo`) — Containers requires it.
- Node.js 20+ and Docker Desktop (image builds locally with `wrangler deploy`).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).

## Setup

```powershell
cd serverless
npm install

# 1. Secrets
npx wrangler secret put TELEGRAM_BOT_TOKEN       # from @BotFather
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET  # random string, e.g. `openssl rand -hex 24`
npx wrangler secret put API_KEY                  # random string for /v1/*
npx wrangler secret put INTERNAL_SECRET          # random string for job callbacks
npx wrangler secret put ALLOWED_USER_IDS         # your Telegram user id(s), comma separated
# optional
npx wrangler secret put ALLOWED_CHAT_IDS         # defaults to ALLOWED_USER_IDS
npx wrangler secret put DEFAULT_CHAT_ID          # used by /v1 when chat_id is omitted

# 2. Deploy (builds and pushes the container image)
npm run deploy
```

Copy `.dev.vars.example` to `.dev.vars` for `npm run dev` (requires Docker).

### Deploy from the Cloudflare dashboard instead

This is a **Worker**, so after the first deploy it appears under **Workers & Pages**
as the `hg-telegram-bot` project. It is not a Pages project: Pages/Functions cannot
run Docker images or this queue's Durable Object.

You can also skip local Docker by letting **Workers Builds** build the image:

1. Push this repo to GitHub/GitLab, then in the dashboard go to
   **Workers & Pages → Create → Connect to Git** and pick the repo.
2. Set **Root directory** to `serverless` and the **Deploy command** to
   `npx wrangler deploy` (Workers Builds can build the Dockerfile itself).
3. Add the secrets under **Settings → Variables and Secrets** (encrypted) instead
   of running `wrangler secret put`.
4. Push to the production branch; the first build takes several minutes while the
   image is built, pushed, and provisioned.

Previews for branches are limited: a non-production build runs
`wrangler versions upload`, which uploads Worker code only — no image, and no
Version URL for Durable Object Workers. Use `wrangler dev` or a separate staging
Worker/env for testing.

### Register the webhook

```powershell
$env:TOKEN="<bot token>"
$env:SECRET="<TELEGRAM_WEBHOOK_SECRET>"
$env:URL="https://hg-telegram-bot.<account>.workers.dev/telegram/webhook"

curl.exe -s "https://api.telegram.org/bot$env:TOKEN/setWebhook" `
  --data-urlencode "url=$env:URL" `
  --data-urlencode "secret_token=$env:SECRET" `
  --data-urlencode 'allowed_updates=["message"]'
```

Check it with `getWebhookInfo`. Re-run `setWebhook` after changing the worker
hostname or the secret.

## Bot commands

| Command | Action |
|---|---|
| `/start`, `/help` | usage |
| `/episodes <url>` | list detected episodes (no download) |
| `/dl <url> 1,3-5` | download up to **10** episodes, compressed and sent |
| `/dl <url>` | first 10 episodes |
| `/status` | active job + queue |
| `/cancel` | stop the running or queued job |

Compression: target 45 MiB, H.264 main + AAC mono, `+faststart`, height 720/480/
360/240 depending on duration. Episodes longer than ~22 minutes are rejected
because they cannot fit under 50 MB at 240p.

## REST API

All `/v1/*` routes require `X-API-Key`. Jobs always deliver to a Telegram chat,
so the API is for automation around your own bot, not a public file host.

| Method | Path | Body | Result |
|---|---|---|---|
| `POST` | `/v1/resolve` | `{"url":"https://huangguoai.com/video/123","chat_id":123}` | `202 {jobId, poll}` |
| `POST` | `/v1/jobs` | `{"url":"...","episodes":"1-3","chat_id":123}` | `202 {jobId, poll}` |
| `GET` | `/v1/jobs/{id}` | — | status, title, per-episode state |
| `DELETE` | `/v1/jobs/{id}` | — | cancel |
| `GET` | `/healthz` | — | `{"ok":true}` |

`episodes` accepts `"1,3-5"` or `[1,3,4,5]`; omit it for the first 10 episodes.

```powershell
curl.exe -s -X POST "https://<worker>/v1/jobs" `
  -H "content-type: application/json" `
  -H "x-api-key: $env:API_KEY" `
  -d '{"url":"https://huangguoai.com/video/123","episodes":"1-2"}'
```

Note: resolve is asynchronous (the first container start includes cold start and
a real page load), so poll `GET /v1/jobs/{id}` for the episode list.

## Configuration

| Variable | Where | Meaning |
|---|---|---|
| `ALLOWED_HOSTS` | `wrangler.jsonc` var | comma-separated source hosts (default `huangguoai.com`) |
| `TELEGRAM_BOT_TOKEN` | secret | bot token (also handed to the container per job) |
| `TELEGRAM_WEBHOOK_SECRET` | secret | checked against `X-Telegram-Bot-Api-Secret-Token` |
| `API_KEY` | secret | `X-API-Key` for `/v1/*` |
| `INTERNAL_SECRET` | secret | HMAC key for container → worker progress callbacks |
| `ALLOWED_USER_IDS` | secret | Telegram user ids allowed to use the bot |
| `ALLOWED_CHAT_IDS` | secret | chats that `/v1` may deliver to (defaults to users) |
| `DEFAULT_CHAT_ID` | secret | chat used when a `/v1` call omits `chat_id` |
| `PUBLIC_BASE_URL` | secret | callback base URL; defaults to the request origin |

Container instance: `standard-2` (1 vCPU, 6 GiB, 12 GB disk), 30 min inactivity
timeout. No maximum runtime; the DO watchdog fails a job if no progress arrives
for 5 minutes.

## Cost

Workers Paid includes 25 GiB-h memory, 375 vCPU-min and 200 GB-h disk per month.
`standard-2` covers roughly **4 hours of container time**; a 3-episode job is
about 6–10 minutes. Overage is around `$0.07/h`. Telegram uploads are egress from
the container and stay inside the included 1 TB for North America/Europe.

## Testing

```powershell
npm test        # pure helpers: episode ranges, URL/host policy, tokens, encoder plan
npm run typecheck
```

Local end-to-end check of the container (fake series page + MP4, no Cloudflare,
no Telegram, no Docker):

```powershell
npx playwright install chromium   # once, from serverless/container
npm run smoke:container
```

It must print `SMOKE PASSED`; the download job ends at "Telegram refused" on
purpose because the token is fake. That proves resolve, yt-dlp, ffprobe, encode
and the upload path all work.

End-to-end check with a real episode:

1. `npm run dev` (Docker running) and set the webhook to a tunnel URL, or deploy.
2. `/episodes <url>` → episode list appears.
3. `/dl <url> 1` → progress message, then one compressed video.
4. `/dl <url> 1-2`, then `/cancel` mid-download → job stops and reports cancelled.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Webhook returns 403 | `secret_token` in `setWebhook` differs from `TELEGRAM_WEBHOOK_SECRET` |
| "Could not start the download worker" | image build/push failed, or the container crashed — check `wrangler tail` |
| "No video stream was found" | episode requires login/VIP, or the site changed its player |
| "too long to fit Telegram's 50 MB limit" | episode exceeds ~22 min at 240p |
| Job fails after ~5 min with "worker stopped responding" | host restart; send `/dl` again |
| `sendVideo` fails, document arrives | Telegram rejected inline playback; file is still delivered |

## Differences from the desktop app

- No library, no existing-file reuse, no checkpoints/resume: a restarted job
  re-downloads the current episode.
- No HEVC fallback, priorities, pause, exports, or Recycle Bin cleanup.
- No browser cookies: only public episodes work.
- Quality is capped by the 50 MB Telegram limit.
