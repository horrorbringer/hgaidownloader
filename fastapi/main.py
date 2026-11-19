import asyncio
import logging
import re
import traceback
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hmac import compare_digest
from os import getenv
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from media import MediaError, run_media_job

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "running", "cancelling"}
LOOKUP_COMMANDS = {"/episodes", "/eps", "/resolve"}
HELP = (
    "Series Downloader\n\n"
    "1. Send /episodes followed by a series URL.\n"
    "2. Tap an episode or Download first 10.\n"
    "   For a custom selection, reply with 1,3-5.\n\n"
    "/dl URL 1,3-5 — download directly\n"
    "/status — view active jobs\n"
    "/cancel — cancel active jobs"
)
EPISODE_RANGE_TEXT = re.compile(r"[0-9,\s-]+")
SERIES_URL_PREFIX = "Series URL: "


def episode_buttons(episodes):
    numbers = [episode["number"] for episode in episodes[:10]]
    rows = [
        [{"text": str(number), "callback_data": f"download:{number}"} for number in numbers[i:i + 5]]
        for i in range(0, len(numbers), 5)
    ]
    if len(numbers) > 1:
        rows.append([{"text": f"Download first {len(numbers)}", "callback_data": "download:default"}])
    return rows


def id_list(name):
    return {int(value) for value in getenv(name, "").split(",") if value.strip()}


def authorized(value, secret):
    return bool(secret) and compare_digest(value.encode(), secret.encode())


def episode_numbers(value):
    if isinstance(value, list):
        numbers = set(value)
    elif value and value.strip():
        numbers = set()
        for part in value.split(","):
            match = re.fullmatch(r"\s*([0-9]{1,16})(?:\s*-\s*([0-9]{1,16}))?\s*", part)
            if not match:
                raise ValueError("Use episode numbers like 1,3-5.")
            first, last = int(match[1]), int(match[2] or match[1])
            if first < 1 or last < first or last - first >= 10:
                raise ValueError("Select up to 10 positive episode numbers.")
            numbers.update(range(first, last + 1))
    else:
        return None
    if len(numbers) > 10 or any(n < 1 or n > 2**53 - 1 for n in numbers):
        raise ValueError("Select up to 10 valid episode numbers.")
    return sorted(numbers) or None


class Chat(BaseModel):
    id: StrictInt
    is_bot: bool = False


class MessageContent(BaseModel):
    chat: Chat
    sender: Chat | None = Field(default=None, alias="from")
    text: str | None = None


class Message(MessageContent):
    reply_to_message: MessageContent | None = None


class CallbackQuery(BaseModel):
    id: str
    sender: Chat = Field(alias="from")
    message: MessageContent | None = None
    data: str | None = None


class Update(BaseModel):
    update_id: StrictInt
    message: Message | None = None
    callback_query: CallbackQuery | None = None


class JobInput(BaseModel):
    url: str
    episodes: str | list[StrictInt] | None = None
    chat_id: StrictInt | None = None


@dataclass
class Job:
    kind: str
    url: str
    chat_id: int
    episodes: list[int] | None
    id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "queued"
    resolved: dict | None = None
    progress: dict | None = None
    error: str | None = None

    def view(self):
        return {
            "jobId": self.id,
            "chatId": self.chat_id,
            "kind": self.kind,
            "status": self.status,
            "resolved": self.resolved,
            "progress": self.progress,
            "error": self.error,
        }


