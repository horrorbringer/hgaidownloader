"""Public GitHub release discovery and checksum-verified Windows updates.

No credentials are bundled. Downloading never executes an installer. Execution
requires a separate caller action and a fresh SHA-256 check of the entire file.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from packaging.version import Version
from app_version import APP_VERSION, UPDATE_REPOSITORY

MAX_INSTALLER_BYTES = 2_000_000_000
VERSION_PATTERN = re.compile(r"v?(\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?)\Z")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A6C49B58-44DF-41DB-8FBA-6891C6657364}_is1"


class UpdateError(Exception):
    pass


class UpdateCancelled(UpdateError):
    pass


def trusted_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and parsed.hostname in {
                    "api.github.com", "github.com", "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com"})
    except (TypeError, ValueError):
        return False


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if not trusted_url(new_url):
            raise UpdateError("The update server redirected outside trusted GitHub download hosts.")
        return super().redirect_request(request, response, code, message, headers, new_url)


@dataclass(frozen=True)
class ReleaseUpdate:
    version: str
    installer_name: str
    installer_url: str
    checksum_url: str
    release_url: str
    size: int
    asset_digest: str = ""


@dataclass(frozen=True)
class VerifiedInstaller:
    path: Path
    sha256: str
    size: int
    version: str


class ReleaseClient:
    def __init__(self, *, open_request=None, timeout=15):
        self._open = open_request or build_opener(GitHubRedirects()).open
        self.timeout = timeout

    def open(self, url):
        if not trusted_url(url):
            raise UpdateError("The update address is not a trusted HTTPS GitHub address.")
        request = Request(url, headers={"User-Agent": f"SeriesDownloader/{APP_VERSION}",
                          "Accept": "application/vnd.github+json" if urlsplit(url).hostname == "api.github.com"
                          else "application/octet-stream", "X-GitHub-Api-Version": "2026-03-10"})
        try:
            return self._open(request, timeout=self.timeout)
        except HTTPError as error:
            if error.code in (403, 429):
                raise UpdateError("GitHub is limiting update checks. Please try again later.") from error
            raise UpdateError(f"The update server is unavailable (HTTP {error.code}).") from error
        except (URLError, OSError, TimeoutError) as error:
            raise UpdateError("Could not reach GitHub. Check your connection and try again.") from error

    def read(self, url, limit):
        try:
            with self.open(url) as response:
                payload = response.read(limit + 1)
        except (OSError, URLError, TimeoutError) as error:
            raise UpdateError("The update server stopped responding. Please try again.") from error
        if len(payload) > limit:
            raise UpdateError("The update server returned an unexpectedly large response.")
        return payload

    def check(self, current_version=APP_VERSION):
        try:
            payload = json.loads(self.read(f"https://api.github.com/repos/{UPDATE_REPOSITORY}/releases?per_page=20", 4_000_000))
        except (ValueError, UnicodeError) as error:
            raise UpdateError("GitHub returned invalid release information.") from error
        return select_update(payload, current_version)


def select_update(releases, current_version):
    if not isinstance(releases, list):
        raise UpdateError("GitHub returned an unexpected release list.")
    current = Version(current_version)
    choices = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        tag = release.get("tag_name", "")
        match = VERSION_PATTERN.fullmatch(tag) if isinstance(tag, str) else None
        if not match:
            continue
        version = match[1]
        parsed = Version(version)
        if parsed <= current or ((parsed.is_prerelease or release.get("prerelease")) and not current.is_prerelease):
            continue
        name = f"SeriesDownloader-{version}-windows-x64-setup.exe"
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            continue
        installers = [a for a in assets if isinstance(a, dict) and a.get("name") == name and a.get("state", "uploaded") == "uploaded"]
        checksums = [a for a in assets if isinstance(a, dict) and a.get("name") == "SHA256SUMS.txt" and a.get("state", "uploaded") == "uploaded"]
        # Releases with assets still uploading are not eligible for installation.
        if len(installers) != 1 or len(checksums) != 1:
            continue
        installer, checksum = installers[0], checksums[0]
        size = installer.get("size")
        if type(size) is not int or not 0 < size <= MAX_INSTALLER_BYTES:
            continue
        prefix = f"https://github.com/{UPDATE_REPOSITORY}/releases/download/{quote(tag, safe='')}/"
        expected_installer = prefix + quote(name, safe="")
        expected_checksum = prefix + "SHA256SUMS.txt"
        if installer.get("browser_download_url") != expected_installer or checksum.get("browser_download_url") != expected_checksum:
            continue
        digest = installer.get("digest") or ""
        if digest and (not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)):
            continue
        choices.append(ReleaseUpdate(version, name, expected_installer, expected_checksum,
                                    f"https://github.com/{UPDATE_REPOSITORY}/releases/tag/{quote(tag, safe='')}",
                                    size, digest.removeprefix("sha256:").lower()))
    return max(choices, key=lambda item: Version(item.version), default=None)


def checksum_for(payload, filename):
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeError as error:
        raise UpdateError("The release checksum file is invalid.") from error
    matches = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if match and match[2] == filename:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise UpdateError("The release has no unique checksum for its installer.")
    return matches[0]


def download_update(client, update, directory, cancelled, progress=lambda *_: None):
    expected = checksum_for(client.read(update.checksum_url, 256_000), update.installer_name)
    if update.asset_digest and expected != update.asset_digest:
        raise UpdateError("GitHub's asset checksum does not match the release checksum file.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or directory.is_junction():
        raise UpdateError("The update cache cannot be a symbolic link or junction.")
    handle, name = tempfile.mkstemp(prefix="installer-", suffix=".part", dir=directory)
    partial = Path(name)
    target = partial.with_suffix(".exe")
    size = 0
    digest = hashlib.sha256()
    started = time.monotonic()
    try:
        with os.fdopen(handle, "wb") as output, client.open(update.installer_url) as response:
            while True:
                if cancelled.is_set():
                    raise UpdateCancelled("Update download cancelled.")
                if time.monotonic() - started > 1800:
                    raise UpdateError("The update download took too long. Please try again.")
                block = response.read(256 * 1024)
                if not block:
                    break
                size += len(block)
                if size > update.size:
                    raise UpdateError("The downloaded installer exceeds its published size.")
                output.write(block)
                digest.update(block)
                progress(size, update.size)
        if cancelled.is_set():
            raise UpdateCancelled("Update download cancelled.")
        if size != update.size or digest.hexdigest() != expected:
            raise UpdateError("The installer failed its size or SHA-256 check. It was not installed.")
        with partial.open("rb") as source:
            if source.read(2) != b"MZ":
                raise UpdateError("The downloaded file is not a Windows executable.")
        # Unique names avoid reusing or overwriting another process's update file.
        partial.rename(target)
        return VerifiedInstaller(target, expected, size, update.version)
    except (OSError, URLError, TimeoutError) as error:
        raise UpdateError("The update download was interrupted. Please try again.") from error
    finally:
        partial.unlink(missing_ok=True)


def registered_install_dir():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            directory = Path(winreg.QueryValueEx(key, "InstallLocation")[0])
        return directory if directory.resolve() == Path(sys.executable).resolve().parent else None
    except (OSError, ValueError):
        return None


def launch_installer(installer, *, install_dir=None):
    if sys.platform != "win32":
        raise UpdateError("These installers support Windows only.")
    path = Path(installer.path)
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise UpdateError("The verified installer is no longer available.")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if path.stat().st_size != installer.size or digest != installer.sha256:
        raise UpdateError("The installer changed after download. Download it again before installing.")
    arguments = [str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"]
    if install_dir is not None:
        arguments.append(f"/DIR={install_dir}")
    return subprocess.Popen(arguments, shell=False, close_fds=True)
