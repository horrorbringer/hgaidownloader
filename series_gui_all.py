import html
import csv
import ctypes
from datetime import datetime
import errno
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from tkinter import font as tkfont
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
import yt_dlp
from file_lifecycle import delete_batch, file_size, file_identity, open_recycle_bin
from safe_paths import sanitize_filename, safe_child
from library_store import LibraryStore
from download_core import download_with_progress
from series_identity import series_folder
from library_recovery import open_library
from checkpoint_storage import inventory, selected_files, recycle_checkpoints
from existing_media import (episode_candidates, available_output_template, inspect_folder,
                            conflicting_claims, registered_episode_paths)
from series_identity import folder_owner

MEDIA_URL_PATTERN = re.compile(r"\.(m3u8|mp4|mpd)(\?.*)?$", re.IGNORECASE)
SETTINGS_FILE = Path(__file__).with_name("series_gui_settings.json")
MAX_EPISODE_ATTEMPTS = 3
RETRY_DELAYS = (2, 4)


def block_scan_media_request(request) -> bool:
    if request.resource_type == "document":
        return False
    return (request.resource_type == "media" or bool(MEDIA_URL_PATTERN.search(request.url))
            or urlparse(request.url).path.lower().endswith((".ts", ".m4s", ".aac")))


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(url: str, output_dir: str, recent_urls: list[str],
                  pending_urls: list[str], history: list[dict],
                  prefer_hevc: bool, marked_episodes: dict[str, list[int]] | None = None) -> None:
    data = {"url": url, "output_dir": output_dir,
            "recent_urls": recent_urls[:10], "pending_urls": pending_urls,
            "history": history[:100], "prefer_hevc": prefer_hevc,
            "marked_episodes": marked_episodes or {}}
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def translate_folder_name(title: str) -> str:
    if not re.search(r"[\u3400-\u9fff]", title):
        return sanitize_filename(title)
    query = urlencode({"q": title, "langpair": "zh-CN|en"})
    with urlopen(f"https://api.mymemory.translated.net/get?{query}", timeout=8) as response:
        data = json.load(response)
    translated = html.unescape(data.get("responseData", {}).get("translatedText", "")).strip()
    if str(data.get("responseStatus")) != "200" or not re.search(r"[A-Za-z]", translated) or re.search(r"[\u3400-\u9fff]", translated):
        raise ValueError("Translation service returned no English title")
    return sanitize_filename(translated)


def format_file_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def describe_ffmpeg_progress(data: dict[str, str], duration: float | None) -> tuple[float | None, str]:
    raw_time = data.get("out_time_us") or data.get("out_time_ms")
    try:
        seconds = max(0.0, int(raw_time) / 1_000_000) if raw_time else None
    except ValueError:
        seconds = None
    percent = (min(99.0, seconds / duration * 100)
               if seconds is not None and duration else None)
    details = [f"Current video: {percent:.0f}%" if percent is not None else
               "Current video: downloading"]
    try:
        size = int(data.get("total_size") or 0)
    except ValueError:
        size = 0
    if size > 0:
        details.append(format_file_size(size))
    speed_text = (data.get("speed") or "").strip()
    speed_match = re.fullmatch(r"(\d+(?:\.\d+)?)x", speed_text)
    if speed_match:
        speed = float(speed_match.group(1))
        if speed > 0:
            details.append(f"{speed:g}×")
            if seconds is not None and duration and seconds < duration:
                remaining = int((duration - seconds) / speed)
                eta = f"{remaining // 60}m {remaining % 60}s" if remaining >= 60 else f"{remaining}s"
                details.append(f"ETA ~{eta}")
    return percent, "   ·   ".join(details)


