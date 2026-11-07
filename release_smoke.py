"""Offline checks executed by the packaged executable as well as source builds."""

import functools
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path


def run_self_test(report_path=None):
    from app_runtime import resource_dir
    from app_version import APP_VERSION
    report = {"version": APP_VERSION, "checks": [], "passed": False}

    def check(name, action):
        started = time.monotonic()
        details = action()
        report["checks"].append({"name": name, "seconds": round(time.monotonic() - started, 2),
                                 "details": details})

    try:
        with tempfile.TemporaryDirectory(prefix="series-release-test-") as temporary:
            base = Path(temporary)

            def media_tools():
                found = {}
                for name in ("ffmpeg", "ffprobe"):
                    executable = shutil.which(name)
                    if not executable:
                        raise RuntimeError(f"Missing {name}")
                    found[name] = subprocess.check_output([executable, "-version"], text=True,
                                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).splitlines()[0]
                return found

            check("media tools", media_tools)

            def browser():
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    # Both the background and visible-browser options are shipped.
                    versions = []
                    for headless in (True, False):
                        instance = playwright.chromium.launch(headless=headless)
                        try:
                            page = instance.new_page()
                            page.set_content("<title>Release check</title><h1>Series Downloader</h1>")
                            if page.title() != "Release check":
                                raise RuntimeError("Chromium page did not load")
                            versions.append(instance.version)
                        finally:
                            instance.close()
                return versions

            check("bundled Chromium (headless and visible)", browser)

            def gui():
                import tkinter as tk
                from series_gui_all import SeriesDownloaderApp
                root = tk.Tk()
                root.withdraw()
                try:
                    app = SeriesDownloaderApp(root, base / "library.sqlite3")
                    root.update()
                    if len(app.icons) != 4:
                        raise RuntimeError("GUI icons are missing")
                    title = root.title()
                    app.progress.stop()
                    app.stop_progress_animation()
                    for job in root.tk.call("after", "info"):
                        root.after_cancel(job)
                finally:
                    root.destroy()
                return title

            check("Tkinter GUI and resources", gui)

            def recycle():
                from file_lifecycle import recycle_file
                disposable = base / "recycle-check.txt"
                disposable.write_text("Disposable release diagnostic file", encoding="utf-8")
                recycle_file(disposable)
                if disposable.exists():
                    raise RuntimeError("The diagnostic file was not recycled")
                return "Moved the diagnostic's own temporary file to the Recycle Bin"

            check("Recycle Bin integration", recycle)

            def updater():
                from urllib.parse import urlsplit
                from urllib.request import Request, urlopen
                from app_updates import ReleaseClient, UpdateError, download_update, launch_installer
                from app_version import UPDATE_REPOSITORY
                import hashlib
                version = f"{int(APP_VERSION.split('.')[0]) + 1}.0.0"
                filename = f"SeriesDownloader-{version}-windows-x64-setup.exe"
                prefix = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/v{version}/"
                payload = b"MZ" + b"local update diagnostic; never execute this file" * 200
                digest = hashlib.sha256(payload).hexdigest()
                metadata = [{"tag_name": "v" + version, "prerelease": False, "draft": False,
                    "assets": [{"name": filename, "state": "uploaded", "size": len(payload),
                                "digest": "sha256:" + digest, "browser_download_url": prefix + filename},
                               {"name": "SHA256SUMS.txt", "state": "uploaded",
                                "browser_download_url": prefix + "SHA256SUMS.txt"}]}]
                routes = {f"/repos/{UPDATE_REPOSITORY}/releases?per_page=20": json.dumps(metadata).encode(),
                          urlsplit(prefix + filename).path: payload,
                          urlsplit(prefix + "SHA256SUMS.txt").path: f"{digest}  {filename}\n".encode()}
                requested = []

                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        requested.append(self.path)
                        body = routes.get(self.path, b"unmodelled route")
                        self.send_response(200 if self.path in routes else 501)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, *_args):
                        pass

                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                def transport(request, *, timeout):
                    parsed = urlsplit(request.full_url)
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
                    return urlopen(Request(f"http://127.0.0.1:{server.server_port}" + path,
                                          headers=dict(request.headers)), timeout=timeout)

                try:
                    client = ReleaseClient(open_request=transport)
                    update = client.check(APP_VERSION)
                    if not update or update.version != version:
                        raise RuntimeError("The packaged updater did not discover the simulated release")
                    artifact = download_update(client, update, base / "update-cache", threading.Event())
                    if artifact.path.read_bytes() != payload or len(requested) != 3:
                        raise RuntimeError("The packaged updater failed its HTTP download check")
                    artifact.path.write_bytes(b"tampered; must never be executed")
                    try:
                        launch_installer(artifact)
                    except UpdateError:
                        pass
                    else:
                        raise RuntimeError("The updater accepted a tampered installer")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                return "Local HTTP release discovery, SHA-256 download, and tamper rejection; no installer executed"

            check("packaged updater against a local release server", updater)

            def hls():
                import download_core
                from series_gui_all import video_is_playable
                media = base / "media"
                media.mkdir()
                subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                                "testsrc=size=160x90:rate=25", "-t", "8", "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-g", "25", "-sc_threshold", "0",
                                "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_filename",
                                str(media / "segment-%03d.ts"), str(media / "index.m3u8")],
                               check=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                requested = []

                class Handler(http.server.SimpleHTTPRequestHandler):
                    def do_GET(self):
                        requested.append(self.path)
                        if self.path.endswith(".ts"):
                            time.sleep(.1)
                        super().do_GET()

                    def log_message(self, *_args):
                        pass

                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                         functools.partial(Handler, directory=str(media)))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                cancelled = threading.Event()
                folder = base / "download"
                folder.mkdir()
                target = folder / "episode.%(ext)s"
                url = f"http://127.0.0.1:{server.server_port}/index.m3u8"
                original_options = download_core.download_options

                def serial_options(*args):
                    options = original_options(*args)
                    options["concurrent_fragment_downloads"] = 1
                    return options

                def interrupt(data):
                    if data.get("fragment_index", 0) >= 2:
                        cancelled.set()

                download_core.download_options = serial_options
                try:
                    try:
                        download_core.download_with_progress(url, target, url, cancelled, interrupt)
                    except RuntimeError as error:
                        if "cancelled" not in str(error).lower():
                            raise
                    else:
                        raise RuntimeError("The test download did not cancel")
                    if not list(folder.rglob("*.ytdl")):
                        raise RuntimeError("No resume checkpoint was retained")
                    requested.clear()
                    cancelled.clear()
                    download_core.download_with_progress(url, target, url, cancelled, lambda _: None)
                    if "/segment-000.ts" in requested:
                        raise RuntimeError("Resume downloaded the first fragment again")
                    if not video_is_playable(folder / "episode.mp4", 8):
                        raise RuntimeError("The completed video failed FFprobe verification")
                finally:
                    download_core.download_options = original_options
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                return "Spawned worker, downloaded, cancelled, resumed, and verified an 8-second HLS video"

            check("frozen download workers and HLS resume", hls)
            report["resource_directory"] = str(resource_dir())
            report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if report_path:
            destination = Path(report_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
