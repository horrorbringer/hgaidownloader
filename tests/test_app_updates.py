"""Real HTTP stand-ins for GitHub metadata, checksum, and asset downloads.

Only the transport maps the validated production HTTPS addresses to localhost.
TLS and the live publication path are outside these tests' scope.
"""

import hashlib
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app_updates import (GitHubRedirects, ReleaseClient, UpdateCancelled, UpdateError,
                         checksum_for, download_update, launch_installer, select_update, trusted_url)
from app_version import UPDATE_REPOSITORY

VERSION = "0.1.0-beta.2"
FILENAME = f"SeriesDownloader-{VERSION}-windows-x64-setup.exe"
PREFIX = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/v{VERSION}/"
API_PATH = f"/repos/{UPDATE_REPOSITORY}/releases?per_page=20"
PAYLOAD = b"MZ" + bytes(range(256)) * 2048
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


def release(version=VERSION, *, prerelease=True, draft=False):
    name = f"SeriesDownloader-{version}-windows-x64-setup.exe"
    prefix = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/v{version}/"
    return {"tag_name": "v" + version, "prerelease": prerelease, "draft": draft, "assets": [
        {"name": name, "state": "uploaded", "size": len(PAYLOAD),
         "digest": "sha256:" + SHA256, "browser_download_url": prefix + name},
        {"name": "SHA256SUMS.txt", "state": "uploaded", "size": 200,
         "browser_download_url": prefix + "SHA256SUMS.txt"}]}


class ReleaseSelectionTests(unittest.TestCase):
    def test_empty_equal_and_older_releases_do_not_offer_an_update(self):
        for entries in ([], [release()], [release("0.1.0-beta.1")]):
            self.assertIsNone(select_update(entries, VERSION))

    def test_beta_channels_and_stable_transition(self):
        self.assertEqual(select_update([release()], "0.1.0-beta.1").version, VERSION)
        self.assertIsNone(select_update([release("0.2.0-beta.1")], "0.1.0"))
        self.assertEqual(select_update([release("0.1.0", prerelease=False), release()], "0.1.0-beta.1").version, "0.1.0")

    def test_draft_missing_checksum_and_foreign_asset_are_ineligible(self):
        draft = release(draft=True)
        incomplete = release()
        incomplete["assets"].pop()
        foreign = release()
        foreign["assets"][0]["browser_download_url"] = "https://github.com/another/repo/installer.exe"
        traversal = release("../evil")
        self.assertIsNone(select_update([draft, incomplete, foreign, traversal], "0.1.0-beta.1"))

    def test_version_comparison_is_numeric(self):
        versions = [release("0.2.0", prerelease=False), release("0.10.0", prerelease=False)]
        self.assertEqual(select_update(versions, "0.1.0").version, "0.10.0")

    def test_duplicate_or_absent_checksums_are_rejected(self):
        entry = f"{SHA256}  {FILENAME}\n"
        for payload in (b"", (entry + entry).encode(), f"{SHA256}  other.exe\n".encode()):
            with self.assertRaises(UpdateError):
                checksum_for(payload, FILENAME)

    def test_redirects_cannot_downgrade_https_or_leave_github(self):
        bad = ("http://github.com/file", "https://github.com.evil.invalid/file", "https://example.com/file",
               "https://user:password@github.com/file", "https://github.com:invalid/file")
        for url in bad:
            self.assertFalse(trusted_url(url))
            with self.assertRaises(UpdateError):
                GitHubRedirects().redirect_request(Request(PREFIX + FILENAME), None, 302, "Found", {}, url)


class UpdateHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.requests.append((self.path, dict(self.headers)))
                status, body, delay = self.server.routes.get(self.path, (501, b"unmodelled route", 0))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *_args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.server.requests = []
        self.server.routes = {
            API_PATH: (200, json.dumps([release()]).encode(), 0),
            urlsplit(PREFIX + "SHA256SUMS.txt").path: (200, f"{SHA256}  {FILENAME}\n".encode(), 0),
            urlsplit(PREFIX + FILENAME).path: (200, PAYLOAD, 0),
        }

        def transport(request, *, timeout):
            parsed = urlsplit(request.full_url)
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            local = Request(f"http://127.0.0.1:{self.server.server_port}" + path, headers=dict(request.headers))
            return urlopen(local, timeout=timeout)

        self.client = ReleaseClient(open_request=transport, timeout=2)
        self.cancelled = threading.Event()

    def test_complete_download_uses_real_http_and_never_executes(self):
        with patch("app_updates.subprocess.Popen") as execute:
            update = self.client.check("0.1.0-beta.1")
            artifact = download_update(self.client, update, self.directory, self.cancelled)
        execute.assert_not_called()
        self.assertEqual(artifact.path.read_bytes(), PAYLOAD)
        self.assertEqual(artifact.sha256, SHA256)
        self.assertEqual(len(self.server.requests), 3)
        self.assertTrue(self.server.requests[0][1]["User-Agent"].startswith("SeriesDownloader/"))
        self.assertFalse(list(self.directory.glob("*.part")))

    def test_absent_releases_are_not_a_failure(self):
        self.server.routes[API_PATH] = (200, b"[]", 0)
        self.assertIsNone(self.client.check("0.1.0-beta.1"))

    def test_rate_limit_server_error_and_invalid_json(self):
        for status, body in ((403, b"limited"), (429, b"limited"), (500, b"<html>error</html>"), (200, b"invalid")):
            self.server.routes[API_PATH] = (status, body, 0)
            with self.assertRaises(UpdateError):
                self.client.check("0.1.0-beta.1")

    def test_timeout_leaves_no_installer(self):
        self.client.timeout = .05
        self.server.routes[API_PATH] = (200, b"[]", .3)
        with self.assertRaises(UpdateError):
            self.client.check("0.1.0-beta.1")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cancel_after_first_chunk_removes_partial_download(self):
        update = self.client.check("0.1.0-beta.1")
        with self.assertRaises(UpdateCancelled):
            download_update(self.client, update, self.directory, self.cancelled,
                            lambda *_: self.cancelled.set())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_wrong_checksum_and_truncated_download_never_publish_an_executable(self):
        update = self.client.check("0.1.0-beta.1")
        for payload in (PAYLOAD[:-3], b"wrong content" * 100):
            self.server.routes[urlsplit(update.installer_url).path] = (200, payload, 0)
            with self.assertRaises(UpdateError):
                download_update(self.client, update, self.directory, self.cancelled)
            self.assertEqual(list(self.directory.iterdir()), [])
        self.server.routes[urlsplit(update.installer_url).path] = (200, b"MZ" + b"X" * (len(PAYLOAD) - 2), 0)
        with self.assertRaises(UpdateError):
            download_update(self.client, update, self.directory, self.cancelled)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_conflicting_github_digest_prevents_asset_download(self):
        update = replace(self.client.check("0.1.0-beta.1"), asset_digest="0" * 64)
        with self.assertRaises(UpdateError):
            download_update(self.client, update, self.directory, self.cancelled)
        self.assertNotIn(urlsplit(update.installer_url).path, [path for path, _ in self.server.requests])

    @unittest.skipUnless(os.name == "nt", "Windows installer handoff")
    def test_changed_installer_is_not_executed(self):
        update = self.client.check("0.1.0-beta.1")
        artifact = download_update(self.client, update, self.directory, self.cancelled)
        artifact.path.write_bytes(b"MZtampered")
        with patch("app_updates.subprocess.Popen") as execute:
            with self.assertRaises(UpdateError):
                launch_installer(artifact)
        execute.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows installer handoff")
    def test_verified_handoff_preserves_spaces_and_uses_no_shell(self):
        update = self.client.check("0.1.0-beta.1")
        artifact = download_update(self.client, update, self.directory, self.cancelled)
        with patch("app_updates.subprocess.Popen") as execute:
            launch_installer(artifact, install_dir=self.directory / "Installed app")
        arguments = execute.call_args.args[0]
        self.assertIn("/UPDATE=1", arguments)
        self.assertIn(f"/DIR={self.directory / 'Installed app'}", arguments)
        self.assertFalse(execute.call_args.kwargs["shell"])


if __name__ == "__main__":
    unittest.main()