def video_is_playable(path: Path, expected_duration: float = 0) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15, check=True,
        )
        info = json.loads(result.stdout)
        duration = float(info.get("format", {}).get("duration") or 0)
        minimum = max(1, expected_duration - max(2, expected_duration * .02)) if expected_duration else 1
        return duration >= minimum and bool(info.get("streams"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def find_existing_episode(folder: Path, number: int, label: str,
                          expected_duration: float = 0, registered_paths=()) -> Path | None:
    for path in episode_candidates(folder, number, label, registered_paths):
        if video_is_playable(path, expected_duration):
            return path
    return None


def verify_episode_output(template: Path, expected_duration: float = 0) -> Path:
    stem = template.name.removesuffix(".%(ext)s")
    candidates = []
    for extension in (".mp4", ".mkv", ".webm"):
        path = template.with_name(stem + extension)
        if path.is_file():
            candidates.append(path)
    for path in candidates:
        if video_is_playable(path, expected_duration):
            return path
    for path in candidates:
        backup = path.with_name(f"{path.stem}.invalid-{time.time_ns()}{path.suffix}")
        path.replace(backup)
    raise RuntimeError("Downloaded video is missing or failed playback verification")


def parse_episode_selection(value: str) -> set[int] | None:
    if not value.strip():
        return None
    selected: set[int] = set()
    for part in value.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise ValueError("Use episode numbers like 1,3-5.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end - start > 1000:
            raise ValueError("Episode range is invalid or too large.")
        selected.update(range(start, end + 1))
    return selected


def should_retry_download(exc: Exception) -> bool:
    if isinstance(exc, OSError) and exc.errno in {
        errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EROFS, errno.ENOENT, errno.EEXIST,
    }:
        return False
    message = str(exc).casefold()
    return not any(reason in message for reason in (
        "http error 401", "http error 403", "http error 404",
        "permission denied", "no space left on device", "unsupported url",
    ))


def should_retry_page_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in (
        "timeout", "timed out", "net::err_", "connection reset", "connection closed",
    ))


def choose_episode_stream(page_source: dict | None, captured: list[str],
                          seen: set[str], prefer_hevc: bool) -> tuple[str | None, str | None, str | None]:
    if page_source:
        main = page_source.get("url")
        hevc = page_source.get("hevc_url")
        candidates = ((hevc, "HEVC"), (main, "H.264")) if prefer_hevc else (
            (main, "H.264"), (hevc, "HEVC"))
        for stream, codec in candidates:
            if stream and stream not in seen:
                fallback = (main if prefer_hevc and codec == "HEVC"
                            and main != stream and main not in seen else None)
                return stream, fallback, codec
        if main or hevc:
            return None, None, None
    available = sorted((stream for stream in captured if stream not in seen),
                       key=lambda stream: (0 if ".m3u8" in stream.lower() else 1))
    return (available[0], None, None) if available else (None, None, None)


def stop_process_on_cancel(process: subprocess.Popen, cancel_event: threading.Event,
                           finished: threading.Event) -> None:
    while not finished.is_set():
        if cancel_event.wait(0.1):
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass  # The process may have exited between poll and terminate.
            return


class RoundedCard(tk.Canvas):
    def __init__(self, parent, *, background: str, surface: str = "#ffffff",
                 border: str = "#dce5f0", height: int = 180,
                 padding_x: int = 12, padding_y: int = 9,
                 content_style: str = "PanelInner.TFrame"):
        super().__init__(parent, background=background, height=height,
                         highlightthickness=0, borderwidth=0)
        self.surface = surface
        self.border = border
        self.inset_x = padding_x
        self.inset_y = padding_y
        self.content = ttk.Frame(self, style=content_style)
        self.content_window = self.create_window(padding_x, padding_y, anchor=tk.NW,
                                                 window=self.content)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2 or height < 2:
            return
        radius = min(14, width / 2, height / 2)
        points = [radius, 1, width - radius, 1, width - 1, 1,
                  width - 1, radius, width - 1, height - radius,
                  width - 1, height - 1, width - radius, height - 1,
                  radius, height - 1, 1, height - 1, 1, height - radius,
                  1, radius, 1, 1]
        self.delete("surface")
        self.create_polygon(points, smooth=True, splinesteps=18,
                            fill=self.surface, outline=self.border, width=1,
                            tags="surface")
        self.tag_lower("surface")
        self.itemconfigure(self.content_window,
                           width=max(1, width - 2 * self.inset_x),
                           height=max(1, height - 2 * self.inset_y))


class RoundedProgressbar(tk.Canvas):
    def __init__(self, parent, *, track: str, fill: str, background: str):
        super().__init__(parent, height=8, background=background,
                         highlightthickness=0, borderwidth=0)
        self.track_color = track
        self.fill_color = fill
        self.mode = "determinate"
        self.maximum = 100.0
        self.value = 0.0
        self.pulse_job = None
        self.pulse_position = 0.0
        self.bind("<Configure>", self._draw)

    def __getitem__(self, key):
        if key in ("value", "maximum", "mode"):
            return getattr(self, key)
        return super().__getitem__(key)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        for key in ("mode", "maximum", "value"):
            if key in kwargs:
                value = kwargs.pop(key)
                setattr(self, key, value if key == "mode" else float(value))
        if kwargs:
            super().configure(**kwargs)
        self._draw()

    def _pill(self, left, right, color):
        height = self.winfo_height()
        width = right - left
        if width <= 0:
            return
        if width < height:
            self.create_oval(left, 0, right, height, fill=color, outline="")
        else:
            self.create_oval(left, 0, left + height, height, fill=color, outline="")
            self.create_oval(right - height, 0, right, height, fill=color, outline="")
            self.create_rectangle(left + height / 2, 0, right - height / 2,
                                  height, fill=color, outline="")

    def _draw(self, _event=None):
        width = self.winfo_width()
        if width < 2:
            return
        self.delete("all")
        self._pill(0, width, self.track_color)
        if self.mode == "indeterminate":
            segment = min(width, max(36, width * 0.24))
            left = (width + segment) * self.pulse_position - segment
            self._pill(max(0, left), min(width, left + segment), self.fill_color)
        else:
            self._pill(0, width * min(1.0, max(0.0, self.value / self.maximum)),
                       self.fill_color)

    def start(self, interval=16):
        if self.pulse_job is not None:
            return

        def pulse():
            self.pulse_position = (self.pulse_position + 0.018) % 1.0
            self._draw()
            self.pulse_job = self.after(interval, pulse)

        pulse()

    def stop(self):
        if self.pulse_job is not None:
            self.after_cancel(self.pulse_job)
            self.pulse_job = None


class RoundedActionButton(tk.Canvas):
    def __init__(self, parent, *, text: str, image: tk.PhotoImage, command,
                 background: str):
        super().__init__(parent, width=160, height=32, background=background,
                         highlightthickness=0, borderwidth=0, takefocus=True,
                         cursor="hand2")
        self.label = text
        self.icon = image
        self.command = command
        self.button_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.button_state = "normal"
        self.hovered = False
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", self._draw)
        self.bind("<FocusOut>", self._draw)

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if "state" in kwargs:
            self.button_state = kwargs.pop("state")
        if "text" in kwargs:
            self.label = kwargs.pop("text")
        if kwargs:
            super().configure(**kwargs)
        self._draw()

    def _enter(self, _event):
        self.hovered = True
        self._draw()

    def _leave(self, _event):
        self.hovered = False
        self._draw()

    def _activate(self, _event):
        if self.button_state == "normal":
            self.focus_set()
            self.command()

    def _draw(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2 or height < 2:
            return
        fill = ("#aebfdd" if self.button_state != "normal" else
                "#1c4fbe" if self.hovered else "#285ed8")
        radius = 10
        points = [radius, 0, width - radius, 0, width, 0,
                  width, radius, width, height - radius,
                  width, height, width - radius, height,
                  radius, height, 0, height, 0, height - radius,
                  0, radius, 0, 0]
        self.delete("all")
        self.create_polygon(points, smooth=True, splinesteps=18,
                            fill=fill, outline="")
        if self.focus_get() is self:
            focus_points = [10, 3, width - 10, 3, width - 3, 3,
                            width - 3, 10, width - 3, height - 10,
                            width - 3, height - 3, width - 10, height - 3,
                            10, height - 3, 3, height - 3,
                            3, height - 10, 3, 10, 3, 3]
            self.create_polygon(focus_points, smooth=True, splinesteps=18,
                                fill="", outline="#ffffff", dash=(2, 3))
        icon_width = self.icon.width()
        content_width = icon_width + 7 + self.button_font.measure(self.label)
        left = (width - content_width) / 2
        self.create_image(left, height / 2, image=self.icon, anchor=tk.W)
        self.create_text(left + icon_width + 7, height / 2, text=self.label,
                         anchor=tk.W, fill="#ffffff", font=self.button_font)


class SeriesDownloaderApp:
    def __init__(self, root: tk.Tk, library_path: Path | None = None):
        self.root = root
        self.root.title("Series Downloader")
        self.root.geometry("960x640")
        self.root.minsize(800, 540)
        self.root.update_idletasks()
        try:
            dark = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(), 20, ctypes.byref(dark), ctypes.sizeof(dark)
            )
            rounded = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(), 33, ctypes.byref(rounded), ctypes.sizeof(rounded)
            )
            mica = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(), 38, ctypes.byref(mica), ctypes.sizeof(mica)
            )
        except (AttributeError, OSError):
            pass
        style = ttk.Style(root)
        style.theme_use("clam")
        bg, panel, input_bg = "#f3f6fb", "#ffffff", "#ffffff"
        text, muted, accent = "#1d2d45", "#66758a", "#285ed8"
        self.root.configure(background=bg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=bg, foreground=muted,
                        font=("Segoe UI", 8, "bold"))
        style.configure("PanelInner.TFrame", background=panel)
        style.configure("StatusInner.TFrame", background="#edf4ff")
        style.configure("Panel.TLabel", background=panel, foreground=text, font=("Segoe UI", 9))
        style.configure("PanelTitle.TLabel", background=panel, foreground=text,
                        font=("Segoe UI", 11, "bold"))
        style.configure("PanelMuted.TLabel", background=panel, foreground=muted,
                        font=("Segoe UI", 8))
        style.configure("StatusTitle.TLabel", background="#edf4ff", foreground=text,
                        font=("Segoe UI", 10, "bold"))
        style.configure("StatusMuted.TLabel", background="#edf4ff", foreground=muted,
                        font=("Segoe UI", 8))
        style.configure("Empty.TLabel", background=panel, foreground=muted,
                        font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=bg, foreground=text, font=("Segoe UI", 17, "bold"))
        style.configure("Status.TLabel", background=bg, foreground=text, font=("Segoe UI", 9, "bold"))
        style.configure("TButton", background=panel, foreground=text, bordercolor="#cfdae9",
                        lightcolor=panel, darkcolor=panel, padding=(9, 3), font=("Segoe UI", 8))
        style.map("TButton", background=[("active", "#e9f0fb"), ("disabled", "#eef2f7")],
                  foreground=[("disabled", "#9a9da2")])
        style.configure("TEntry", fieldbackground=input_bg, foreground=text, insertcolor=text,
                        bordercolor="#cbd7e8", padding=(6, 3), font=("Segoe UI", 9))
        style.configure("TCombobox", fieldbackground=input_bg, background=input_bg, foreground=text,
                        arrowcolor=text, bordercolor="#cbd7e8", padding=(6, 3), font=("Segoe UI", 9))
        style.map("TCombobox", fieldbackground=[("readonly", input_bg)], foreground=[("readonly", text)])
        style.configure("TCheckbutton", background=bg, foreground=text, font=("Segoe UI", 8))
        style.map("TCheckbutton", background=[("active", bg)])
        style.configure("Episodes.Treeview", background=panel, fieldbackground=panel, foreground=text,
                        bordercolor="#e1e8f2", rowheight=27, font=("Segoe UI", 9))
        style.map("Episodes.Treeview", background=[("selected", "#dce9ff")],
                  foreground=[("selected", text)])
        style.configure("Episodes.Treeview.Heading", background="#f4f7fc", foreground=text,
                        relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Episodes.Treeview.Heading", background=[("active", "#e7eefb")])
        style.configure("TScrollbar", background=panel, troughcolor=bg, arrowcolor=muted)
        icon_dir = Path(__file__).with_name("icons")
        self.icons = {name: tk.PhotoImage(file=str(icon_dir / f"{name}.png"))
                      for name in ("download", "folder", "history", "settings")}
        self.log_queue: queue.Queue = queue.Queue()
        self.worker_context = threading.local()
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pending_urls: list[str] = []
        self.failed_episodes: set[int] = set()
        self.saved_files: dict[int, Path] = {}
        self.stream_urls: dict[int, str] = {}
        self.episode_priorities: dict[int, str] = {}
        self.delete_running = False
        self.running = False
        self.scanning = False
        self.sort_column: str | None = None
        self.sort_reverse = False
        self.episode_order: list[str] = []
        self._displayed_selection: set[str] = set()
        self.library = open_library(root, library_path or Path(__file__).with_name("series_library.sqlite3"))
        self.library_dirty = False
        self.library_can_save = True
        settings = load_settings()
        stored_marks = settings.get("marked_episodes", {})
        self.marked_episodes = {
            url: [n for n in numbers if isinstance(n, int) and n > 0]
            for url, numbers in (stored_marks.items() if isinstance(stored_marks, dict) else [])
            if isinstance(url, str) and isinstance(numbers, list)
        }
        recent = settings.get("recent_urls")
        self.recent_urls = [url for url in (recent if isinstance(recent, list) else [])
                            if isinstance(url, str) and urlparse(url).scheme in ("http", "https")][:10]
        pending = settings.get("pending_urls")
        self.pending_urls = [url for url in (pending if isinstance(pending, list) else [])
                             if isinstance(url, str) and urlparse(url).scheme in ("http", "https")
                             and bool(urlparse(url).netloc)]
        saved_history = settings.get("history")
        self.history = [entry for entry in (saved_history if isinstance(saved_history, list) else [])
                        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
                        and isinstance(entry.get("folder"), str)][:100]

        # URL Input
        top_frame = ttk.Frame(root, padding=(16, 9, 16, 3))
        top_frame.pack(fill=tk.X)
        brand_row = ttk.Frame(top_frame)
        brand_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(brand_row, text="Series Downloader", style="Title.TLabel").pack(side=tk.LEFT)
        self.system_status_var = tk.StringVar(value="Checking storage and FFmpeg...")
        ttk.Label(brand_row, textvariable=self.system_status_var,
                  style="Section.TLabel").pack(side=tk.RIGHT)
        ttk.Label(top_frame, text="Series URL", style="Section.TLabel").pack(anchor=tk.W)
        self.url_var = tk.StringVar(value=settings.get("url") or "https://huangguoai.com/video/10007")
        url_row = ttk.Frame(top_frame)
        url_row.pack(fill=tk.X, pady=(3, 0))
        self.url_entry = ttk.Combobox(url_row, textvariable=self.url_var,
                                      values=self.recent_urls, state="normal")
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.url_entry.bind("<Return>", lambda _event: self.scan_episodes())
        self.scan_btn = ttk.Button(url_row, text="Scan Episodes", command=self.scan_episodes)
        self.scan_btn.pack(side=tk.LEFT, padx=(7, 0))

        self.settings_toggle = ttk.Button(top_frame, text="Options",
                                          image=self.icons["settings"], compound=tk.LEFT,
                                          command=self.toggle_settings)
        self.settings_toggle.pack(anchor=tk.W, pady=(4, 0))
        self.settings_visible = True
        self.settings_frame = ttk.Frame(root)
        self.settings_frame.pack(fill=tk.X)

        queue_frame = ttk.Frame(self.settings_frame, padding=(16, 3, 16, 2))
        queue_frame.pack(fill=tk.X)
        ttk.Label(queue_frame, text="Series queue", style="Section.TLabel").pack(anchor=tk.W, pady=(0, 2))
        self.queue_list = ttk.Treeview(queue_frame, columns=("url", "status"),
                                       show="headings", height=2, style="Episodes.Treeview")
        self.queue_list.heading("url", text="Series URL", anchor=tk.W)
        self.queue_list.heading("status", text="Status", anchor=tk.W)
        self.queue_list.column("url", width=400, stretch=True)
        self.queue_list.column("status", width=110, stretch=False)
        self.queue_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for queued_url in self.pending_urls:
            self.queue_list.insert("", tk.END, iid=queued_url, values=(queued_url, "Waiting"))
        self.remove_queue_btn = ttk.Button(queue_frame, text="Remove", command=self.remove_from_queue)
        self.remove_queue_btn.pack(side=tk.LEFT, padx=(7, 0))
        queue_actions = ttk.Frame(self.settings_frame, padding=(16, 2, 16, 3))
        queue_actions.pack(fill=tk.X)
        self.add_queue_btn = ttk.Button(queue_actions, text="Add current URL", command=self.add_to_queue)
        self.add_queue_btn.pack(side=tk.LEFT)
        ttk.Button(queue_actions, text="Add multiple URLs...", command=self.add_multiple_urls).pack(side=tk.LEFT, padx=(7, 0))
        self.run_queue_btn = ttk.Button(queue_actions, text="Run Queue", command=lambda: self.start_download(use_queue=True))
        self.run_queue_btn.pack(side=tk.LEFT, padx=(7, 0))

        # Output Folder Input
        dir_frame = ttk.Frame(self.settings_frame, padding=(16, 3, 16, 2))
        dir_frame.pack(fill=tk.X)
        ttk.Label(dir_frame, text="Save directory", style="Section.TLabel").pack(anchor=tk.W)
        dir_row = ttk.Frame(dir_frame)
        dir_row.pack(fill=tk.X, pady=(3, 0))
        self.output_var = tk.StringVar(value=settings.get("output_dir") or str(Path("./downloads").resolve()))
        self.output_var.trace_add("write", lambda *_: self.refresh_system_status())
        ttk.Entry(dir_row, textvariable=self.output_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dir_row, text="Browse...", command=self.choose_dir).pack(side=tk.LEFT, padx=(7, 0))

        selection_row = ttk.Frame(self.settings_frame, padding=(16, 3, 16, 2))
        selection_row.pack(fill=tk.X)
        ttk.Label(selection_row, text="Episodes (blank = all):").pack(side=tk.LEFT)
        self.episode_range_var = tk.StringVar()
        ttk.Entry(selection_row, textvariable=self.episode_range_var, width=18).pack(side=tk.LEFT, padx=(7, 6))
        ttk.Label(selection_row, text="Example: 1,3-5").pack(side=tk.LEFT)

        ttk.Separator(self.settings_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=16, pady=(4, 2))

        # Options and actions stay on separate rows at narrower window sizes.
        opt_frame = ttk.Frame(self.settings_frame, padding=(16, 3, 16, 4))
        opt_frame.pack(fill=tk.X)
        self.headful_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame, text="Show Browser Window (for CAPTCHA / manual login)", variable=self.headful_var
        ).pack(side=tk.LEFT)
        self.translate_folder_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="English folder names", variable=self.translate_folder_var).pack(side=tk.LEFT, padx=(12, 0))
        self.prefer_hevc_var = tk.BooleanVar(value=settings.get("prefer_hevc") is True)
        ttk.Checkbutton(opt_frame, text="Prefer HEVC for new episodes",
                        variable=self.prefer_hevc_var,
                        command=self.remember_settings).pack(side=tk.LEFT, padx=(12, 0))

        self.toggle_settings()

        actions = ttk.Frame(root, padding=(16, 3, 16, 4))
        actions.pack(fill=tk.X)
        self.actions_frame = actions
        self.start_btn = RoundedActionButton(
            actions, text="Start Download", image=self.icons["download"],
            command=self.start_download, background=bg,
        )
        self.start_btn.pack(side=tk.LEFT)
        self.refresh_settings_caption()
        self.pause_btn = ttk.Button(actions, text="Pause after episode", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side=tk.LEFT, padx=(7, 0))
        self.cancel_btn = ttk.Button(actions, text="Cancel", command=self.cancel_download, state="disabled")
        self.cancel_btn.pack(side=tk.LEFT, padx=(7, 0))
        self.retry_btn = ttk.Button(actions, text="Retry Failed", command=self.retry_failed, state="disabled")
        self.retry_btn.pack(side=tk.RIGHT)
        ttk.Button(actions, text="Open Folder", image=self.icons["folder"], compound=tk.LEFT,
                   command=self.open_folder).pack(side=tk.RIGHT, padx=(0, 7))
        ttk.Button(actions, text="History", image=self.icons["history"], compound=tk.LEFT,
                   command=self.show_history).pack(side=tk.RIGHT, padx=(0, 7))

        self.feedback_card = RoundedCard(root, background=bg, surface="#edf4ff",
                                         border="#d5e4fb", height=68,
                                         content_style="StatusInner.TFrame")
        self.feedback_card.pack(fill=tk.X, padx=16, pady=(2, 2))
        feedback = self.feedback_card.content
        status_row = ttk.Frame(feedback, style="StatusInner.TFrame")
        status_row.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Ready to scan episodes")
        ttk.Label(status_row, textvariable=self.status_var, style="StatusTitle.TLabel",
                  wraplength=540).pack(side=tk.LEFT)
        self.count_var = tk.StringVar(value="Episodes: 0 / 0")
        ttk.Label(status_row, textvariable=self.count_var,
                  style="StatusMuted.TLabel").pack(side=tk.RIGHT)
        self.progress = RoundedProgressbar(feedback, track="#d6e3f6", fill=accent,
                                           background="#edf4ff")
        self.progress.pack(fill=tk.X, pady=(5, 0))
        self.progress_mode = "determinate"
        self.progress_target = 0.0
        self.progress_animation = None
        self.detail_var = tk.StringVar(value="")
        self.detail_label = ttk.Label(feedback, textvariable=self.detail_var,
                                      style="StatusMuted.TLabel")

        episode_card = RoundedCard(root, background=bg, height=250)
        episode_card.pack(fill=tk.BOTH, expand=True, padx=16, pady=2)
        episode_frame = episode_card.content
        episode_heading = ttk.Frame(episode_frame, style="PanelInner.TFrame")
        episode_heading.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(episode_heading, text="Episodes", style="PanelTitle.TLabel").pack(side=tk.LEFT)
        ttk.Button(episode_heading, text="Library", command=self.show_library).pack(side=tk.LEFT, padx=10)
        ttk.Button(episode_heading, text="Partial downloads", command=self.show_checkpoints).pack(side=tk.LEFT)
        self.series_name_var = tk.StringVar(value="No series loaded")
        ttk.Label(episode_heading, textvariable=self.series_name_var,
                  style="PanelMuted.TLabel", wraplength=440,
                  justify=tk.RIGHT).pack(side=tk.RIGHT)
        filter_row = ttk.Frame(episode_frame, style="PanelInner.TFrame")
        filter_row.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(filter_row, text="Find", style="Panel.TLabel").pack(side=tk.LEFT)
        self.episode_search = tk.StringVar()
        search_entry = ttk.Entry(filter_row, textvariable=self.episode_search, width=14)
        search_entry.pack(side=tk.LEFT, padx=(6, 12))
        self.episode_search.trace_add("write", lambda *_: self.filter_episodes())
        ttk.Label(filter_row, text="Status", style="Panel.TLabel").pack(side=tk.LEFT)
        self.episode_filter = tk.StringVar(value="All")
        status_filter = ttk.Combobox(filter_row, textvariable=self.episode_filter, state="readonly", width=13,
                                     values=("All", "Ready", "Waiting", "Downloading", "Done", "Failed", "Skipped"))
        status_filter.pack(side=tk.LEFT, padx=(6, 0))
        status_filter.bind("<<ComboboxSelected>>", lambda _event: self.filter_episodes())
        ttk.Button(filter_row, text="Existing videos...", command=self.link_existing_videos).pack(side=tk.RIGHT)
        ttk.Button(filter_row, text="Select Pending", command=self.select_pending).pack(side=tk.RIGHT, padx=6)
        self.batch_bar = ttk.Frame(episode_frame, style="PanelInner.TFrame")
        self.batch_bar.pack(fill=tk.X, pady=(0, 5))
        self.selection_caption = ttk.Label(self.batch_bar, text="0 selected", style="Panel.TLabel")
        self.selection_caption.pack(side=tk.LEFT)
        ttk.Button(self.batch_bar, text="Download Selected", command=self.download_selected).pack(side=tk.LEFT, padx=(10, 4))
        ttk.Button(self.batch_bar, text="Mark Downloaded", command=self.mark_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(self.batch_bar, text="Copy Links", command=self.copy_selected_links).pack(side=tk.LEFT, padx=4)
        ttk.Button(self.batch_bar, text="Delete Files", command=self.confirm_delete_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(self.batch_bar, text="Deselect All", command=self.deselect_episodes).pack(side=tk.RIGHT)
        self.batch_buttons = [child for child in self.batch_bar.winfo_children()
                              if isinstance(child, ttk.Button)]
        for button in self.batch_buttons:
            button.configure(state="disabled")
        table_frame = ttk.Frame(episode_frame, style="PanelInner.TFrame")
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.episode_list = ttk.Treeview(table_frame, columns=("select", "episode", "state", "percent", "size", "file"), show="headings", height=5, style="Episodes.Treeview", selectmode="extended")
        for key, label, width, stretch in (
            ("select", "☐", 36, False),
            ("episode", "Episode", 78, False),
            ("state", "Status", 155, False),
            ("percent", "Progress", 78, False),
            ("size", "Size", 72, False),
            ("file", "Saved file", 230, True),
        ):
            self.episode_list.heading(key, text=label, anchor=tk.W,
                                      command=(self.toggle_select_all if key == "select" else
                                               lambda column=key: self.sort_episodes(column)))
            self.episode_list.column(key, width=width, minwidth=width, stretch=stretch, anchor=tk.W)
        self.episode_list.tag_configure("odd", background="#f8f9fb")
        self.episode_list.tag_configure("even", background="#ffffff")
        self.episode_list.tag_configure("done", foreground="#267344")
        self.episode_list.tag_configure("failed", foreground="#b42332")
        self.episode_list.tag_configure("active", foreground="#005ca9")
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.episode_list.yview)
        self.episode_list.configure(yscrollcommand=scrollbar.set)
        self.episode_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.empty_table_label = ttk.Label(
            table_frame, text="Scan a series URL to load episodes here.",
            style="Empty.TLabel", justify=tk.CENTER,
        )
        self.empty_table_label.place(relx=0.5, rely=0.52, anchor=tk.CENTER)
        self.episode_list.bind("<Double-1>", self.open_episode)
        self.episode_list.bind("<Button-3>", self.show_episode_menu)
        self.episode_list.bind("<<TreeviewSelect>>", self.update_episode_selection)
        self.episode_list.bind("<Button-1>", self.on_episode_click, add="+")
        self.episode_list.bind("<space>", self.toggle_focused_episode)
        self.root.bind("<Escape>", self.deselect_episodes)
        self.root.bind("<Shift-Delete>", self.confirm_delete_selected)
        self.root.bind("<Control-e>", lambda _event: self.export_selected("json"))
        self.episode_menu = tk.Menu(self.root, tearoff=False, background="#ffffff",
                                    foreground=text, activebackground="#dce9ff",
                                    activeforeground=text)

        # Log Console
        log_card = RoundedCard(root, background=bg, height=160)
        log_card.pack(fill=tk.BOTH, expand=True, padx=16, pady=(2, 10))
        self.log_frame = log_card
        log_frame = log_card.content
        log_header = ttk.Frame(log_frame, style="PanelInner.TFrame")
        log_header.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(log_header, text="Activity", style="PanelTitle.TLabel").pack(side=tk.LEFT)
        self.activity_visible = True
        self.activity_toggle = ttk.Button(log_header, text="Show details", command=self.toggle_activity)
        self.activity_toggle.pack(side=tk.RIGHT)
        self.log_box = scrolledtext.ScrolledText(
            log_frame, state="disabled", height=4, font=("Consolas", 9),
            background="#ffffff", foreground=text, insertbackground=text,
            selectbackground="#dce9ff", borderwidth=0, relief="flat",
            highlightthickness=1, highlightbackground="#e1e8f2", padx=9, pady=5,
        )
        self.log_box.pack(fill=tk.BOTH, expand=True)
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, "Paste a series URL and click Scan Episodes. Review the list, then Download Selected.\n")
        self.log_box.configure(state="disabled")
        self.log_actions = ttk.Frame(log_frame, style="PanelInner.TFrame")
        self.log_actions.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(self.log_actions, text="Copy Log", command=self.copy_log).pack(side=tk.RIGHT)
        ttk.Button(self.log_actions, text="Clear Log", command=self.clear_log).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(self.log_actions, text="Export Summary", command=self.export_summary).pack(side=tk.LEFT)
        ttk.Button(self.log_actions, text="Clear Recent", command=self.clear_recent).pack(side=tk.LEFT, padx=(6, 0))

        self.toggle_activity()

        self.root.after_idle(self.update_feedback_height)
        self.root.after(100, self.poll_logs)
        threading.Thread(target=self.detect_ffmpeg_version, daemon=True).start()
        stored = self.library.all()
        if stored:
            self.load_library_series(stored[0]["url"])
        if hasattr(self.library, "temporary_directory"):
            self.status_var.set("Temporary library session — changes will not survive restart")
        elif hasattr(self.library, "recovery_backup"):
            self.log(f"Library backup saved to: {self.library.recovery_backup}")
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    def save_current_library(self):
        if not self.library_can_save:
            return
        url = getattr(self, "current_series_url", "")
        if not url:
            return
        records = []
        for item in self.episode_order:
            number = int(item.removeprefix("ep-"))
            records.append(dict(number=number, title=self.episode_list.set(item, "episode"),
                                state=self.episode_list.set(item, "state"),
                                percent=self.episode_list.set(item, "percent"),
                                file=str(self.saved_files.get(number, "")),
                                priority=self.episode_priorities.get(number, "Normal")))
        try:
            self.library.save(url, self.series_name_var.get(),
                              getattr(self, "current_output_root", self.output_var.get()),
                              getattr(self, "current_series_folder", ""), records)
            self.library_dirty = False
        except (OSError, sqlite3.Error) as exc:
            self.log(f"[!] Could not save the library: {exc}")

    def load_library_series(self, url: str, *, save_previous: bool = True):
        if self.running or self.delete_running:
            return
        if save_previous:
            self.save_current_library()
        data = self.library.get(url)
        if not data:
            return
        self.library_can_save = True
        for item in self.episode_order:
            self.episode_list.delete(item)
        self.episode_order = []
        self._displayed_selection.clear()
        self.saved_files.clear()
        self.stream_urls.clear()
        self.failed_episodes.clear()
        self.episode_priorities.clear()
        self.current_series_url = url
        self.current_output_root = data["output_root"]
        self.current_series_folder = data["folder"]
        self.url_var.set(url)
        self.output_var.set(data["output_root"])
        self.series_name_var.set(data["title"])
        for record in data["episodes"]:
            number = record["number"]
            item = f"ep-{number}"
            state = record["state"]
            if state.startswith(("Downloading", "Finding", "Verifying", "Retrying")):
                state = "Interrupted"
            path = Path(record["file"]) if record["file"] else None
            if path and path.is_file():
                self.saved_files[number] = path
            elif path:
                state = "Missing file"
            size = format_file_size(path.stat().st_size) if number in self.saved_files else "—"
            self.episode_list.insert("", tk.END, iid=item, values=("☐", record["title"], state,
                                     record["percent"], size, str(path) if path else "—"))
            self.episode_order.append(item)
            self.episode_priorities[number] = record["priority"]
            if state.startswith(("Failed", "Skipped", "Interrupted")):
                self.failed_episodes.add(number)
        self.retry_btn.configure(state="normal" if self.failed_episodes else "disabled")
        self.filter_episodes()
        self.update_episode_selection()

    def show_library(self):
        if self.running or self.delete_running:
            return
        window = tk.Toplevel(self.root)
        window.title("Saved library")
        window.geometry("700x350")
        rows = ttk.Treeview(window, columns=("title", "url"), show="headings")
        rows.heading("title", text="Series")
        rows.heading("url", text="Source URL")
        rows.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        for record in self.library.all():
            rows.insert("", tk.END, iid=record["url"], values=(record["title"], record["url"]))

        def load(_event=None):
            if rows.selection():
                self.load_library_series(rows.selection()[0])
                window.destroy()

        rows.bind("<Double-1>", load)
        ttk.Button(window, text="Open series", command=load).pack(pady=(0, 12))

    def link_existing_videos(self):
        if self.running or self.delete_running:
            return
        if not shutil.which("ffprobe"):
            messagebox.showerror("Video verification unavailable", "Install FFprobe on PATH before linking existing videos.")
            return
        folder = filedialog.askdirectory(title="Choose the folder containing this series' existing videos",
                                         initialdir=self.output_var.get())
        if not folder:
            return
        directory = Path(folder)
        window = tk.Toplevel(self.root)
        window.title("Existing videos")
        window.geometry("800x450")
        window.transient(self.root)
        window.grab_set()
        body = ttk.Frame(window, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Link selected videos to this series URL:").pack(anchor=tk.W)
        source = tk.StringVar(value=getattr(self, "current_series_url", self.url_var.get().strip()))
        ttk.Entry(body, textvariable=source).pack(fill=tk.X, pady=(4, 8))
        ttk.Label(body, text="Files stay in their current folder. Select only episodes belonging to this series.",
                  wraplength=750).pack(anchor=tk.W, pady=(0, 8))
        rows = ttk.Treeview(body, columns=("episode", "file", "status", "size"), show="headings", selectmode="extended")
        for key, label, width in (("episode", "Episode", 70), ("file", "Existing video", 330),
                                  ("status", "Check", 190), ("size", "Size", 90)):
            rows.heading(key, text=label)
            rows.column(key, width=width)
        rows.pack(fill=tk.BOTH, expand=True)
        status = tk.StringVar(value="Verifying local videos...")
        ttk.Label(body, textvariable=status, wraplength=750).pack(anchor=tk.W, pady=8)
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)
        cancelled = threading.Event()
        events = queue.Queue()
        scanned = []
        poll_job = None

        def close():
            cancelled.set()
            if poll_job:
                window.after_cancel(poll_job)
            window.destroy()

        def link():
            url = source.get().strip()
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                status.set("Enter a valid HTTP or HTTPS series URL.")
                return
            chosen = [scanned[int(item)] for item in rows.selection()]
            if not chosen or any(not row.verified or row.number is None for row in chosen):
                status.set("Select videos that passed verification and have an episode number.")
                return
            if len({row.number for row in chosen}) != len(chosen):
                status.set("Select only one video for each episode number.")
                return
            try:
                conflicts = conflicting_claims(self.library, url)
                owner = folder_owner(directory)
                if owner not in (None, url) or any(row.path in conflicts for row in chosen):
                    raise ValueError("A selected file or its folder is assigned to another series.")
                if any(file_identity(row.path) != row.identity for row in chosen):
                    raise ValueError("A selected file changed after verification. Close this dialog and scan again.")
                self.save_current_library()
                previous = self.library.get(url)
                priorities = {r['number']: r['priority'] for r in previous['episodes']} if previous else {}
                records = [dict(number=row.number, title=f"Episode {row.number}", state="Already downloaded",
                                percent="100%", file=str(row.path),
                                priority=priorities.get(row.number, "Normal")) for row in chosen]
                root = previous['output_root'] if previous else str(Path(self.output_var.get()).expanduser().resolve())
                self.library.save(url, previous['title'] if previous else directory.name, root,
                                  previous['folder'] if previous else '', records)
                self.load_library_series(url, save_previous=False)
                self.status_var.set(f"Linked {len(records)} verified existing video(s)")
                close()
            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                status.set(str(exc))

        link_button = ttk.Button(buttons, text="Link selected videos", command=link, state="disabled")
        link_button.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=close).pack(side=tk.RIGHT, padx=8)
        window.protocol("WM_DELETE_WINDOW", close)
        window.bind("<Escape>", lambda _event: close())

        def scan():
            try:
                events.put(inspect_folder(directory, video_is_playable, cancelled))
            except Exception as exc:
                events.put(str(exc))

        def poll():
            nonlocal poll_job
            if events.empty():
                poll_job = window.after(80, poll)
                return
            poll_job = None
            result = events.get_nowait()
            if isinstance(result, str):
                status.set(result)
                return
            scanned.extend(result)
            counts = {}
            for row in scanned:
                if row.verified:
                    counts[row.number] = counts.get(row.number, 0) + 1
            for index, row in enumerate(scanned):
                check = "Choose one rendition" if row.verified and counts[row.number] > 1 else row.status
                rows.insert("", tk.END, iid=str(index), values=(row.number or "—", row.path.name, check,
                                                              format_file_size(row.size)))
                if row.verified and counts[row.number] == 1:
                    rows.selection_add(str(index))
            link_button.configure(state="normal")
            status.set(f"{len(scanned)} video(s) found · {sum(row.verified for row in scanned)} verified. "
                       "Names such as 01_, Episode 1, S01E01, or 第1集 identify the episode.")

        threading.Thread(target=scan, daemon=True).start()
        poll()

    def show_checkpoints(self):
        if self.running or self.delete_running:
            return
        folder = getattr(self, "current_series_folder", "")
        if not folder:
            messagebox.showinfo("Partial downloads", "Open a saved series first.")
            return
        series = Path(folder)
        window = tk.Toplevel(self.root)
        window.title("Partial downloads")
        window.geometry("720x370")
        window.transient(self.root)
        window.grab_set()
        body = ttk.Frame(window, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Select checkpoints to recycle. Removing them restarts those downloads.",
                  wraplength=670).pack(anchor=tk.W, pady=(0, 8))
        rows = ttk.Treeview(body, columns=("file", "size", "date"), show="headings", selectmode="extended")
        for key, label, width in (("file", "Partial file", 340), ("size", "Size", 90), ("date", "Last modified", 150)):
            rows.heading(key, text=label)
            rows.column(key, width=width)
        rows.pack(fill=tk.BOTH, expand=True)
        status = tk.StringVar()
        ttk.Label(body, textvariable=status, wraplength=670).pack(anchor=tk.W, pady=8)

        def refresh():
            rows.delete(*rows.get_children())
            try:
                records = inventory(series)
                for row in records:
                    rows.insert("", tk.END, iid=row.identifier, values=(row.example, format_file_size(row.size),
                                datetime.fromtimestamp(row.modified).strftime("%Y-%m-%d %H:%M")))
                status.set(f"{len(records)} checkpoint(s) · {format_file_size(sum(row.size for row in records))}")
            except (OSError, ValueError) as exc:
                status.set(str(exc))

        def cleanup():
            try:
                paths = selected_files(series, rows.selection())
                identities = {path: file_identity(path) for path in paths}
                size = sum(identity.size for identity in identities.values())
            except (OSError, ValueError) as exc:
                status.set(str(exc))
                return
            if not paths:
                return
            if not messagebox.askyesno("Recycle partial downloads",
                    f"Move {len(paths)} checkpoint file(s), {format_file_size(size)}, to the Recycle Bin / Trash?\n"
                    "Affected downloads will restart from the beginning.", default="no", parent=window):
                return
            self.delete_running = True
            self.deletion_cancel = threading.Event()
            events = queue.Queue()
            recycle_button.configure(state="disabled")
            stop_button.configure(state="normal")

            def worker():
                try:
                    for result in recycle_checkpoints(series, paths, self.deletion_cancel, identities):
                        events.put(result)
                except Exception as exc:
                    events.put(str(exc))
                finally:
                    events.put(None)

            threading.Thread(target=worker, daemon=True).start()
            results = []
            errors = []

            def poll():
                while not events.empty():
                    result = events.get_nowait()
                    if result is None:
                        self.delete_running = False
                        recycle_button.configure(state="normal")
                        stop_button.configure(state="disabled")
                        refresh()
                        failed = [r.error for r in results if not r.success] + errors
                        status.set(f"{sum(r.success for r in results)} recycled · {len(failed)} failed · "
                                   f"{len(paths) - len(results)} stopped" + (f" · {failed[0]}" if failed else ""))
                        return
                    if isinstance(result, str):
                        errors.append(result)
                        status.set(result)
                    else:
                        results.append(result)
                        status.set(f"Processed {len(results)} / {len(paths)} checkpoint files")
                window.after(80, poll)

            poll()

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)
        recycle_button = ttk.Button(buttons, text="Recycle selected", command=cleanup)
        recycle_button.pack(side=tk.LEFT)
        stop_button = ttk.Button(buttons, text="Stop after current file", state="disabled",
                                 command=lambda: self.deletion_cancel.set())
        stop_button.pack(side=tk.LEFT, padx=8)

        def close():
            if self.delete_running:
                self.deletion_cancel.set()
            else:
                window.destroy()

        ttk.Button(buttons, text="Close", command=close).pack(side=tk.RIGHT)
        window.protocol("WM_DELETE_WINDOW", close)
        refresh()

    def close_app(self):
        self.cancel_event.set()
        self.pause_event.clear()
        if self.delete_running:
            self.deletion_cancel.set()
            self.root.after(100, self.close_app)
            return
        self.save_current_library()
        self.progress.stop()
        self.stop_progress_animation()
        self.root.destroy()

    def detect_ffmpeg_version(self):
        executable = shutil.which("ffmpeg")
        version = "FFmpeg unavailable"
        if executable:
            try:
                result = subprocess.run([executable, "-version"], capture_output=True,
                                        text=True, timeout=4)
                match = re.search(r"ffmpeg version\s+(\S+)", result.stdout)
                version = f"FFmpeg {match.group(1)}" if match else "FFmpeg available"
            except (OSError, subprocess.SubprocessError):
                version = "FFmpeg unavailable"
        self.emit(("ffmpeg_version", version))

    def refresh_system_status(self):
        try:
            destination = Path(self.output_var.get()).expanduser()
            while not destination.exists() and destination != destination.parent:
                destination = destination.parent
            free = format_file_size(shutil.disk_usage(destination).free)
            storage = f"{free} free"
        except OSError:
            storage = "Storage unavailable"
        self.system_status_var.set(f"{getattr(self, 'ffmpeg_version', 'FFmpeg...')}  ·  {storage}")

    def update_feedback_height(self):
        # The detail line and wrapped status text can change the card's height.
        needed = self.feedback_card.content.winfo_reqheight()
        self.feedback_card.configure(height=max(58, needed + 2 * self.feedback_card.inset_y + 6))

    def toggle_activity(self):
        self.activity_visible = not self.activity_visible
        if self.activity_visible:
            self.log_frame.configure(height=160)
            self.log_frame.pack_configure(fill=tk.BOTH, expand=True)
            self.log_box.pack(fill=tk.BOTH, expand=True)
            self.log_actions.pack(fill=tk.X, pady=(5, 0))
            self.activity_toggle.configure(text="Hide details")
        else:
            self.log_box.pack_forget()
            self.log_actions.pack_forget()
            self.log_frame.configure(height=48)
            self.log_frame.pack_configure(fill=tk.X, expand=False)
            self.activity_toggle.configure(text="Show details")

    def toggle_settings(self):
        self.settings_visible = not self.settings_visible
        if self.settings_visible:
            self.settings_frame.pack(fill=tk.X, before=self.actions_frame)
        else:
            self.settings_frame.pack_forget()
        self.refresh_settings_caption()

    def refresh_settings_caption(self):
        action = "Hide options" if self.settings_visible else "Options"
        count = f" · {len(self.pending_urls)} queued" if self.pending_urls else ""
        self.settings_toggle.configure(text=action + count)
        if hasattr(self, "run_queue_btn"):
            self.run_queue_btn.configure(text=f"Run Queue ({len(self.pending_urls)})",
                                         state="normal" if self.pending_urls and not self.running else "disabled")

    def choose_dir(self):
        selected = filedialog.askdirectory(initialdir=self.output_var.get())
        if selected:
            self.output_var.set(selected)
            self.remember_settings()

    def remember_settings(self, add_recent: bool = False):
        try:
            url = self.url_var.get().strip()
            if add_recent:
                self.recent_urls = [url] + [old for old in self.recent_urls if old != url]
                self.recent_urls = self.recent_urls[:10]
                self.url_entry.configure(values=self.recent_urls)
            save_settings(url, self.output_var.get().strip(), self.recent_urls,
                          self.pending_urls, self.history, self.prefer_hevc_var.get(),
                          self.marked_episodes)
        except OSError as exc:
            self.log(f"[!] Could not save settings: {exc}")

    def clear_recent(self):
        self.recent_urls.clear()
        self.url_entry.configure(values=())
        self.remember_settings()
        self.status_var.set("Recent URLs cleared")

    def show_history(self):
        window = tk.Toplevel(self.root)
        window.title("Download History")
        window.geometry("800x360")
        window.configure(background="#f3f6fb")
        frame = ttk.Frame(window, padding=(12, 8))
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Download History", style="Title.TLabel").pack(anchor=tk.W, pady=(0, 4))
        table = ttk.Treeview(frame, columns=("date", "title", "result", "folder"),
                             show="headings", style="Episodes.Treeview")
        for column, label, width in (("date", "Date", 130), ("title", "Series", 190),
                                     ("result", "Result", 200), ("folder", "Folder", 240)):
            table.heading(column, text=label, anchor=tk.W)
            table.column(column, width=width, anchor=tk.W)
        for index, entry in enumerate(self.history):
            table.insert("", tk.END, iid=str(index), values=(entry.get("date", ""),
                         entry.get("title", ""), entry.get("summary", ""), entry["folder"]))
        table.pack(fill=tk.BOTH, expand=True)

        def open_selected(_event=None):
            selected = table.selection()
            if not selected:
                return
            folder = Path(self.history[int(selected[0])]["folder"])
            if folder.is_dir():
                open_path(folder)
            else:
                messagebox.showinfo("Folder missing", "This download folder no longer exists.", parent=window)

        table.bind("<Double-1>", open_selected)
        ttk.Button(frame, text="Open Selected Folder", command=open_selected).pack(anchor=tk.E, pady=(4, 0))

    def export_summary(self):
        if not self.episode_order:
            messagebox.showinfo("No episodes", "Run a download before exporting a summary.")
            return
        target = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")],
                                              initialfile="download-summary.csv")
        if not target:
            return
        try:
            with open(target, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(("Episode", "Status", "Progress", "Size", "Saved file"))
                for item in self.episode_order:
                    writer.writerow([self.episode_list.set(item, column) for column in
                                     ("episode", "state", "percent", "size", "file")])
            self.status_var.set(f"Summary saved: {target}")
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))

    def add_to_queue(self):
        url = self.url_var.get().strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            messagebox.showwarning("Invalid URL", "Enter a valid series URL first.")
            return
        if url not in self.pending_urls:
            self.pending_urls.append(url)
            if self.queue_list.exists(url):
                self.queue_list.set(url, "status", "Waiting")
            else:
                self.queue_list.insert("", tk.END, iid=url, values=(url, "Waiting"))
            self.refresh_settings_caption()
            self.remember_settings()

    def remove_from_queue(self):
        for url in self.queue_list.selection():
            if url in self.pending_urls:
                self.pending_urls.remove(url)
            self.queue_list.delete(url)
        self.refresh_settings_caption()
        self.remember_settings()

    def add_multiple_urls(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Add series URLs")
        dialog.geometry("560x330")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Paste one HTTP or HTTPS URL per line.").pack(anchor=tk.W, pady=(0, 8))
        entry = scrolledtext.ScrolledText(body, height=9, wrap=tk.NONE, font=("Consolas", 9))
        entry.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

        def add():
            candidates = [line.strip() for line in entry.get("1.0", tk.END).splitlines() if line.strip()]
            invalid = [url for url in candidates if urlparse(url).scheme not in ("http", "https")
                       or not urlparse(url).netloc]
            if invalid:
                messagebox.showwarning("Invalid URLs", "Each line must contain one valid HTTP or HTTPS URL.", parent=dialog)
                return
            added = 0
            for url in candidates:
                if url not in self.pending_urls:
                    self.pending_urls.append(url)
                    if self.queue_list.exists(url):
                        self.queue_list.set(url, "status", "Waiting")
                    else:
                        self.queue_list.insert("", tk.END, iid=url, values=(url, "Waiting"))
                    added += 1
            self.refresh_settings_caption()
            self.remember_settings()
            self.status_var.set(f"Added {added} series URL(s) to the queue")
            dialog.destroy()

        ttk.Button(buttons, text="Add to queue", command=add).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        entry.focus_set()

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="Pause after episode")
            self.status_var.set("Resuming downloads...")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="Resume")
            self.status_var.set("Will pause after the current episode")

    def open_folder(self):
        path = Path(self.output_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state="disabled")

    def copy_log(self):
        contents = self.log_box.get("1.0", tk.END).strip()
        if contents:
            self.root.clipboard_clear()
            self.root.clipboard_append(contents)
            self.status_var.set("Log copied to clipboard")

    def open_episode(self, _event):
        selected = self.episode_list.selection()
        if selected:
            number = int(selected[0].split("-")[1])
            path = self.saved_files.get(number)
            if path and path.exists():
                open_path(path)

    def show_episode_menu(self, event):
        item = self.episode_list.identify_row(event.y)
        if not item:
            return
        if item not in self.episode_list.selection():
            self.episode_list.selection_set(item)
        selected = self.selected_episode_numbers()
        count = len(selected)
        menu = self.episode_menu
        menu.delete(0, tk.END)
        if count == 1:
            number = selected[0]
            path = self.saved_files.get(number)
            menu.add_command(label="Play", command=lambda: self.open_episode(None),
                             state="normal" if path and path.is_file() else "disabled")
            menu.add_command(label="Show in folder", command=self.reveal_selected,
                             state="normal" if path and path.exists() else "disabled")
            menu.add_command(label="Copy stream URL", command=self.copy_selected_links,
                             state="normal" if number in self.stream_urls else "disabled")
            menu.add_separator()
        menu.add_command(label=f"Download {count} episode{'s' if count != 1 else ''}",
                         command=self.download_selected, state="disabled" if self.running else "normal")
        menu.add_command(label="Mark downloaded", command=self.mark_selected)
        menu.add_command(label=f"Copy {count} stream URL{'s' if count != 1 else ''}",
                         command=self.copy_selected_links,
                         state="normal" if any(n in self.stream_urls for n in selected) else "disabled")
        priority_menu = tk.Menu(menu, tearoff=False)
        for priority in ("Urgent", "High", "Normal", "Low"):
            priority_menu.add_command(label=priority,
                                      command=lambda value=priority: self.set_selected_priority(value))
        menu.add_cascade(label="Batch priority", menu=priority_menu)
        export_menu = tk.Menu(menu, tearoff=False)
        for label, extension in (("M3U8 playlist", "m3u8"), ("JSON manifest", "json"),
                                 ("CSV inventory", "csv")):
            export_menu.add_command(label=label,
                                    command=lambda value=extension: self.export_selected(value))
        menu.add_cascade(label="Export", menu=export_menu)
        if count == 1 and selected[0] in self.failed_episodes:
            menu.add_command(label="Retry failed episode", command=self.retry_selected,
                             state="disabled" if self.running else "normal")
        menu.add_separator()
        menu.add_command(label="Delete selected files...", command=self.confirm_delete_selected,
                         state="normal" if not self.running and any(n in self.saved_files for n in selected) else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def selected_episode_numbers(self) -> list[int]:
        return [int(item.removeprefix("ep-")) for item in self.episode_list.selection()]

    def on_episode_click(self, event):
        item = self.episode_list.identify_row(event.y)
        if not item:
            return None
        if self.episode_list.identify_column(event.x) != "#1":
            if not event.state & 0x1:
                self.selection_anchor = item
            return None
        visible = list(self.episode_list.get_children(""))
        anchor = getattr(self, "selection_anchor", None)
        if event.state & 0x1 and anchor in visible:
            start, end = sorted((visible.index(anchor), visible.index(item)))
            self.episode_list.selection_add(*visible[start:end + 1])
        elif item in self.episode_list.selection():
            self.episode_list.selection_remove(item)
            self.selection_anchor = item
        else:
            self.episode_list.selection_add(item)
            self.selection_anchor = item
        self.episode_list.focus(item)
        return "break"

    def toggle_focused_episode(self, _event=None):
        item = self.episode_list.focus()
        if item:
            if item in self.episode_list.selection():
                self.episode_list.selection_remove(item)
            else:
                self.episode_list.selection_add(item)
        return "break"

    def update_episode_selection(self, _event=None):
        selected = set(self.episode_list.selection())
        for item in selected ^ self._displayed_selection:
            if self.episode_list.exists(item):
                marker = "☑" if item in selected else "☐"
                self.episode_list.set(item, "select", marker)
        self._displayed_selection = selected
        self.selection_caption.configure(text=f"{len(selected)} selected")
        for button in self.batch_buttons:
            button.configure(state="normal" if selected else "disabled")

    def toggle_select_all(self):
        visible = self.episode_list.get_children("")
        selected = set(self.episode_list.selection())
        if visible and all(item in selected for item in visible):
            self.episode_list.selection_remove(*visible)
        else:
            self.episode_list.selection_add(*visible)

    def deselect_episodes(self, _event=None):
        selected = self.episode_list.selection()
        if selected:
            self.episode_list.selection_remove(*selected)

    def select_pending(self):
        if self.running:
            return
        pending = [item for item in self.episode_list.get_children("")
                   if self.episode_list.set(item, "state") not in
                   ("Done", "Already downloaded", "Marked downloaded")]
        self.episode_list.selection_set(pending)

    def download_selected(self):
        numbers = set(self.selected_episode_numbers())
        if numbers and not self.running:
            url = getattr(self, "current_series_url", self.url_var.get().strip())
            self.url_var.set(url)
            marked = set(self.marked_episodes.get(url, []))
            self.marked_episodes[url] = sorted(marked - numbers)
            self.remember_settings()
            self.start_download(numbers)

    def mark_selected(self):
        if self.running:
            return
        numbers = set(self.selected_episode_numbers())
        if not numbers:
            return
        url = getattr(self, "current_series_url", self.url_var.get().strip())
        marked = set(self.marked_episodes.get(url, []))
        if numbers <= marked:
            marked -= numbers
            state = "Waiting"
        else:
            marked |= numbers
            state = "Marked downloaded"
        self.marked_episodes[url] = sorted(marked)
        for number in numbers:
            item = f"ep-{number}"
            if self.episode_list.exists(item):
                self.episode_list.set(item, "state", state)
        self.remember_settings()
        self.filter_episodes()
        self.save_current_library()

    def copy_selected_links(self):
        links = [self.stream_urls[n] for n in self.selected_episode_numbers()
                 if n in self.stream_urls]
        if not links:
            messagebox.showinfo("No stream links", "Stream URLs become available when a download resolves the episode. Scan Episodes only loads the episode list.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(links))
        self.status_var.set(f"Copied {len(links)} stream URL(s)")

    def set_selected_priority(self, priority: str):
        if self.running:
            return
        for number in self.selected_episode_numbers():
            self.episode_priorities[number] = priority
        self.status_var.set(f"Set {len(self.selected_episode_numbers())} episode(s) to {priority.lower()} priority")
        self.save_current_library()

    def reveal_selected(self):
        numbers = self.selected_episode_numbers()
        path = self.saved_files.get(numbers[0]) if numbers else None
        if path and path.exists():
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path.parent)])

    def export_selected(self, extension: str = "json"):
        numbers = self.selected_episode_numbers()
        if not numbers:
            return
        path = filedialog.asksaveasfilename(defaultextension=f".{extension}",
                                            filetypes=[(extension.upper(), f"*.{extension}")])
        if not path:
            return
        records = [{"episode": n, "status": self.episode_list.set(f"ep-{n}", "state"),
                    "file": str(self.saved_files.get(n, "")),
                    "stream_url": self.stream_urls.get(n, ""),
                    "priority": self.episode_priorities.get(n, "Normal")}
                   for n in numbers]
        try:
            if extension == "json":
                Path(path).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            elif extension == "csv":
                with open(path, "w", newline="", encoding="utf-8-sig") as output:
                    writer = csv.DictWriter(output, fieldnames=records[0].keys())
                    writer.writeheader()
                    writer.writerows(records)
            else:
                locations = [(r["episode"], Path(r["file"]).absolute().as_uri() if r["file"] else r["stream_url"])
                             for r in records if r["file"] or r["stream_url"]]
                if not locations:
                    raise ValueError("No saved files or stream URLs are available for a playlist")
                lines = ["#EXTM3U"]
                for number, location in locations:
                    lines.extend((f"#EXTINF:-1,Episode {number}", location))
                Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self.status_var.set(f"Exported {len(records)} episode(s) to {path}")

    def confirm_delete_selected(self, _event=None):
        if self.running or self.delete_running:
            return
        targets = [(n, self.saved_files[n]) for n in self.selected_episode_numbers()
                   if n in self.saved_files and self.saved_files[n].is_file()]
        if not targets:
            messagebox.showinfo("No saved files", "Select episodes with saved files to delete.")
            return
        self.confirm_delete_paths(targets)

    def confirm_delete_paths(self, targets: list[tuple[int, Path]]):
        sizes = []
        identities = {}
        for number, path in targets:
            try:
                identity = file_identity(path)
                identities[number] = identity
                sizes.append((number, path, identity.size))
            except (OSError, ValueError):
                continue
        if not sizes:
            messagebox.showinfo("Files unavailable", "The selected files are no longer available.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Confirm file cleanup")
        dialog.geometry("650x430")
        dialog.minsize(500, 350)
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=f"Delete {len(sizes)} saved file(s)?",
                  font=("Segoe UI", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(body, text=f"Estimated space: {format_file_size(sum(size for _, _, size in sizes))}").pack(anchor=tk.W, pady=(4, 10))
        paths = scrolledtext.ScrolledText(body, height=10, wrap=tk.NONE, font=("Consolas", 9))
        paths.pack(fill=tk.BOTH, expand=True)
        for _, path, size in sizes:
            paths.insert(tk.END, f"{format_file_size(size):>10}  {path.absolute()}\n")
        paths.configure(state="disabled")
        recycle_var = tk.BooleanVar(value=True)
        purge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="Move files to Recycle Bin / Trash", variable=recycle_var).pack(anchor=tk.W, pady=(10, 2))
        ttk.Checkbutton(body, text="Purge successful entries from episode list", variable=purge_var).pack(anchor=tk.W)
        warning = ttk.Label(body, text="", foreground="#DC2626")
        warning.pack(anchor=tk.W, pady=(5, 0))

        def refresh_warning(*_args):
            warning.configure(text="Permanent deletion cannot be undone." if not recycle_var.get() else "")

        recycle_var.trace_add("write", refresh_warning)
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(12, 0))
        cancel_button = ttk.Button(buttons, text="Cancel", command=dialog.destroy)
        cancel_button.pack(side=tk.RIGHT)

        def commit():
            recycle = recycle_var.get()
            purge = purge_var.get()
            dialog.destroy()
            self.start_deletion([(n, path) for n, path, _ in sizes], recycle, purge, identities=identities)

        delete_button = ttk.Button(buttons, text="Delete files", command=commit)
        delete_button.pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Shift-Delete>", lambda _event: commit())
        def activate_focused(_event):
            focused = dialog.focus_get()
            if focused in (cancel_button, delete_button):
                focused.invoke()
            return "break"

        dialog.bind("<Return>", activate_focused)
        cancel_button.focus_set()

    def start_deletion(self, targets: list[tuple[int, Path]], recycle: bool, purge: bool, identities=None):
        if self.running or self.delete_running:
            return
        self.delete_running = True
        stop_event = threading.Event()
        self.deletion_cancel = stop_event
        events: queue.Queue = queue.Queue()
        started = time.monotonic()
        results = []
        dialog = tk.Toplevel(self.root)
        dialog.title("Deleting saved files")
        dialog.geometry("660x360")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="File cleanup in progress", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        summary = tk.StringVar(value=f"0 of {len(targets)} files processed")
        ttk.Label(body, textvariable=summary).pack(anchor=tk.W, pady=(5, 8))
        progress = ttk.Progressbar(body, maximum=len(targets), mode="determinate")
        progress.pack(fill=tk.X, pady=(0, 10))
        rows = ttk.Treeview(body, columns=("episode", "state", "path"), show="headings")
        for key, label, width in (("episode", "Episode", 80), ("state", "State", 130),
                                  ("path", "File", 400)):
            rows.heading(key, text=label)
            rows.column(key, width=width, stretch=key == "path")
        rows.pack(fill=tk.BOTH, expand=True)
        for number, path in targets:
            rows.insert("", tk.END, iid=str(number), values=(number, "Queued", str(path)))
        stop_button = ttk.Button(body, text="Stop after current file", command=stop_event.set)
        stop_button.pack(anchor=tk.E, pady=(10, 0))
        dialog.protocol("WM_DELETE_WINDOW", stop_event.set)

        def worker():
            try:
                for number, result in delete_batch(targets, stop_event, recycle=recycle,
                                                    on_start=lambda n: events.put(("active", n)),
                                                    identities=identities):
                    events.put(("result", (number, result)))
            finally:
                events.put(("finished", None))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            while True:
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    break
                if kind == "active":
                    rows.set(str(value), "state", "Deleting...")
                elif kind == "result":
                    number, result = value
                    results.append((number, result))
                    rows.set(str(number), "state", "Deleted" if result.success else "Failed")
                    progress["value"] = len(results)
                    elapsed = max(time.monotonic() - started, 0.001)
                    reclaimed = sum(r.size for _, r in results if r.success)
                    summary.set(f"{len(results)} of {len(targets)} files processed · "
                                f"{format_file_size(reclaimed)} · {format_file_size(int(reclaimed / elapsed))}/s processing rate")
                    if result.success:
                        self.saved_files.pop(number, None)
                        item = f"ep-{number}"
                        if self.episode_list.exists(item):
                            if purge:
                                self.episode_list.delete(item)
                                self.episode_order.remove(item)
                                self.stream_urls.pop(number, None)
                                self.episode_priorities.pop(number, None)
                                self.failed_episodes.discard(number)
                                url = getattr(self, "current_series_url", self.url_var.get().strip())
                                self.marked_episodes[url] = [n for n in self.marked_episodes.get(url, []) if n != number]
                                self.library.remove_episode(url, number)
                            else:
                                self.episode_list.set(item, "state", "Deleted")
                                self.episode_list.set(item, "file", "—")
                                self.episode_list.set(item, "size", "—")
                        self.filter_episodes()
                elif kind == "finished":
                    dialog.destroy()
                    self.delete_running = False
                    self.remember_settings()
                    self.save_current_library()
                    self.refresh_system_status()
                    self.show_deletion_summary(results, len(targets), time.monotonic() - started,
                                               recycle, purge)
                    return
            dialog.after(80, poll)

        dialog.after(80, poll)

    def show_deletion_summary(self, results, requested: int, duration: float,
                              recycle: bool, purge: bool):
        succeeded = [result for _, result in results if result.success]
        failed = [(number, result) for number, result in results if not result.success]
        reclaimed = sum(result.size for result in succeeded)
        window = tk.Toplevel(self.root)
        window.title("File cleanup summary")
        window.geometry("620x390" if failed else "520x250")
        window.transient(self.root)
        window.grab_set()
        body = ttk.Frame(window, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        metric = "moved to Recycle Bin" if recycle else "permanently deleted"
        ttk.Label(body, text=f"{format_file_size(reclaimed)} {metric}",
                  font=("Segoe UI", 17, "bold"), foreground="#059669").pack(anchor=tk.W)
        ttk.Label(body, text=f"{len(succeeded)} deleted · {len(failed)} failed · "
                  f"{requested - len(results)} stopped · {duration:.1f}s · "
                  f"{format_file_size(int(reclaimed / max(duration, 0.001)))}/s processing rate").pack(anchor=tk.W, pady=(4, 10))
        if failed:
            ttk.Label(body, text="Files still in place", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
            failures = ttk.Treeview(body, columns=("episode", "file", "lock"),
                                    show="headings", height=6, selectmode="browse")
            for column, title, width in (("episode", "Episode", 65),
                                         ("file", "File", 260), ("lock", "In use by / error", 250)):
                failures.heading(column, text=title)
                failures.column(column, width=width, stretch=column != "episode")
            failures.pack(fill=tk.BOTH, expand=True)
            for number, result in failed:
                locker = ", ".join(f"{p.name} (PID {p.pid})" for p in result.lockers)
                failures.insert("", tk.END, iid=str(number),
                                values=(number, str(result.path), locker or result.error))
            failures.selection_set(str(failed[0][0]))
            error_detail = tk.StringVar()
            detail_label = ttk.Label(body, textvariable=error_detail, wraplength=560)
            detail_label.pack(anchor=tk.W, pady=(7, 0))

            def selected_failure():
                selected = failures.selection()
                return next(((n, r) for n, r in failed if str(n) == selected[0]), None) if selected else None

            def show_failure_detail(_event=None):
                current = selected_failure()
                if current:
                    error_detail.set(current[1].error)

            failures.bind("<<TreeviewSelect>>", show_failure_detail)
            show_failure_detail()
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side=tk.RIGHT)
        if recycle and succeeded and os.name == "nt":
            ttk.Button(buttons, text="Open Recycle Bin", command=open_recycle_bin).pack(side=tk.LEFT)
        if failed:
            def retry(targets):
                window.destroy()
                self.confirm_delete_paths([(n, r.path) for n, r in targets])

            def retry_one():
                current = selected_failure()
                if current:
                    retry([current])

            def reveal_one():
                current = selected_failure()
                if current:
                    self.show_file_in_folder(current[1].path)

            ttk.Button(buttons, text="Retry all failed", command=lambda: retry(failed)).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(buttons, text="Retry selected", command=retry_one).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(buttons, text="Show in folder", command=reveal_one).pack(side=tk.LEFT, padx=(8, 0))
        window.bind("<Escape>", lambda _event: window.destroy())

    def show_file_in_folder(self, path: Path):
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path.parent)])

    def sort_episodes(self, column: str):
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        rows = list(self.episode_order)

        def sort_value(item: str):
            if column == "episode":
                return int(item.split("-")[1])
            if column == "size":
                path = self.saved_files.get(int(item.split("-")[1]))
                try:
                    return path.stat().st_size if path else -1
                except OSError:
                    return -1
            if column == "percent":
                value = self.episode_list.set(item, "percent").rstrip("%")
                try:
                    return float(value)
                except ValueError:
                    return -1.0
            return self.episode_list.set(item, column).casefold()

        rows.sort(key=sort_value, reverse=self.sort_reverse)
        self.episode_order = rows
        self.filter_episodes()
        for key, label in (("episode", "Episode"), ("state", "Status"),
                           ("percent", "Progress"), ("size", "Size"), ("file", "Saved file")):
            arrow = " ↓" if self.sort_reverse else " ↑"
            self.episode_list.heading(key, text=label + (arrow if key == column else ""))

    def filter_episodes(self):
        if not hasattr(self, "episode_list"):
            return
        query = self.episode_search.get().strip().casefold()
        selected_status = self.episode_filter.get()
        visible = []
        for item in self.episode_order:
            if not self.episode_list.exists(item):
                continue
            number = item.removeprefix("ep-")
            state = self.episode_list.set(item, "state")
            matches_status = (selected_status == "All"
                              or (selected_status == "Done" and state in ("Done", "Already downloaded", "Marked downloaded"))
                              or (selected_status == "Downloading" and
                                  (state in ("Downloading...", "Finding video...", "Verifying...") or
                                   state.startswith("Retrying")))
                              or state.startswith(selected_status))
            search_text = f"{number} {self.episode_list.set(item, 'episode')} {self.episode_list.set(item, 'file')}".casefold()
            if query in search_text and matches_status:
                visible.append(item)
            else:
                self.episode_list.detach(item)
        for index, item in enumerate(visible):
            self.episode_list.move(item, "", index)
            state = self.episode_list.set(item, "state")
            tone = ("done" if state in ("Done", "Already downloaded", "Marked downloaded") else
                    "failed" if state.startswith(("Failed", "Skipped")) else
                    "active" if state.startswith(("Downloading", "Finding", "Retrying", "Verifying")) else "")
            tags = ("even" if index % 2 == 0 else "odd",)
            self.episode_list.item(item, tags=tags + ((tone,) if tone else ()))
        if visible:
            self.empty_table_label.place_forget()
        else:
            message = ("No episodes match this filter." if self.episode_order else
                       "Scan a series URL to load episodes here.")
            self.empty_table_label.configure(text=message)
            self.empty_table_label.place(relx=0.5, rely=0.52, anchor=tk.CENTER)

    def cancel_download(self):
        self.cancel_event.set()
        self.pause_event.clear()
        self.status_var.set("Stopping after the current step...")
        self.cancel_btn.configure(state="disabled")

    def retry_failed(self):
        if self.failed_episodes and not self.running:
            self.start_download(set(self.failed_episodes))

    def retry_selected(self):
        if self.running:
            return
        selected = self.episode_list.selection()
        if not selected:
            messagebox.showinfo("Select an episode", "Select an episode row first.")
            return
        number = int(selected[0].split("-")[1])
        if number not in self.failed_episodes:
            messagebox.showinfo("Already downloaded", "Select a failed or skipped episode to retry.")
            return
        self.start_download({number})

    def log(self, msg: str):
        self.log_queue.put(msg)

    def emit(self, event):
        context = getattr(self, "worker_context", None)
        source = getattr(context, "url", None)
        self.log_queue.put((*event, source) if source else event)

    def set_worker_source(self, url):
        if not hasattr(self, "worker_context"):
            self.worker_context = threading.local()
        self.worker_context.url = url

    def stop_progress_animation(self):
        if self.progress_animation is not None:
            self.root.after_cancel(self.progress_animation)
            self.progress_animation = None

    def show_indeterminate_progress(self):
        if self.progress_mode == "indeterminate":
            return
        self.stop_progress_animation()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress_mode = "indeterminate"
        self.progress_target = 0.0
        self.progress.start(12)

    def set_progress_target(self, percent: float, immediate: bool = False):
        target = min(100.0, max(0.0, percent))
        if self.progress_mode == "determinate" and target == self.progress_target and not immediate:
            return
        if self.progress_mode != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.progress_mode = "determinate"
        self.stop_progress_animation()
        self.progress_target = target
        start = float(self.progress["value"])
        if immediate or target <= start or target - start < 0.2:
            self.progress.configure(value=target)
            return

        started = time.monotonic()
        duration = 0.28

        def animate():
            elapsed = min(1.0, (time.monotonic() - started) / duration)
            eased = 1 - (1 - elapsed) ** 3
            self.progress.configure(value=start + (target - start) * eased)
            if elapsed < 1:
                self.progress_animation = self.root.after(16, animate)
            else:
                self.progress_animation = None

        animate()

    def poll_logs(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            if isinstance(msg, tuple):
                kind, value = msg[:2]
                source = msg[2] if len(msg) == 3 else None
                if (source and kind not in ("series_context", "queue_state", "queue_done", "history", "done", "scan_done")
                        and source != getattr(self, "current_series_url", None)):
                    continue
                if kind == "status":
                    self.status_var.set(value)
                    self.root.after_idle(self.update_feedback_height)
                elif kind == "series_context":
                    value, output_root = value
                    if getattr(self, "current_series_url", None) != value:
                        self.save_current_library()
                        self.library_can_save = False
                        for item in self.episode_order:
                            self.episode_list.delete(item)
                        self.episode_order = []
                        self.stream_urls.clear()
                        self.episode_priorities.clear()
                        self.saved_files.clear()
                        self.failed_episodes.clear()
                    self.current_series_url = value
                    self.current_output_root = output_root
                    self.current_series_folder = ""
                    stored = self.library.get(value)
                    self.episode_priorities = {r["number"]: r["priority"] for r in stored["episodes"]} if stored else {}
                elif kind == "series_folder":
                    self.current_series_folder = value
                elif kind == "series_name":
                    self.series_name_var.set(value)
                elif kind == "ffmpeg_version":
                    self.ffmpeg_version = value
                    self.refresh_system_status()
                elif kind == "detail":
                    self.detail_var.set(value)
                    if value:
                        self.detail_label.pack(anchor=tk.W, pady=(2, 0))
                    else:
                        self.detail_label.pack_forget()
                    self.root.after_idle(self.update_feedback_height)
                elif kind == "progress":
                    self.count_var.set(f"Episodes: {value[0]} / {value[1]}")
                    if self.scanning and value[1]:
                        self.set_progress_target(value[0] / value[1] * 100, immediate=True)
                elif kind == "video_progress":
                    _done, _total, percent = value
                    self.set_progress_target(percent)
                elif kind == "unknown_progress":
                    self.show_indeterminate_progress()
                elif kind == "episodes":
                    self.library_can_save = True
                    stored = self.library.get(getattr(self, "current_series_url", ""))
                    stored_rows = {r["number"]: r for r in stored["episodes"]} if stored else {}
                    for item in self.episode_order:
                        self.episode_list.delete(item)
                    self.episode_order = []
                    self._displayed_selection.clear()
                    for row, episode in enumerate(value):
                        number, title = episode if isinstance(episode, tuple) else (episode, f"Episode {episode}")
                        item = f"ep-{number}"
                        previous_path = stored_rows.get(number, {}).get("file", "")
                        if (previous_path and Path(previous_path).is_file() and stored
                                and Path(stored["output_root"]).resolve() == Path(self.current_output_root).resolve()):
                            self.saved_files[number] = Path(previous_path)
                        marked = number in self.marked_episodes.get(getattr(self, "current_series_url", ""), [])
                        self.episode_list.insert("", tk.END, iid=item,
                                                 values=("☐", title or f"Episode {number}",
                                                         "Marked downloaded" if marked else "Waiting",
                                                         "—", "—", str(self.saved_files.get(number, "—"))),
                                                 tags=("even" if row % 2 == 0 else "odd",))
                        self.episode_order.append(item)
                    self.filter_episodes()
                elif kind == "stream_url":
                    number, stream = value
                    self.stream_urls[number] = stream
                elif kind == "episode_state":
                    number, state = value
                    item = f"ep-{number}"
                    if self.episode_list.exists(item):
                        self.episode_list.set(item, "state", state)
                        if state == "Done":
                            self.episode_list.set(item, "percent", "100%")
                        elif state == "Downloading..." or state.startswith("Retrying"):
                            self.episode_list.set(item, "percent",
                                                  "Retrying" if state.startswith("Retrying") else "Working")
                            self.show_indeterminate_progress()
                        elif state == "Verifying...":
                            self.episode_list.set(item, "percent", "Checking")
                            self.show_indeterminate_progress()
                        self.episode_list.see(item)
                    if state == "Finding video...":
                        self.show_indeterminate_progress()
                    if state in ("Done", "Already downloaded") and not self.scanning:
                        self.set_progress_target(100, immediate=True)
                    if state.startswith("Failed") or state.startswith("Skipped"):
                        self.failed_episodes.add(number)
                    elif state in ("Done", "Already downloaded"):
                        self.failed_episodes.discard(number)
                    self.filter_episodes()
                elif kind == "episode_percent":
                    number, percent = value
                    item = f"ep-{number}"
                    if self.episode_list.exists(item):
                        self.episode_list.set(item, "percent", percent)
                elif kind == "saved_file":
                    number, path = value
                    self.saved_files[number] = Path(path)
                    item = f"ep-{number}"
                    if self.episode_list.exists(item):
                        self.episode_list.set(item, "file", str(Path(path).absolute()))
                        try:
                            self.episode_list.set(item, "size", format_file_size(Path(path).stat().st_size))
                        except OSError:
                            self.episode_list.set(item, "size", "—")
                elif kind in ("done", "scan_done"):
                    self.stop_progress_animation()
                    self.progress.stop()
                    self.running = False
                    self.scanning = False
                    if kind == "scan_done" and value:
                        self.status_var.set(value)
                    self.scan_btn.configure(state="normal")
                    self.start_btn.configure(state="normal")
                    self.refresh_settings_caption()
                    self.cancel_btn.configure(state="disabled")
                    self.pause_btn.configure(state="disabled", text="Pause after episode")
                    self.add_queue_btn.configure(state="normal")
                    self.remove_queue_btn.configure(state="normal")
                    self.retry_btn.configure(state="normal" if self.failed_episodes else "disabled")
                    self.save_current_library()
                    if value and kind == "done":
                        self.root.bell()
                        messagebox.showinfo("Download finished", value)
                elif kind == "queue_done":
                    if value in self.pending_urls:
                        self.pending_urls.remove(value)
                        self.refresh_settings_caption()
                        self.remember_settings()
                elif kind == "queue_state":
                    url, state = value
                    if self.queue_list.exists(url):
                        self.queue_list.set(url, "status", state)
                elif kind == "history":
                    self.history = [value] + [entry for entry in self.history
                                              if entry.get("url") != value["url"]]
                    self.history = self.history[:100]
                    self.remember_settings()
                if kind in ("episodes", "episode_state", "saved_file", "series_folder"):
                    self.library_dirty = True
                continue
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, msg + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")
        if self.library_dirty:
            self.save_current_library()
        self.root.after(100, self.poll_logs)

    def scan_episodes(self):
        self.start_download(scan_only=True)

    def start_download(self, retry_only: set[int] | None = None, use_queue: bool = False, *, scan_only=False):
        if self.running or self.delete_running:
            return
        if not scan_only and not shutil.which("ffprobe"):
            messagebox.showerror("Video verification unavailable", "Install FFprobe on PATH before downloading or checking existing videos.")
            return
        if use_queue and not self.pending_urls:
            messagebox.showinfo("Queue empty", "Add a series URL to the queue first.")
            return
        url = (self.pending_urls[0] if use_queue else
               getattr(self, "current_series_url", "") if retry_only is not None else
               self.url_var.get().strip())
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a video or series URL.")
            return

        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            messagebox.showwarning("Invalid URL", "Enter a valid http or https URL.")
            return
        destination = (getattr(self, "current_output_root", self.output_var.get())
                       if retry_only is not None else self.output_var.get())
        if scan_only and not destination.strip():
            destination = str(Path('downloads').resolve())
        if not destination.strip():
            messagebox.showwarning("Missing directory", "Choose a save directory.")
            return
        output_root = Path(destination).expanduser().resolve()
        try:
            if not scan_only:
                output_root.mkdir(parents=True, exist_ok=True)
            if not scan_only and shutil.disk_usage(output_root).free < 512 * 1024 * 1024:
                messagebox.showwarning("Low disk space", "Less than 512 MB is free in the save location.")
                return
        except OSError as exc:
            messagebox.showerror("Save folder unavailable", str(exc))
            return
        try:
            selected_episodes = retry_only if retry_only is not None else parse_episode_selection(self.episode_range_var.get())
        except ValueError as exc:
            messagebox.showwarning("Invalid episodes", str(exc))
            return
        self.remember_settings(add_recent=True)
        self.save_current_library()
        self.status_var.set("Opening browser and inspecting the page...")
        self.series_name_var.set("Loading series...")
        self.count_var.set("Episodes: 0 / 0")
        self.detail_var.set("")
        self.detail_label.pack_forget()
        self.root.after_idle(self.update_feedback_height)
        if retry_only is None:
            self.library_can_save = False
            self.failed_episodes.clear()
            self.saved_files.clear()
            self.stream_urls.clear()
            self.episode_priorities.clear()
            for item in self.episode_order:
                self.episode_list.delete(item)
            self.episode_order = []
            self.filter_episodes()
        self.cancel_event.clear()
        self.pause_event.clear()
        self.running = True
        self.scanning = scan_only
        self.scan_btn.configure(state="disabled")
        self.run_queue_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled" if scan_only else "normal", text="Pause after episode")
        self.add_queue_btn.configure(state="disabled")
        self.remove_queue_btn.configure(state="disabled")
        self.progress_mode = "determinate"
        self.show_indeterminate_progress()
        self.start_btn.configure(state="disabled")
        urls = list(self.pending_urls) if use_queue else [url]
        threading.Thread(target=self.run_queue,
                         args=(urls, output_root, not self.headful_var.get(), selected_episodes,
                               retry_only is not None, self.translate_folder_var.get(),
                               self.prefer_hevc_var.get(), use_queue, scan_only),
                         daemon=True).start()

    def run_queue(self, urls: list[str], output_root: Path, headless: bool,
                  selected_episodes: set[int] | None, preserve_table: bool,
                  translate_folder: bool, prefer_hevc: bool, consume_queue: bool, scan_only=False):
        summaries = []
        for index, url in enumerate(urls, start=1):
            self.set_worker_source(url)
            if self.cancel_event.is_set():
                break
            self.emit(("status", f"Series {index} of {len(urls)}: opening page..."))
            if consume_queue:
                self.emit(("queue_state", (url, "Scanning" if scan_only else "Downloading")))
            summary = self.worker(url, output_root, headless, selected_episodes,
                                  finalize=False, preserve_table=preserve_table,
                                  translate_folder=translate_folder, prefer_hevc=prefer_hevc, scan_only=scan_only)
            summaries.append(summary)
            if consume_queue and not scan_only:
                failed_match = re.search(r"(\d+) failed or skipped", summary)
                state = ("Cancelled" if summary.startswith("Cancelled:") else
                         "Failed" if summary.startswith("Stopped:") or
                         (failed_match and int(failed_match.group(1)) > 0) else "Done")
                self.emit(("queue_state", (url, state)))
                if state == "Done":
                    self.emit(("queue_done", url))
            while self.pause_event.is_set() and not self.cancel_event.is_set():
                time.sleep(0.2)
        result = "\n".join(summaries)
        if scan_only and not summaries and self.cancel_event.is_set():
            result = "Scan cancelled. No downloads were started."
        self.emit(("scan_done" if scan_only else "done", result))

    def download_with_retries(self, stream_url: str, out_file: Path, referer: str,
                              episode_number: int, episode_index: int,
                              total_count: int, duration: float | None) -> Path:
        for attempt in range(1, MAX_EPISODE_ATTEMPTS + 1):
            if self.cancel_event.is_set():
                raise RuntimeError("Download cancelled")
            try:
                self.download_m3u8(stream_url, out_file, referer, episode_number,
                                   episode_index - 1, total_count, duration)
                if self.cancel_event.is_set():
                    raise RuntimeError("Download cancelled")
                self.emit(("status", f"Verifying episode {episode_index} of {total_count}..."))
                self.emit(("detail", "Checking the saved video..."))
                self.emit(("episode_state", (episode_number, "Verifying...")))
                saved = verify_episode_output(out_file, duration or 0)
                if self.cancel_event.is_set():
                    raise RuntimeError("Download cancelled")
                return saved
            except Exception as exc:
                if (self.cancel_event.is_set() or attempt == MAX_EPISODE_ATTEMPTS
                        or not should_retry_download(exc)):
                    raise
                delay = RETRY_DELAYS[attempt - 1]
                next_attempt = attempt + 1
                self.log(f"[!] Episode {episode_number} attempt {attempt} failed: {exc}")
                self.emit(("episode_state", (episode_number,
                                                     f"Retrying ({next_attempt}/{MAX_EPISODE_ATTEMPTS})...")))
                self.emit(("status", f"Episode {episode_index} of {total_count}: "
                                               f"retry {next_attempt}/{MAX_EPISODE_ATTEMPTS} in {delay}s..."))
                self.emit(("detail", "Waiting for the next attempt..."))
                if self.cancel_event.wait(delay):
                    raise RuntimeError("Download cancelled") from exc
                self.emit(("episode_state", (episode_number, "Downloading...")))
                self.emit(("status", f"Downloading episode {episode_index} of {total_count} "
                                               f"(attempt {next_attempt}/{MAX_EPISODE_ATTEMPTS})..."))

    def navigate_with_retries(self, page, url: str, timeout: int):
        max_attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, max_attempts + 1):
            if self.cancel_event.is_set():
                raise RuntimeError("Download cancelled")
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                status = response.status if response else None
                if status != 429 and (status is None or status < 500):
                    return response
                reason = f"HTTP {status}"
            except Exception as exc:
                if (self.cancel_event.is_set() or attempt == max_attempts
                        or not should_retry_page_error(exc)):
                    raise
                reason = str(exc).splitlines()[0]
            else:
                if attempt == max_attempts:
                    raise RuntimeError(f"Page returned {reason} after {max_attempts} attempts")
            delay = RETRY_DELAYS[attempt - 1]
            self.log(f"[!] Page load attempt {attempt} failed: {reason}")
            self.emit(("status", f"Page load interrupted; retry {attempt + 1}/{max_attempts} in {delay}s..."))
            if self.cancel_event.wait(delay):
                raise RuntimeError("Download cancelled")

    def resolve_episode_source(self, page, episode, series_url, captured, seen):
        href = episode.get("href")
        destination = urljoin(series_url, href) if href else None
        if destination and urlparse(destination).scheme in ("http", "https") and destination.rstrip("/") != page.url.rstrip("/"):
            captured.clear()
            self.navigate_with_retries(page, destination, timeout=25000)
        elif episode.get("attrId"):
            captured.clear()
            control = page.locator(f"[data-hg-ep='{episode['attrId']}']").first
            control.scroll_into_view_if_needed(timeout=3000)
            control.click(force=True)
        source = None
        for attempt in range(21):
            if self.cancel_event.is_set():
                raise RuntimeError("Download cancelled")
            source = page.evaluate("""() => {
                const el = document.getElementById('videoInitialData');
                if (!el) return null;
                try {
                    const data = JSON.parse(el.textContent);
                    const info = (data.trackingEpisodes || {})[String(data.ep)] || {};
                    return {ep: Number(data.ep),
                        url: data.videoSrc || (data.epPlaySrcs || {})[String(data.ep)],
                        hevc_url: data.videoSrcHevc || (data.epPlaySrcsHevc || {})[String(data.ep)],
                        duration: Number(info.drama_duration || (data.tracking || {}).drama_duration || 0)};
                } catch (_) { return null; }
            }""")
            if source and source.get("ep") == episode["epNum"] and (source.get("url") or source.get("hevc_url")):
                return source
            # Explicit episode metadata takes precedence over captured requests,
            # which can include preloaded videos or advertisements.
            if source is None and any(stream not in seen for stream in captured):
                return None
            if attempt < 20:
                page.wait_for_timeout(500)
        if source and source.get("ep") != episode["epNum"]:
            raise RuntimeError(f"Page shows episode {source.get('ep')}; expected {episode['epNum']}")
        return source

    def download_m3u8(self, stream_url: str, out_file_template: Path, referer: str, episode_number: int, completed_count: int, total_count: int, duration: float | None = None):
        # Native yt-dlp HLS downloads preserve .part and .ytdl fragment checkpoints.
        # FFmpeg remains responsible for merging/remuxing finished media.
        last_update = 0.0

        def on_progress(data):
            nonlocal last_update
            if self.cancel_event.is_set():
                raise RuntimeError("Download cancelled")
            if data.get("status") == "finished":
                self.emit(("detail", "Processing video..."))
                self.emit(("episode_percent", (episode_number, "Processing")))
                return
            if data.get("status") != "downloading":
                return
            now = time.monotonic()
            if now - last_update < 0.25:
                return
            last_update = now
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            fragment_count = data.get("fragment_count")
            fragment_index = data.get("fragment_index")
            if total:
                ratio = min(1.0, downloaded / total)
            elif fragment_count and fragment_index is not None:
                ratio = min(1.0, fragment_index / fragment_count)
            else:
                ratio = None
            percent = f"{ratio:.0%}" if ratio is not None else "Downloading"
            if ratio is not None:
                self.emit(("episode_percent", (episode_number, percent)))
                self.emit(("video_progress", (completed_count, total_count, ratio * 100)))
            else:
                self.emit(("unknown_progress", None))
            speed = data.get("_speed_str") or ""
            eta = data.get("_eta_str") or ""
            detail = f"Current video: {percent}"
            if speed:
                detail += f"    {speed}"
            if eta:
                detail += f"    ETA {eta}"
            self.emit(("detail", detail))

        download_with_progress(stream_url, out_file_template, referer,
                               self.cancel_event, on_progress)

    def worker(self, series_url: str, output_root: Path, headless: bool,
               retry_only: set[int] | None = None, finalize: bool = True,
               preserve_table: bool = False, translate_folder: bool = True,
               prefer_hevc: bool = False, scan_only: bool = False):
        self.set_worker_source(series_url)
        current_streams: list[str] = []
        all_seen_streams: set[str] = set()
        result_summary = ""

        def on_request(request):
            u = request.url
            if MEDIA_URL_PATTERN.search(u) and not u.endswith(".ts"):
                if u not in current_streams:
                    current_streams.append(u)

        def on_response(response):
            u = response.url
            ctype = response.headers.get("content-type", "").lower()
            if any(m in ctype for m in ["mpegurl", "video/mp4", "dash+xml"]):
                if u not in current_streams and not u.endswith(".ts"):
                    current_streams.append(u)

        try:
            self.emit(("series_context", (series_url, str(output_root))))
            self.log(f"[*] Launching browser for: {series_url}")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 900},
                    ignore_https_errors=False,
                )
                if scan_only:
                    context.route("**/*", lambda route: route.abort() if block_scan_media_request(route.request)
                                  else route.continue_())
                page = context.new_page()
                page.on("request", on_request)
                page.on("response", on_response)

                response = self.navigate_with_retries(page, series_url, timeout=35000)
                try:
                    page.wait_for_selector("#videoInitialData, [data-web-play-ep-grid], video source",
                                           state="attached", timeout=5000)
                except Exception:
                    pass
                title = page.title().lower()
                if (response and response.status >= 400) or any(
                    marker in title for marker in ("\u9875\u9762\u672a\u627e\u5230", "page not found", "404 not found")
                ):
                    status = f"HTTP {response.status}" if response else "unknown status"
                    raise RuntimeError(
                        f"The site returned a page-not-found or error page ({status}). "
                        "Check the URL or open it in a browser."
                    )

                # Some pages lazy-load the episode list after scrolling.
                if page.locator("[data-web-play-ep-grid]").count() == 0:
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(500)

                raw_title = page.title().split("-")[0].split("|")[0].strip()
                series_title = sanitize_filename(raw_title or "series_download")
                self.emit(("series_name", raw_title or "Series download"))
                if translate_folder and not scan_only:
                    try:
                        series_title = translate_folder_name(series_title)
                    except Exception as exc:
                        self.log(f"[!] English folder name unavailable; using original title: {exc}")
                stored = self.library.get(series_url)
                conflicts = conflicting_claims(self.library, series_url)
                series_dir = series_folder(output_root, series_title, series_url, stored, create=not scan_only)
                self.emit(("series_folder", str(series_dir)))

                if not headless:
                    self.log("[*] Open the episode list in the browser. Scanning will start in 20 seconds.")
                    self.emit(("status", "Open the episode list in the browser; scanning starts in 20 seconds..."))
                    if self.cancel_event.wait(20):
                        raise RuntimeError("Download cancelled")

                # Open common episode-list controls when present.
                for label in ("\u9009\u96c6", "\u9078\u96c6", "\u5267\u96c6",
                              "\u5287\u96c6", "\u5168\u90e8\u5267\u96c6", "Episodes"):
                    try:
                        control = page.get_by_text(label, exact=True).first
                        if control.is_visible(timeout=500):
                            control.click(timeout=1500)
                            page.wait_for_timeout(1000)
                            break
                    except Exception:
                        pass

                # Tag and collect all episode buttons in the DOM
                # Matches "第1集", "第2集 VIP", "第02集", "EP1", or items inside an episode list container
                episode_targets = page.evaluate(
                    """() => {
                        const results = [];
                        const seenNums = new Set();

                        // Find leaf-most elements containing 第X集
                        const allEls = Array.from(document.querySelectorAll('a, button, li, div, span'));
                        for (const el of allEls) {
                            const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                            if (text.length > 25) continue; // Skip large wrapper containers

                            const match = text.match(/第\\s*(\\d+)\\s*集/) || text.match(/^[Ee][Pp]\\.?\\s*(\\d+)/);
                            if (!match) continue;

                            // Prefer elements that don't have children with the exact same match
                            const childHasMatch = Array.from(el.children).some(
                                c => /第\\s*\\d+\\s*集/.test((c.innerText || ''))
                            );
                            if (childHasMatch) continue;

                            const epNum = parseInt(match[1], 10);
                            if (seenNums.has(epNum)) continue;
                            seenNums.add(epNum);

                            // Click target is either the element or its closest interactive parent
                            const clickTarget = el.closest('a, button, li') || el;
                            const attrId = 'ep-target-' + epNum;
                            clickTarget.setAttribute('data-hg-ep', attrId);

                            const href = clickTarget.getAttribute('href');
                            results.push({
                                epNum: epNum,
                                label: `第${epNum}集`,
                                rawText: text,
                                attrId: attrId,
                                href: href
                            });
                        }

                        results.sort((a, b) => a.epNum - b.epNum);
                        return results;
                    }"""
                )

                # Huangguo series pages advertise the episode count in metadata.
                # Their first episode uses the series URL; later ones use /ep-N.
                parsed_url = urlparse(page.url)
                series_match = re.match(r"^/video/(\d+)(?:/ep-\d+)?/?$", parsed_url.path)
                description = page.locator('meta[name="description"]').get_attribute("content") or ""
                count_match = re.search(r"\u5168\s*(\d+)\s*\u96c6", description)
                if parsed_url.hostname == "huangguoai.com" and series_match and count_match:
                    count = int(count_match.group(1))
                    if 1 < count <= 1000:
                        base = f"{parsed_url.scheme}://{parsed_url.netloc}/video/{series_match.group(1)}/"
                        episode_targets = [
                            {"epNum": n, "label": f"\u7b2c{n}\u96c6", "rawText": f"\u7b2c{n}\u96c6",
                             "attrId": None, "href": base if n == 1 else f"{base}ep-{n}"}
                            for n in range(1, count + 1)
                        ]
                        self.log(f"[+] Found {count} episodes in the series description.")
                        if page.url.rstrip("/") != base.rstrip("/"):
                            current_streams.clear()
                            self.navigate_with_retries(page, base, timeout=25000)
                            page.wait_for_timeout(3000)

                # Prefer actual unlocked episode links from the site's own grid.
                grid_targets = page.evaluate("""() => Array.from(
                    document.querySelectorAll('[data-web-play-ep-grid] a[data-ep-id]:not(.is-locked)[href]')
                ).map(a => ({epNum: Number(a.dataset.epId), label: `Episode ${a.dataset.epId}`,
                    rawText: a.textContent.trim(), attrId: null, href: a.getAttribute('href')}))
                    .filter(e => Number.isInteger(e.epNum) && e.epNum > 0)
                    .sort((a, b) => a.epNum - b.epNum)""")
                if grid_targets:
                    episode_targets = grid_targets
                    self.log(f"[+] Found {len(grid_targets)} unlocked episode links in the page.")

                # Discover streams by selecting each episode instead of assuming API URLs are episodes.

                if not episode_targets:
                    self.log("[!] Only found single video stream on page.")
                    episode_targets = [{"epNum": 1, "label": "Episode 1", "rawText": "Episode 1", "attrId": None, "href": series_url}]
                if retry_only is not None:
                    episode_targets = [ep for ep in episode_targets if ep["epNum"] in retry_only]
                    if not episode_targets:
                        raise RuntimeError("The selected episode numbers are not listed on this page.")

                if episode_targets:
                    labels_str = ", ".join(f"{e['label']} ({e['rawText']})" for e in episode_targets)
                    self.log(f"[+] Detected {len(episode_targets)} episodes: {labels_str}")
                    if len(episode_targets) == 1 and not scan_only:
                        self.log("[!] Only one episode control was found. The site may hide or paginate its episode list; try Show Browser Window and open the list before scanning.")
                        diagnostic = series_dir / "episode-page.html"
                        diagnostic.write_text(page.content(), encoding="utf-8")
                        self.log(f"[*] Saved page HTML for episode detection troubleshooting: {diagnostic}")

                priorities = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}
                stored_priorities = {r["number"]: r["priority"] for r in stored["episodes"]} if stored else {}
                episode_targets.sort(key=lambda ep: (
                    priorities.get(stored_priorities.get(ep["epNum"], "Normal"), 2),
                    ep["epNum"],
                ))

                completed = failed = existing = pending = marked = 0
                total = len(episode_targets)
                episode_durations = page.evaluate("""() => {
                    try {
                        const data = JSON.parse(document.getElementById('videoInitialData')?.textContent || '{}');
                        return Object.fromEntries(Object.entries(data.trackingEpisodes || {})
                            .map(([number, info]) => [number, Number(info.drama_duration || 0)]));
                    } catch (_) { return {}; }
                }""")
                if not preserve_table:
                    self.emit(("episodes", [(ep["epNum"], ep.get("rawText") or ep.get("label"))
                                                     for ep in episode_targets]))
                self.emit(("progress", (0, total)))
                for idx, ep in enumerate(episode_targets, start=1):
                    if self.pause_event.is_set():
                        self.emit(("status", "Paused between episodes"))
                    while self.pause_event.is_set() and not self.cancel_event.is_set():
                        time.sleep(0.2)
                    if self.cancel_event.is_set():
                        self.emit(("status", "Download cancelled"))
                        break
                    self.emit(("status", f"Inspecting episode {idx} of {total}..."))
                    self.emit(("detail", ""))
                    self.emit(("episode_state", (ep["epNum"], "Finding video...")))
                    ep_label = sanitize_filename(ep["label"])
                    self.log(f"\n--- [{idx}/{len(episode_targets)}] Inspecting {ep_label} ---")

                    saved = find_existing_episode(
                        series_dir, ep["epNum"], ep_label,
                        episode_durations.get(str(ep["epNum"])) or 0,
                        registered_paths=registered_episode_paths(stored, ep["epNum"], series_url, conflicts),
                    )
                    if saved:
                        existing += 1
                        self.log(f"[=] Already downloaded: {saved.name}")
                        self.emit(("episode_state", (ep["epNum"], "Already downloaded")))
                        self.emit(("episode_percent", (ep["epNum"], "100%")))
                        self.emit(("saved_file", (ep["epNum"], str(saved))))
                        self.emit(("progress", (idx, total)))
                        continue
                    if retry_only is None and ep["epNum"] in self.marked_episodes.get(series_url, []):
                        marked += 1
                        self.emit(("episode_state", (ep["epNum"], "Marked downloaded")))
                        self.emit(("progress", (idx, total)))
                        continue

                    if scan_only:
                        pending += 1
                        self.emit(("episode_state", (ep["epNum"], "Ready")))
                        self.emit(("episode_percent", (ep["epNum"], "—")))
                        self.emit(("progress", (idx, total)))
                        continue

                    try:
                        page_source = self.resolve_episode_source(
                            page, ep, series_url, current_streams, all_seen_streams)
                    except Exception as exc:
                        if self.cancel_event.is_set():
                            self.emit(("episode_state", (ep["epNum"], "Cancelled")))
                            self.emit(("status", "Download cancelled"))
                            break
                        self.log(f"[-] Could not open {ep_label}: {exc}")
                        self.emit(("episode_state", (ep["epNum"], "Failed: page error")))
                        failed += 1
                        self.emit(("progress", (idx, total)))
                        continue
                    current_page_source = (page_source if page_source and
                                           page_source.get("ep") == ep["epNum"] else None)
                    stream_url, fallback_url, codec = choose_episode_stream(
                        current_page_source, current_streams, all_seen_streams, prefer_hevc,
                    )
                    if not stream_url:
                        self.log(f"[-] No stream found for {ep_label} (may require VIP login). Skipping.")
                        self.emit(("episode_state", (ep["epNum"], "Skipped: video unavailable")))
                        failed += 1
                        self.emit(("progress", (idx, total)))
                        continue

                    if prefer_hevc and current_page_source and codec == "H.264":
                        self.log(f"[=] HEVC is unavailable for {ep_label}; using H.264.")
                    all_seen_streams.add(stream_url)
                    self.emit(("stream_url", (ep["epNum"], stream_url)))

                    out_file = available_output_template(series_dir, ep["epNum"], ep_label)
                    self.log(f"[+] Downloading {codec or 'detected'} stream: {stream_url}")
                    codec_status = f" ({codec})" if codec else ""
                    self.emit(("status", f"Downloading episode {idx} of {total}{codec_status}..."))
                    self.emit(("episode_state", (ep["epNum"], "Downloading...")))
                    try:
                        duration = current_page_source.get("duration") if current_page_source else None
                        try:
                            saved = self.download_with_retries(stream_url, out_file, page.url,
                                                               ep["epNum"], idx, total, duration)
                        except Exception:
                            if (not fallback_url or self.cancel_event.is_set()
                                    or not shutil.which("ffmpeg")
                                    or ".m3u8" not in urlparse(stream_url).path.lower()):
                                raise
                            self.log(f"[!] HEVC download failed for {ep_label}; trying H.264.")
                            self.emit(("status", f"HEVC failed; downloading episode {idx} of {total} with H.264..."))
                            self.emit(("detail", "Trying the compatible H.264 stream..."))
                            self.emit(("episode_state", (ep["epNum"], "Downloading...")))
                            all_seen_streams.add(fallback_url)
                            saved = self.download_with_retries(fallback_url, out_file, page.url,
                                                               ep["epNum"], idx, total, duration)
                        self.log(f"[+] Completed {ep_label}")
                        self.emit(("episode_state", (ep["epNum"], "Done")))
                        self.emit(("saved_file", (ep["epNum"], str(saved))))
                        completed += 1
                    except Exception as exc:
                        if self.cancel_event.is_set():
                            self.emit(("episode_state", (ep["epNum"], "Cancelled")))
                            self.emit(("status", "Download cancelled"))
                            break
                        self.log(f"[-] Failed {ep_label}: {exc}")
                        self.emit(("episode_state", (ep["epNum"], "Failed")))
                        failed += 1
                    self.emit(("progress", (idx, total)))

                browser.close()
            if scan_only:
                summary = (f"Scan complete: {total} episodes · {existing} already downloaded · "
                           f"{pending} ready · {marked} marked downloaded")
                if not shutil.which("ffprobe"):
                    summary += " · FFprobe unavailable; existing videos were not verified"
                if self.cancel_event.is_set():
                    summary = "Scan cancelled. No downloads were started."
            else:
                summary = f"Finished: {completed} new, {existing} already downloaded, {failed} failed or skipped"
                if self.cancel_event.is_set():
                    summary = f"Cancelled: {completed} downloaded before stopping"
            self.log(f"\n{summary}. {'Planned save folder' if scan_only else 'Saved to'}: {series_dir}")
            self.emit(("status", summary))
            self.emit(("detail", str(series_dir)))
            self.emit(("history", {
                "url": series_url,
                "title": series_title,
                "folder": str(series_dir),
                "date": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
                "summary": summary,
            }))
            result_summary = summary
        except Exception as exc:
            if self.cancel_event.is_set():
                result_summary = "Scan cancelled. No downloads were started." if scan_only else "Cancelled: no further episodes downloaded"
            else:
                self.log(f"[!] Error: {exc}")
                result_summary = f"Stopped: {exc}"
            self.emit(("status", result_summary))
        finally:
            if finalize:
                self.emit(("scan_done" if scan_only else "done", result_summary))
        return result_summary


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    app = SeriesDownloaderApp(root)
    root.mainloop()
