import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as BrowserError, async_playwright

UPLOAD_LIMIT = 47 * 1024 * 1024
MEDIA = re.compile(r"\.(?:m3u8|mp4|mpd|flv)$", re.I)
EPISODE_LABEL = re.compile(r"第\s*(\d+)\s*集|^ep\.?\s*(\d+)", re.I)

# Playwright's Node subprocess inherits this path when launched on Vercel.
bundled_lib = Path(__file__).resolve().parent / "runtime-lib"
if sys.platform == "linux" and (bundled_lib / "libatomic.so.1").is_file():
    current_path = os.environ.get("LD_LIBRARY_PATH")
    os.environ["LD_LIBRARY_PATH"] = f"{bundled_lib}:{current_path}" if current_path else str(bundled_lib)


class MediaError(Exception):
    pass


@dataclass
class Episode:
    number: int
    label: str
    url: str = ""
    selector: str = ""


async def open_page(service, page, url):
    service.source_url(url)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
    service.source_url(page.url)
    if response and response.status >= 400:
        raise MediaError(f"The source returned HTTP {response.status}.")


async def discover(page):
    grid = page.locator("[data-web-play-ep-grid] a[data-ep-id]:not(.is-locked)[href]")
    try:
        await grid.first.wait_for(state="attached", timeout=2500)
    except BrowserError:
        for label in ("选集", "選集", "剧集", "劇集", "全部剧集", "Episodes"):
            control = page.get_by_text(label, exact=True).first
            if await control.is_visible():
                await control.click(timeout=1500)
                break

    episodes = {}
    for item in await grid.evaluate_all("""nodes => nodes.map(node => ({
        id: node.dataset.epId, label: node.textContent.trim(), url: node.href
    }))"""):
        if item["id"].isdigit() and int(item["id"]) > 0:
            number = int(item["id"])
            episodes[number] = Episode(number, item["label"] or f"Episode {number}", item["url"])

    if not episodes:
        candidates = await page.locator("a, button, li, div, span").evaluate_all(
            """nodes => nodes
                .map(node => ({node, label: (node.innerText || '').trim()}))
                .filter(({label}) => label.length > 0 && label.length <= 25)
                .reverse()
                .map(({node, label}, index) => {
                    const target = node.closest('a, button, li') || node;
                    if (!target.dataset.hgEpisode) target.dataset.hgEpisode = String(index);
                    return {label, url: target.href || '', id: target.dataset.hgEpisode};
                })"""
        )
        for item in candidates:
            match = EPISODE_LABEL.search(item["label"])
            if match:
                number = int(match[1] or match[2])
                episodes.setdefault(
                    number, Episode(number, item["label"], item["url"], f'[data-hg-episode="{item["id"]}"]')
                )

    path = re.fullmatch(r"/video/(\d+)(?:/ep-([1-9]\d*))?/?", urlsplit(page.url).path)
    if not episodes:
        description = await page.locator('meta[name="description"]').evaluate_all(
            "nodes => nodes[0]?.content || ''"
        )
        count = re.search(r"全\s*(\d{1,4})\s*集", description)
        if path and count and 1 <= int(count[1]) <= 1000:
            base = urljoin(page.url, f"/video/{path[1]}")
            for number in range(1, int(count[1]) + 1):
                url = base if number == 1 else f"{base}/ep-{number}"
                episodes[number] = Episode(number, f"第{number}集", url)

    title = re.split(r"[-|]", await page.title(), maxsplit=1)[0].strip() or "Series"
    if not episodes:
        number = int(path[2]) if path and path[2] else 1
        episodes[number] = Episode(number, f"Episode {number}", page.url)
    return title, dict(sorted(episodes.items()))


async def find_stream(service, page, episode, captured, seen):
    captured.clear()
    if episode.url:
        await open_page(service, page, episode.url)
    else:
        await page.locator(episode.selector).first.click(timeout=5000)

    for attempt in range(2):
        service.source_url(page.url)
        initial = await page.locator("#videoInitialData").evaluate_all(
            "nodes => nodes[0]?.textContent || '{}'"
        )
        try:
            data = json.loads(initial)
        except ValueError:
            data = {}
        sources = [data.get("url"), data.get("hevc_url")] if isinstance(data, dict) else []
        sources += await page.locator("video, source").evaluate_all(
            "nodes => nodes.map(node => node.currentSrc || node.src).filter(Boolean)"
        )
        sources += sorted(captured, key=lambda url: ".m3u8" not in url.lower())
        for source in sources:
            if isinstance(source, str) and source:
                url = urljoin(page.url, source)
                parsed = urlsplit(url)
                if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and url not in seen:
                    seen.add(url)
                    return url

        if attempt == 0:
            for selector in ("video", ".vjs-big-play-button", ".dplayer-video-wrap", ".plyr__control--overlaid", "[aria-label='Play']"):
                control = page.locator(selector).first
                if await control.is_visible():
                    try:
                        await control.click(timeout=2000, force=True)
                        break
                    except BrowserError:
                        continue
            await asyncio.sleep(3)

    raise MediaError("No video stream was found for this episode.")


async def command(*args, timeout, failure):
    windows = sys.platform == "win32"
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if windows else {"start_new_session": True}),
        )
    except FileNotFoundError:
        raise MediaError(f"Install {args[0]} and make sure it is on PATH.") from None

    output = asyncio.create_task(process.communicate())
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(output), timeout)
    except (asyncio.CancelledError, TimeoutError) as error:
        try:
            if windows and process.returncode is None:
                stop = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if await stop.wait() and process.returncode is None:
                    process.kill()
            elif not windows:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await output
        if isinstance(error, TimeoutError):
            raise MediaError(failure + " The command timed out.") from None
        raise
    if process.returncode:
        raise MediaError(failure)
    return stdout


