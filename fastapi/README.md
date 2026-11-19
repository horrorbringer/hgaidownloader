# FastAPI Telegram bot

The FastAPI app accepts Telegram webhooks and processes media jobs in its own Python process. It uses Playwright Chromium for episode discovery, yt-dlp for downloads, and FFmpeg for video compression.

## Run on Windows

Install Python 3.12 and FFmpeg. Verify that `ffmpeg` and `ffprobe` are on your PATH.

Open PowerShell in this folder. Install the Python dependencies and Chromium.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Copy the environment template and fill in the required settings in your private `.env` file.

```powershell
Copy-Item .env.example .env
```

Use your BotFather token and your numeric Telegram user ID. Use separate random values for the webhook secret and API key. Telegram accepts letters, digits, underscores, and hyphens in the webhook secret. Separate multiple IDs or hosts with commas. Set `ALLOWED_CHAT_IDS` to restrict the bot to specific chats. API jobs use these chat IDs when configured and otherwise use `ALLOWED_USER_IDS`.

Start FastAPI from the same folder.

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --env-file .env --host 127.0.0.1 --port 8000
```

The health check is at `http://127.0.0.1:8000/healthz` and interactive API docs are at `http://127.0.0.1:8000/docs`.

## Connect Telegram

Expose the API through an HTTPS reverse proxy or tunnel forwarding to port 8000. Register the webhook for your bot using `setWebhook`. In this PowerShell example, the bot token and webhook secret are already loaded in the shell environment.

```powershell
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot${env:TELEGRAM_BOT_TOKEN}/setWebhook" -ContentType "application/json" -Body (@{
    url = "https://YOUR_HOST/telegram/webhook"
    secret_token = $env:TELEGRAM_WEBHOOK_SECRET
    allowed_updates = @("message")
} | ConvertTo-Json)
```

Telegram supports one active webhook per bot. Registering this service replaces any webhook already configured for that token.

Send `/help` from an allowed Telegram account. Use `/episodes URL` to list episodes. Then reply to the episode list with `/dl 1,3-5` for a selection or `/dl` for the first ten. Replying to the list works across app restarts and Vercel instances. You can also send those commands as a new message while the latest lookup is still in memory. Use `/dl URL 1,3-5` to specify a different series. Sending a URL by itself queues the first ten episodes. Use `/status` or `/cancel` to manage the chat's jobs.

## JSON API

Send your API key in the `X-API-Key` header.

| Method | Route | Purpose |
| --- | --- | --- |
| POST | /v1/resolve | Queue episode discovery |
| POST | /v1/jobs | Queue a download |
| GET | /v1/jobs/{job_id} | Read job status and latest progress |
| DELETE | /v1/jobs/{job_id} | Cancel a job |

POST bodies accept `url`, optional `chat_id` (defaults to `DEFAULT_CHAT_ID`), and optional `episodes` (a range string or integer array). For example, `{"url":"https://huangguoai.com/video/123","episodes":"1,3-5","chat_id":123456789}`. POST returns HTTP 202 and a job ID. The destination chat must have started a conversation with the bot or added it to the group.

Run a single Uvicorn worker. Jobs and webhook deduplication are held in memory and reset on restart.



- TELEGRAM_WEBHOOK_SECRET verifies incoming requests from Telegram. Put it in .env and pass the same value as secret_token when registering the webhook with setWebhook.
- API_KEY protects your FastAPI /v1/* endpoints. Put it in .env and send it in the X-API-Key header when calling those endpoints. Telegram bot commands don’t use this key.

In PowerShell, run this command to generate two random values:

```python
python -c "import secrets; print('TELEGRAM_WEBHOOK_SECRET=' + secrets.token_hex(32)); print('API_KEY=' + secrets.token_hex(32))"
```
