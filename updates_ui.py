"""Tk update flow. All widgets and installer handoff stay on the UI thread."""

import logging
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

from app_runtime import user_data_dir
from app_version import APP_VERSION
from app_updates import (ReleaseClient, UpdateCancelled, download_update,
                         launch_installer, registered_install_dir)


class UpdateController:
    def __init__(self, root, *, is_busy, before_install, close_app, client=None):
        self.root = root
        self.is_busy = is_busy
        self.before_install = before_install
        self.close_app = close_app
        self.client = client or ReleaseClient()
        self.events = queue.Queue()
        self.cancelled = threading.Event()
        self.closed = False
        self.working = False
        self.downloading = False
        self.update = None
        self.installer = None
        self.window = None
        self.button = None
        self.status = "Check for a newer Windows version."
        self.percent = 0
        self.root.bind("<Destroy>", self._destroyed, add="+")
        self.root.after(100, self._poll)

    def _destroyed(self, event):
        if event.widget is self.root:
            self.closed = True
            self.cancelled.set()

    def show(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            return
        self.window = tk.Toplevel(self.root)
        self.window.title("Application updates")
        self.window.geometry("560x280")
        self.window.resizable(False, False)
        self.window.transient(self.root)
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="Series Downloader updates", font=("Segoe UI", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(frame, text=f"Installed version: {APP_VERSION}").pack(anchor=tk.W, pady=(8, 0))
        self.status_var = tk.StringVar(value=self.status)
        ttk.Label(frame, textvariable=self.status_var, wraplength=510).pack(anchor=tk.W, pady=12)
        self.progress = ttk.Progressbar(frame, maximum=100, value=self.percent)
        self.progress.pack(fill=tk.X)
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(16, 8))
        self.check_button = ttk.Button(actions, text="Check again", command=self.check)
        self.check_button.pack(side=tk.LEFT)
        self.download_button = ttk.Button(actions, text="Download update", command=self.download)
        self.download_button.pack(side=tk.LEFT, padx=6)
        self.install_button = ttk.Button(actions, text="Install and restart", command=self.install)
        self.install_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(frame, text="Cancel download", command=self.cancelled.set)
        self.cancel_button.pack(side=tk.LEFT)
        ttk.Button(frame, text="Release notes", command=self.release_notes).pack(side=tk.RIGHT)
        self._refresh()
        if self.update is None and not self.working:
            self.check()

    def _refresh(self):
        if self.button is not None:
            self.button.configure(text="Update available" if self.update else "Updates")
        if self.window is None or not self.window.winfo_exists():
            return
        self.status_var.set(self.status)
        self.progress.configure(value=self.percent)
        self.check_button.configure(state="disabled" if self.working else "normal")
        self.download_button.configure(state="normal" if self.update and not self.working and not self.installer else "disabled")
        self.install_button.configure(state="normal" if self.installer and not self.working else "disabled")
        self.cancel_button.configure(state="normal" if self.downloading else "disabled")

    def _start(self, action, *, download=False):
        self.working = True
        self.downloading = download
        self._refresh()
        events = self.events

        def work():
            try:
                kind, value = action()
            except UpdateCancelled as error:
                events.put(("cancelled", str(error)))
            except Exception as error:
                logging.exception("Update operation failed")
                events.put(("error", str(error)))
            else:
                events.put((kind, value))

        threading.Thread(target=work, daemon=True).start()

    def check(self):
        if self.working or self.closed:
            return
        self.status = "Checking GitHub for updates…"
        client = self.client
        self._start(lambda: ("checked", client.check()))

    def download(self):
        if self.working or self.update is None or self.closed:
            return
        self.cancelled.clear()
        self.percent = 0
        self.status = f"Downloading version {self.update.version}…"
        last_progress = 0
        events = self.events

        def progress(done, total):
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress > .15 or done == total:
                last_progress = now
                events.put(("progress", (done, total)))

        update = self.update
        client, cancelled = self.client, self.cancelled
        directory = user_data_dir() / "updates"
        self._start(lambda: ("downloaded", download_update(client, update,
                    directory, cancelled, progress)), download=True)

    def install(self):
        if self.installer is None or self.working or self.closed:
            return
        if self.is_busy():
            self.status = "Finish or cancel the current download or cleanup before installing."
            self._refresh()
            return
        self.status = "Verifying the installer before closing the app…"
        self._refresh()
        self.root.update_idletasks()
        try:
            self.before_install()
            launch_installer(self.installer, install_dir=registered_install_dir())
        except Exception as error:
            logging.exception("Could not launch the update installer")
            self.status = f"Update was not installed: {error}"
            self._refresh()
            return
        self.close_app()

    def release_notes(self):
        if self.update:
            webbrowser.open(self.update.release_url)

    def _poll(self):
        if self.closed:
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                done, total = value
                self.percent = done / total * 100
                self.status = f"Downloading update: {self.percent:.0f}% ({done / 1048576:.1f} of {total / 1048576:.1f} MiB)"
            else:
                self.working = False
                self.downloading = False
                if kind == "checked":
                    self.update = value
                    self.installer = None
                    self.status = (f"Version {value.version} is available. Download it, then choose Install and restart."
                                   if value else "No newer published Windows installer is available.")
                elif kind == "downloaded":
                    self.installer = value
                    self.percent = 100
                    self.status = f"Version {value.version} downloaded and SHA-256 verified. Ready to install."
                else:
                    self.status = value
            self._refresh()
        self.root.after(100, self._poll)
