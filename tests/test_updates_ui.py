import gc
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app_updates import VerifiedInstaller
from updates_ui import UpdateController


class UpdateUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Desktop display unavailable")
        self.root.withdraw()
        self.root.update_idletasks()
        self.addCleanup(self.destroy_root)
        self.busy = False
        self.save = Mock()
        self.close = Mock()
        self.client = Mock()
        self.controller = UpdateController(self.root, is_busy=lambda: self.busy,
                           before_install=self.save, close_app=self.close, client=self.client)

    def destroy_root(self):
        self.controller.closed = True
        self.controller.cancelled.set()
        for job in self.root.tk.call("after", "info"):
            self.root.after_cancel(job)
        self.root.destroy()
        self.controller = None
        self.root = None
        gc.collect()

    def test_update_check_runs_off_the_ui_thread_and_reports_no_release(self):
        called_on = []
        self.client.check.side_effect = lambda: called_on.append(threading.get_ident()) or None
        self.controller.check()
        deadline = time.monotonic() + 3
        while self.controller.working and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.controller.working)
        self.assertNotEqual(called_on, [threading.get_ident()])
        self.assertIn("No newer published", self.controller.status)

    def test_installer_handoff_is_blocked_during_downloads(self):
        self.controller.installer = VerifiedInstaller(Path("unused.exe"), "0" * 64, 2, "0.1.0")
        self.busy = True
        with patch("updates_ui.launch_installer") as execute, patch("updates_ui.logging.exception"):
            self.controller.install()
        execute.assert_not_called()
        self.save.assert_not_called()
        self.close.assert_not_called()
        self.assertIn("Finish or cancel", self.controller.status)

    def test_save_failure_keeps_the_app_open_and_does_not_install(self):
        self.controller.installer = VerifiedInstaller(Path("unused.exe"), "0" * 64, 2, "0.1.0")
        self.save.side_effect = OSError("disk full")
        with patch("updates_ui.launch_installer") as execute, patch("updates_ui.logging.exception"):
            self.controller.install()
        execute.assert_not_called()
        self.close.assert_not_called()
        self.assertIn("disk full", self.controller.status)

    def test_verified_handoff_saves_before_launch_and_closes_afterwards(self):
        self.controller.installer = VerifiedInstaller(Path("unused.exe"), "0" * 64, 2, "0.1.0")
        steps = []
        self.save.side_effect = lambda: steps.append("save")
        self.close.side_effect = lambda: steps.append("close")
        with patch("updates_ui.registered_install_dir", return_value=None), \
             patch("updates_ui.launch_installer", side_effect=lambda *_args, **_kwargs: steps.append("launch")):
            self.controller.install()
        self.assertEqual(steps, ["save", "launch", "close"])


if __name__ == "__main__":
    unittest.main()
