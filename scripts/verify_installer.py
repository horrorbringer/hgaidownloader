"""Exercise a built installer in a temporary directory on the current Windows PC."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import winreg
from contextlib import closing
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from app_version import APP_VERSION

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{A6C49B58-44DF-41DB-8FBA-6891C6657364}_is1"


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*command, env=None):
    result = subprocess.run(command, env=env, timeout=240)
    if result.returncode:
        raise RuntimeError(f"Exit {result.returncode}: {command[0]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path)
    args = parser.parse_args()
    # Never replace a user's actual registered installation while testing.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY):
            raise RuntimeError("Series Downloader is already installed for this user; use a clean test account")
    except FileNotFoundError:
        pass
    release = PROJECT / "release" / APP_VERSION
    installer = (args.installer or release / f"SeriesDownloader-{APP_VERSION}-windows-x64-setup.exe").resolve()
    if not installer.is_file():
        raise FileNotFoundError(installer)
    workspace = Path(tempfile.mkdtemp(prefix="installer-check-", dir=PROJECT / "build"))
    application = workspace / "app"
    profile = workspace / "profile"
    profile.mkdir()
    settings = profile / "series_gui_settings.json"
    settings.write_text('{"history": [], "release_test": "preserve settings"}', encoding="utf-8")
    library = profile / "series_library.sqlite3"
    with closing(sqlite3.connect(library)) as database:
        database.execute("CREATE TABLE preservation_check (value TEXT)")
        database.execute("INSERT INTO preservation_check VALUES ('preserve library')")
        database.commit()
    expected = {path.name: fingerprint(path) for path in (settings, library)}
    report = {"version": APP_VERSION, "machine_test_only": True, "passed": False, "checks": []}
    executable = application / "SeriesDownloader.exe"
    installed = False
    try:
        run(str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=",
            f"/DIR={application}", f"/LOG={workspace / 'install.log'}")
        installed = True
        if not executable.is_file():
            raise RuntimeError("Installed executable is missing")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            if Path(location).resolve() != application.resolve():
                raise RuntimeError("The uninstall registration points to a different folder")
        report["checks"].append("per-user install and uninstall registration")
        environment = os.environ.copy()
        environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
        environment["SERIES_DOWNLOADER_DATA_DIR"] = str(profile)
        environment["PLAYWRIGHT_BROWSERS_PATH"] = str(workspace / "missing-browser")
        smoke = workspace / "installed-smoke.json"
        run(str(executable), "--self-test", "--report", str(smoke), env=environment)
        if not json.loads(smoke.read_text())["passed"]:
            raise RuntimeError("Installed application's self-test failed")
        report["checks"].append("installed app browser, GUI, recycle, media, and worker diagnostics")
        run(str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=",
            f"/DIR={application}", f"/LOG={workspace / 'upgrade.log'}")
        for name, checksum in expected.items():
            if fingerprint(profile / name) != checksum:
                raise RuntimeError(f"Upgrade changed the saved profile: {name}")
        report["checks"].append("reinstall/upgrade preserves saved settings and library")
    finally:
        if installed:
            run(str(application / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                f"/LOG={workspace / 'uninstall.log'}")
            deadline = time.monotonic() + 30
            while executable.exists() and time.monotonic() < deadline:
                time.sleep(.2)
    if executable.exists():
        raise RuntimeError("Uninstall left the application executable in place")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY):
            raise RuntimeError("Uninstall registration was not removed")
    except FileNotFoundError:
        pass
    for name, checksum in expected.items():
        if fingerprint(profile / name) != checksum:
            raise RuntimeError(f"Uninstall changed the saved profile: {name}")
    report["checks"].append("uninstall removes app/registration and preserves the saved profile")
    report["passed"] = True
    destination = release / "INSTALLER_TEST.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Installer lifecycle checks passed. Report: {destination}")
    print(f"Detailed logs retained at: {workspace}")


if __name__ == "__main__":
    main()