class Service:
    def __init__(self, client):
        self.client = client
        self.bot_token = getenv("TELEGRAM_BOT_TOKEN", "")
        self.webhook_secret = getenv("TELEGRAM_WEBHOOK_SECRET", "")
        self.api_key = getenv("API_KEY", "")
        self.users = id_list("ALLOWED_USER_IDS")
        self.chats = id_list("ALLOWED_CHAT_IDS")
        self.default_chat = int(getenv("DEFAULT_CHAT_ID") or 0)
        self.hosts = {
            host.strip().lower().lstrip(".")
            for host in getenv("ALLOWED_HOSTS", "huangguoai.com").split(",")
            if host.strip().lstrip(".")
        }
        self.jobs = {}
        self.queue = asyncio.Queue(maxsize=100)
        self.updates = OrderedDict()
        self.recent_series: OrderedDict[tuple[int, int], str] = OrderedDict()
        self.active_task = None

    def remember_series(self, chat_id, user_id, url):
        key = (chat_id, user_id)
        self.recent_series[key] = url
        self.recent_series.move_to_end(key)
        if len(self.recent_series) > 1024:
            self.recent_series.popitem(last=False)

    def source_url(self, value):
        value = value.strip()
        if not value or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
            raise ValueError("Send a valid series URL.")
        if "://" not in value:
            value = "https://" + value
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.port not in {None, 80, 443}
            or not any(hostname == host or hostname.endswith("." + host) for host in self.hosts)
        ):
            raise ValueError("Use an HTTP(S) URL from an allowed source host.")
        return value

    async def telegram(self, method, payload):
        if not self.bot_token:
            raise HTTPException(503, "Telegram bot token is missing.")
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.bot_token}/{method}", json=payload,
            )
            response.raise_for_status()
            if response.json().get("ok"):
                return
        except (httpx.HTTPError, ValueError):
            pass
        raise HTTPException(502, "Telegram message delivery failed.")

    async def send(self, chat_id, message, buttons=None):
        payload = {
            "chat_id": chat_id,
            "text": message[:4000],
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        await self.telegram("sendMessage", payload)

    async def answer_callback(self, callback_id, text, alert=False):
        try:
            await self.telegram(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert},
            )
        except HTTPException:
            logger.warning("Could not acknowledge a Telegram button press")

    def enqueue(self, kind, raw_url, episodes, chat_id):
        url = self.source_url(raw_url)
        selection = episode_numbers(episodes)
        if not self.bot_token:
            raise HTTPException(503, "Telegram bot token is missing.")
        if self.queue.full():
            raise HTTPException(503, "The job queue is full.")
        job = Job(kind, url, chat_id, selection)
        self.jobs[job.id] = job
        self.queue.put_nowait(job)
        return job

    async def run_jobs(self):
        while True:
            job = await self.queue.get()
            try:
                if job.status == "queued":
                    job.status = "running"
                    self.active_task = asyncio.create_task(run_media_job(self, job))
                    try:
                        await self.active_task
                        job.status = "done"
                    except asyncio.CancelledError:
                        job.status = "cancelled"
                        if asyncio.current_task().cancelling():
                            raise
                    except MediaError as error:
                        job.status = "failed"
                        job.error = str(error)
                    except Exception as error:
                        job.status = "failed"
                        stage = (job.progress or {}).get("state", "starting")
                        job.error = f"Media processing failed during {stage.replace('_', ' ')}."
                        frames = traceback.extract_tb(error.__traceback__)
                        logger.warning(
                            "Job %s failed during %s (%s). Trace: %s",
                            job.id, stage, type(error).__name__,
                            " -> ".join(f"{frame.name}:{frame.lineno}" for frame in frames[-8:]),
                        )
                    finally:
                        self.active_task = None
                buttons = None
                if job.kind == "resolve" and job.status == "done" and job.resolved:
                    episodes = job.resolved["episodes"] or []
                    listing = ", ".join(str(episode["number"]) for episode in episodes)
                    message = (
                        f"{SERIES_URL_PREFIX}{job.url}\n\n"
                        f"{job.resolved['title'] or 'Series'}\n"
                        f"Episodes: {listing or 'none'}\n\n"
                        "Tap an episode or reply with a range like 1,3-5."
                    )
                    buttons = episode_buttons(episodes)
                else:
                    heading = "Download" if job.kind == "download" else "Episode lookup"
                    message = f"{heading} {job.status}.\nJob: {job.id}"
                    if job.error:
                        message += f"\n\n{job.error}"
                try:
                    await self.send(job.chat_id, message, buttons)
                except HTTPException:
                    logger.warning("Could not deliver completion for job %s", job.id)
            finally:
                self.queue.task_done()
                if len(self.jobs) > 1000:
                    for job_id, previous in self.jobs.items():
                        if previous.status not in ACTIVE:
                            del self.jobs[job_id]
                            break

    async def upload(self, chat_id, file_path, caption):
        for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
            try:
                with file_path.open("rb") as video:
                    response = await self.client.post(
                        f"https://api.telegram.org/bot{self.bot_token}/{method}",
                        data={"chat_id": str(chat_id), "caption": caption[:500]},
                        files={field: (file_path.name, video, "video/mp4")},
                        timeout=180,
                    )
                result = response.json()
                if response.is_success and result.get("ok"):
                    return
                if method == "sendVideo" and response.status_code == 400:
                    continue
            except (httpx.HTTPError, ValueError):
                pass
            break
        raise MediaError("Telegram video delivery failed.")

    def cancel(self, job):
        if job.status == "queued":
            job.status = "cancelled"
        elif job.status == "running":
            job.status = "cancelling"
            self.active_task.cancel()
        elif job.status != "cancelling":
            raise HTTPException(404, "Job is already finished.")


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        service = Service(client)
        app.state.service = service
        runner = asyncio.create_task(service.run_jobs())
        try:
            yield
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