async def prepare_video(stream, referer, directory, number):
    await command(
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
        "--quiet", "--no-progress", "--socket-timeout", "20", "--retries", "5",
        "--fragment-retries", "5", "--concurrent-fragments", "8", "--merge-output-format", "mp4",
        "-f", "bv*+ba/b", "--referer", referer, "-o", str(directory / "source.%(ext)s"),
        "--", stream, timeout=1200, failure="Episode download failed.",
    )
    sources = [path for path in directory.glob("source.*") if not path.name.endswith((".part", ".ytdl"))]
    if not sources:
        raise MediaError("The download did not produce a video.")
    source = max(sources, key=lambda path: path.stat().st_size)
    probe = await command(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height:format=duration", "-of", "json", str(source),
        timeout=30, failure="The downloaded video could not be read.",
    )
    try:
        info = json.loads(probe)
        height = int(info["streams"][0]["height"])
        duration = float(info["format"]["duration"])
        if height < 2 or not math.isfinite(duration) or duration <= 0:
            raise ValueError
    except (KeyError, IndexError, ValueError, TypeError):
        raise MediaError("The download did not contain a playable video.") from None

    output = directory / f"episode-{number}.mp4"
    for target in (45, 35, 25):
        bitrate = min(2500, int(target * 1024**2 * 8 / duration / 1000) - 72)
        if bitrate < 80:
            break
        scaled_height = min(height, 480 if bitrate >= 450 else 360 if bitrate >= 240 else 240)
        await command(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", f"scale=-2:{scaled_height // 2 * 2}", "-c:v", "libx264", "-preset", "fast",
            "-pix_fmt", "yuv420p", "-b:v", f"{bitrate}k", "-maxrate", f"{bitrate}k",
            "-bufsize", f"{bitrate * 2}k", "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart", str(output),
            timeout=1800, failure="Episode compression failed.",
        )
        if 0 < output.stat().st_size <= UPLOAD_LIMIT:
            return output
    raise MediaError("The episode is too large for Telegram.")


async def run_media_job(service, job):
    job.progress = {"type": "stage", "state": "starting_playwright"}
    try:
        async with async_playwright() as playwright:
            job.progress = {"type": "stage", "state": "launching_chromium"}
            try:
                browser = await playwright.chromium.launch()
            except BrowserError as error:
                detail = str(error)
                if "Executable doesn't exist" in detail:
                    raise MediaError("Chromium is missing from the runtime. Install it with python -m playwright install chromium.") from None
                if "missing dependencies" in detail.lower() or "error while loading shared libraries" in detail.lower():
                    raise MediaError("Chromium is missing required system libraries.") from None
                raise MediaError("Chromium failed to start.") from None
            try:
                await process_series(service, job, browser)
            finally:
                await browser.close()
    except (OSError, NotImplementedError) as error:
        if job.progress["state"] == "starting_playwright":
            raise MediaError(f"The runtime could not start the Playwright driver ({type(error).__name__}).") from None
        raise
    except BrowserError:
        raise MediaError("Chromium could not open or read the series page.") from None
    except ValueError:
        raise MediaError("The series redirected to an invalid or disallowed URL.") from None
    except Exception as error:
        if (
            job.progress["state"] == "starting_playwright"
            and "Connection closed while reading from the driver" in str(error)
        ):
            raise MediaError("The Playwright driver exited during startup. Check the deployment's runtime logs.") from None
        raise


async def process_series(service, job, browser):
    page = await browser.new_page()
    captured = {}

    def capture(response):
        url = response.url
        mime = response.headers.get("content-type", "").lower()
        if MEDIA.search(urlsplit(url).path) or any(
            value in mime for value in ("mpegurl", "video/mp4", "dash+xml")
        ):
            captured[url] = None

    page.on("response", capture)
    job.progress = {"type": "stage", "state": "resolving"}
    await open_page(service, page, job.url)
    title, episodes = await discover(page)
    job.resolved = {
        "title": title,
        "episodes": [{"number": ep.number, "label": ep.label} for ep in list(episodes.values())[:300]],
        "note": None,
    }
    if job.kind == "resolve":
        return

    job.progress = {"type": "stage", "state": "checking_media_tools"}
    missing = [binary for binary in ("ffmpeg", "ffprobe") if not shutil.which(binary)]
    if missing:
        raise MediaError(f"Missing from the runtime PATH: {', '.join(missing)}.")

    selected = job.episodes or list(episodes)[:10]
    seen = set()
    failures = []
    for number in selected:
        try:
            episode = episodes.get(number)
            if not episode:
                raise MediaError("Episode is not listed on this page.")
            job.progress = {"type": "episode", "state": "downloading", "number": number}
            stream = await find_stream(service, page, episode, captured, seen)
            with TemporaryDirectory(prefix="series-bot-") as temp:
                video = await prepare_video(stream, page.url, Path(temp), number)
                job.progress["state"] = "uploading"
                await service.upload(job.chat_id, video, f"{title}\n{episode.label}")
            job.progress["state"] = "done"
        except (MediaError, BrowserError, ValueError) as error:
            reason = str(error) if isinstance(error, MediaError) else "Could not open the episode."
            job.progress = {"type": "episode", "number": number, "state": "failed", "error": reason}
            failures.append(f"{number}: {reason}")
    if failures:
        raise MediaError("Failed episodes: " + "; ".join(failures))
