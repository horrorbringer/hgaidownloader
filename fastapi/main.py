import asyncio
import logging
import re
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
HELP = (
    "/episodes URL — list episodes\n"
    "/dl URL 1,3-5 — download selected episodes\n"
    "/dl URL — download the first 10 episodes\n"
    "/status — show active jobs\n"
    "/cancel — cancel active jobs"
)


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


class Message(BaseModel):
    chat: Chat
    sender: Chat | None = Field(default=None, alias="from")
    text: str | None = None


class Update(BaseModel):
    update_id: StrictInt
    message: Message | None = None


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
        self.active_task = None

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

    async def send(self, chat_id, message):
        if not self.bot_token:
            raise HTTPException(503, "Telegram bot token is missing.")
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message[:2000]},
            )
            response.raise_for_status()
            if response.json().get("ok"):
                return
        except (httpx.HTTPError, ValueError):
            pass
        raise HTTPException(502, "Telegram message delivery failed.")

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
                        job.error = "Media processing failed."
                        logger.warning("Job %s failed with %s", job.id, type(error).__name__)
                    finally:
                        self.active_task = None
                if job.kind == "resolve" and job.status == "done" and job.resolved:
                    episodes = job.resolved["episodes"] or []
                    listing = ", ".join(str(episode["number"]) for episode in episodes)
                    message = f"{job.resolved['title'] or 'Series'}\nEpisodes: {listing or 'none'}"
                else:
                    message = f"Job {job.id}: {job.status}."
                    if job.error:
                        message += f" {job.error}"
                try:
                    await self.send(job.chat_id, message)
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
        await handle_message(service, update.message)
    except Exception:
        service.updates.pop(update.update_id, None)
        raise
    if len(service.updates) > 4096:
        service.updates.popitem(last=False)
    return {"ok": True}


async def handle_message(service, message):
    if not message or not message.sender or not message.text or not message.text.strip():
        return
    chat_id = message.chat.id
    if message.sender.id not in service.users or (service.chats and chat_id not in service.chats):
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
        await service.send(chat_id, "\n".join(lines) or "No active jobs.")
        return
    if command in {"/cancel", "/stop"}:
        for job in jobs:
            if job.status in ACTIVE:
                service.cancel(job)
        await service.send(chat_id, "Cancellation requested." if jobs else "No active jobs.")
        return
    if command in {"/episodes", "/resolve", "/dl", "/download"}:
        kind = "resolve" if command in {"/episodes", "/resolve"} else "download"
        url = parts[1] if len(parts) > 1 else ""
        selection = parts[2] if len(parts) > 2 and kind == "download" else None
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
    try:
        await service.send(chat_id, f"Queued {kind} job {job.id}.")
    except HTTPException:
        logger.warning("Could not deliver acknowledgement for job %s", job.id)


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