app = FastAPI(title="Series Downloader Bot API", lifespan=lifespan)


def get_service(request: Request):
    return request.app.state.service


def api_service(
    service: Service = Depends(get_service),
    x_api_key: str = Header(default=""),
):
    if not authorized(x_api_key, service.api_key):
        raise HTTPException(401, "Unauthorized.")
    return service


@app.get("/")
def index():
    return {"service": "Series Downloader API", "health": "/healthz"}


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def webhook(
    update: Update, service: Service = Depends(get_service),
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if not authorized(x_telegram_bot_api_secret_token, service.webhook_secret):
        raise HTTPException(403, "Forbidden.")
    if update.update_id in service.updates:
        return {"ok": True}
    service.updates[update.update_id] = None
    try:
        if update.callback_query:
            await handle_callback(service, update.callback_query)
        else:
            await handle_message(service, update.message)
    except Exception:
        service.updates.pop(update.update_id, None)
        raise
    if len(service.updates) > 4096:
        service.updates.popitem(last=False)
    return {"ok": True}


def episode_list_url(message):
    if message and message.text and message.sender and message.sender.is_bot:
        first_line = message.text.partition("\n")[0]
        if first_line.startswith(SERIES_URL_PREFIX):
            return first_line.removeprefix(SERIES_URL_PREFIX)
    return None


def replied_series(message):
    reply = message.reply_to_message
    if not reply or not reply.text or not reply.sender:
        return None
    url = episode_list_url(reply)
    if url:
        return url
    parts = reply.text.split()
    if (
        reply.sender.id == message.sender.id
        and len(parts) >= 2
        and parts[0].split("@", 1)[0].lower() in LOOKUP_COMMANDS
    ):
        return parts[1]
    return None


def allowed_chat_user(service, chat_id, user_id):
    return user_id in service.users and (not service.chats or chat_id in service.chats)


async def announce_job(service, job):
    label = "Download queued" if job.kind == "download" else "Episode lookup queued"
    buttons = [[{"text": "Cancel job", "callback_data": f"cancel:{job.id}"}]]
    try:
        await service.send(job.chat_id, f"{label}\nJob: {job.id}", buttons)
    except HTTPException:
        logger.warning("Could not deliver acknowledgement for job %s", job.id)


async def handle_callback(service, callback):
    message = callback.message
    if not message or not allowed_chat_user(service, message.chat.id, callback.sender.id):
        await service.answer_callback(callback.id, "This action is unavailable.")
        return

    action, _, value = (callback.data or "").partition(":")
    if action == "cancel":
        job = service.jobs.get(value)
        if not job or job.chat_id != message.chat.id or job.status not in ACTIVE:
            await service.answer_callback(callback.id, "This job is no longer available to cancel.")
            return
        service.cancel(job)
        await service.answer_callback(callback.id, "Cancellation requested.")
        return

    url = episode_list_url(message)
    if action != "download" or not url or not (value == "default" or value.isascii() and value.isdecimal()):
        await service.answer_callback(callback.id, "Run /episodes again to get a new episode list.")
        return
    try:
        job = service.enqueue("download", url, None if value == "default" else value, message.chat.id)
    except (ValueError, HTTPException) as error:
        await service.answer_callback(callback.id, str(error) if isinstance(error, ValueError) else error.detail, alert=True)
        return
    await service.answer_callback(callback.id, "Download queued.")
    await announce_job(service, job)


async def handle_message(service, message):
    if not message or not message.sender or not message.text or not message.text.strip():
        return
    chat_id = message.chat.id
    if not allowed_chat_user(service, chat_id, message.sender.id):
        return
    text = message.text.strip()
    parts = text.split(maxsplit=2)
    command = parts[0].split("@", 1)[0].lower()
    jobs = [job for job in service.jobs.values() if job.chat_id == chat_id and job.status in ACTIVE]
    if command == "/status":
        lines = [
            f"{job.id}: {(job.progress or {}).get('state') or job.status}"
            if job.status == "running" else f"{job.id}: {job.status}"
            for job in jobs
        ]
        await service.send(chat_id, ("Active jobs\n\n" + "\n".join(lines)) if lines else "No active jobs.")
        return
    if command in {"/cancel", "/stop"}:
        for job in jobs:
            if job.status in ACTIVE:
                service.cancel(job)
        await service.send(chat_id, "Cancellation requested." if jobs else "No active jobs.")
        return
    if command in LOOKUP_COMMANDS:
        kind = "resolve"
        url = parts[1] if len(parts) > 1 else ""
        selection = None
    elif command in {"/dl", "/download"}:
        kind = "download"
        arguments = text[len(parts[0]):].strip()
        if not arguments or EPISODE_RANGE_TEXT.fullmatch(arguments):
            url = replied_series(message) or service.recent_series.get((chat_id, message.sender.id))
            if not url:
                await service.send(chat_id, "Send /episodes URL first, or reply to an episode list with /dl.")
                return
            selection = arguments or None
        else:
            url = parts[1]
            selection = parts[2] if len(parts) > 2 else None
    elif EPISODE_RANGE_TEXT.fullmatch(text) and (url := episode_list_url(message.reply_to_message)):
        kind, selection = "download", text
    elif re.match(r"^(?:https?://|[\w.-]+\.[\w-]+/)", text, re.I):
        kind, url, selection = "download", text, None
    else:
        await service.send(chat_id, HELP)
        return
    try:
        job = service.enqueue(kind, url, selection, chat_id)
    except (ValueError, HTTPException) as error:
        await service.send(chat_id, str(error) if isinstance(error, ValueError) else error.detail)
        return
    if kind == "resolve":
        service.remember_series(chat_id, message.sender.id, job.url)
    await announce_job(service, job)


def create_job(service, payload, kind):
    chat_id = payload.chat_id if payload.chat_id is not None else service.default_chat
    if not chat_id or chat_id not in (service.chats or service.users):
        raise HTTPException(400, "An allowed chat_id is required.")
    try:
        job = service.enqueue(kind, payload.url, payload.episodes, chat_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {"jobId": job.id, "chatId": chat_id, "status": job.status, "poll": f"/v1/jobs/{job.id}"}


@app.post("/v1/resolve", status_code=202)
async def resolve(payload: JobInput, service: Service = Depends(api_service)):
    return create_job(service, payload, "resolve")


@app.post("/v1/jobs", status_code=202)
async def download(payload: JobInput, service: Service = Depends(api_service)):
    return create_job(service, payload, "download")


def find_job(service, job_id):
    job = service.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@app.get("/v1/jobs/{job_id}")
async def status(job_id: str, service: Service = Depends(api_service)):
    return find_job(service, job_id).view()


@app.delete("/v1/jobs/{job_id}")
async def cancel(job_id: str, service: Service = Depends(api_service)):
    job = find_job(service, job_id)
    service.cancel(job)
    return {"jobId": job.id, "status": job.status}

