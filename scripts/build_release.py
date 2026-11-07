"""Build and verify the Windows x64 beta. Run inside requirements-build.txt's venv."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from app_version import APP_NAME, APP_VERSION, FILE_VERSION

COMPILER_URL = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe"
COMPILER_SHA256 = "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0"


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(command, *, env=None, log=None, timeout=1200):
    print("Running:", subprocess.list2cmdline([str(part) for part in command]), flush=True)
    if log:
        with Path(log).open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=PROJECT, env=env, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            print(Path(log).read_text(encoding="utf-8", errors="replace")[-10000:])
            raise RuntimeError(f"Command failed ({result.returncode}); see {log}")
    else:
        subprocess.run(command, cwd=PROJECT, env=env, check=True, timeout=timeout)


def compiler(path, build):
    if path:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved
    tools = build / "tools"
    installed = tools / "innosetup-7.1.1" / "ISCC.exe"
    if not installed.is_file():
        tools.mkdir(parents=True, exist_ok=True)
        setup = tools / "innosetup-7.1.1-x64.exe"
        if not setup.is_file() or digest(setup) != COMPILER_SHA256:
            print("Downloading the pinned Inno Setup compiler...", flush=True)
            with urllib.request.urlopen(COMPILER_URL, timeout=60) as source, setup.open("wb") as target:
                shutil.copyfileobj(source, target)
        if digest(setup) != COMPILER_SHA256:
            raise RuntimeError("Inno Setup download checksum mismatch")
        run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
             "/NOICONS", f"/DIR={installed.parent}", f"/LOG={tools / 'inno-install.log'}"], timeout=180)
    if not installed.is_file():
        raise FileNotFoundError("Inno Setup did not install its compiler")
    return installed


def collect_licenses(destination):
    destination.mkdir(parents=True, exist_ok=True)
    distributions = ("playwright", "yt-dlp", "Send2Trash", "pywin32", "greenlet",
                     "pyee", "typing_extensions", "pyinstaller")
    for name in distributions:
        package = importlib.metadata.distribution(name)
        for entry in package.files or []:
            if any(word in entry.name.lower() for word in ("license", "copying", "notice")):
                source = Path(package.locate_file(entry))
                if source.is_file():
                    target = destination / name / Path(*[part for part in entry.parts if part not in ("..", ".")])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError("Python's license file is required")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
    for name in ("tcl8.6", "tk8.6"):
        source = Path(sys.base_prefix) / "tcl" / name / "license.terms"
        target = destination / f"{name}-license.terms"
        if source.is_file():
            shutil.copy2(source, target)
        else:
            # Some official Python installers omit Tcl's standalone license.
            # Retrieve the notice from the exact runtime's upstream release tag.
            import tkinter
            interpreter = tkinter.Tcl()
            version = str(interpreter.call("info", "patchlevel"))
            tag = "core-" + version.replace(".", "-")
            url = f"https://raw.githubusercontent.com/tcltk/tcl/{tag}/license.terms"
            with urllib.request.urlopen(url, timeout=30) as response:
                target.write_bytes(response.read())


def write_metadata(staging):
    image = (PROJECT / "icons" / "download.png").read_bytes()
    width, height = struct.unpack(">II", image[16:24])
    entry = struct.pack("<BBBBHHII", width if width < 256 else 0,
                        height if height < 256 else 0, 0, 0, 1, 32, len(image), 22)
    (staging / "application.ico").write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + image)
    strings = {"FileDescription": APP_NAME, "FileVersion": ".".join(map(str, FILE_VERSION)),
               "ProductName": APP_NAME, "ProductVersion": APP_VERSION,
               "OriginalFilename": "SeriesDownloader.exe", "InternalName": "SeriesDownloader"}
    entries = ",\n".join(f"StringStruct({key!r}, {value!r})" for key, value in strings.items())
    (staging / "version-info.txt").write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={FILE_VERSION!r}, prodvers={FILE_VERSION!r}, "
        "mask=0x3f, flags=0x2, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)), "
        f"kids=[StringFileInfo([StringTable('040904B0', [{entries}])]), "
        "VarFileInfo([VarStruct('Translation', [1033, 1200])])])", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg-root", type=Path, help="Vendor folder with bin/ffmpeg.exe, bin/ffprobe.exe, LICENSE, README.txt")
    parser.add_argument("--iscc", help="Use an already-installed Inno Setup compiler")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3, 12):
        parser.error("Build with 64-bit Python 3.12 on Windows")
    build = PROJECT / "build"
    build.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="release-staging-", dir=build))
    release = PROJECT / "release" / APP_VERSION
    release.mkdir(parents=True, exist_ok=True)
    iscc = compiler(args.iscc, build)
    environment = os.environ.copy()
    environment["SERIES_DOWNLOADER_DATA_DIR"] = str(build / "test-profile")
    test_files = (list(PROJECT.glob("*.py")) + list((PROJECT / "tests").glob("*.py"))
                  + list((PROJECT / "icons").glob("*"))
                  + [PROJECT / "requirements-build.txt", PROJECT / "requirements-release.txt"])
    packages = {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}
    test_fingerprint = hashlib.sha256((sys.version + json.dumps(packages, sort_keys=True) + "".join(
        str(path.relative_to(PROJECT)) + digest(path) for path in sorted(test_files))).encode()).hexdigest()
    test_log = build / "unit-tests.log"
    test_record = build / "regression-check.json"
    previous = json.loads(test_record.read_text()) if test_record.exists() else {}
    if (previous.get("inputs") == test_fingerprint and test_log.is_file()
            and previous.get("log_sha256") == digest(test_log)):
        print("Reusing the passing regression result for unchanged source/dependencies.", flush=True)
    else:
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            env=environment, log=test_log, timeout=300)
        test_record.write_text(json.dumps({"inputs": test_fingerprint, "log_sha256": digest(test_log)}))

    media = args.ffmpeg_root
    if not media:
        located = shutil.which("ffmpeg")
        if not located:
            parser.error("Install FFmpeg or supply --ffmpeg-root")
        media = Path(located).resolve().parent.parent
    media = media.resolve()
    media_target = staging / "media-tools"
    for relative in ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt"):
        source = media / relative
        if not source.is_file():
            raise FileNotFoundError(f"Required vendor file: {source}")
        target = media_target / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    ffmpeg_info = subprocess.check_output([str(media / "bin/ffmpeg.exe"), "-version"], text=True)
    if "--enable-nonfree" in ffmpeg_info:
        raise RuntimeError("Non-redistributable FFmpeg builds cannot be bundled")

    # Install only Chromium and its support files, then copy exactly the pinned
    # revisions. This reuses Playwright's verified cache when already present.
    browser_environment = os.environ.copy()
    browser_environment.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    run([sys.executable, "-m", "playwright", "install", "chromium"], env=browser_environment,
        log=build / "browser-install.log", timeout=600)
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    import playwright
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright_instance:
        cache = Path(playwright_instance.chromium.executable_path).parents[2]
    definitions = json.loads((Path(playwright.__file__).parent / "driver/package/browsers.json").read_text())
    for browser in definitions["browsers"]:
        if browser["name"] in ("chromium", "chromium-headless-shell", "ffmpeg", "winldd"):
            directory = browser["name"].replace("-", "_") + "-" + browser["revision"]
            shutil.copytree(cache / directory, staging / "browsers" / directory)
    collect_licenses(staging / "licenses")
    write_metadata(staging)
    environment["SERIES_BUILD_STAGING"] = str(staging)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(PROJECT / "dist"),
         "--workpath", str(build / "pyinstaller"), str(PROJECT / "packaging/SeriesDownloader.spec")],
        env=environment, log=build / "pyinstaller.log")
    bundle = PROJECT / "dist" / "SeriesDownloader"
    for name in ("THIRD_PARTY_NOTICES.md", "CHANGELOG.md"):
        shutil.copy2(PROJECT / name, bundle / name)
    shutil.copy2(PROJECT / "packaging/QUICK_START.txt", bundle / "QUICK_START.txt")
    info = {"application": APP_NAME, "version": APP_VERSION,
            "built_at_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
            "platform": platform.platform(), "packages": packages, "ffmpeg": ffmpeg_info,
            "ffmpeg_sha256": digest(media / "bin/ffmpeg.exe"),
            "ffprobe_sha256": digest(media / "bin/ffprobe.exe"), "signed": False}
    (bundle / "BUILD_INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    for file in bundle.rglob("*"):
        if file.name in ("series_gui_settings.json", "series_library.sqlite3", ".git"):
            raise RuntimeError(f"Personal or repository data leaked into bundle: {file}")

    # Verify the frozen app without system Python, media tools, or browser cache.
    smoke_environment = os.environ.copy()
    smoke_environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    smoke_environment["PLAYWRIGHT_BROWSERS_PATH"] = str(build / "nonexistent-browser-cache")
    smoke_environment["SERIES_DOWNLOADER_DATA_DIR"] = str(build / "frozen-test-profile")
    smoke_report = build / "frozen-smoke-test.json"
    run([str(bundle / "SeriesDownloader.exe"), "--self-test", "--report", str(smoke_report)],
        env=smoke_environment, log=build / "frozen-smoke-test.log", timeout=180)
    if not json.loads(smoke_report.read_text())["passed"]:
        raise RuntimeError(f"Frozen self-test failed; see {smoke_report}")
    published_report = json.loads(smoke_report.read_text())
    published_report["resource_directory"] = "SeriesDownloader/_internal"
    (release / "SMOKE_TEST.json").write_text(json.dumps(published_report, indent=2), encoding="utf-8")

    run([str(iscc), f"/DAppVersion={APP_VERSION}", f"/DNumericVersion={'.'.join(map(str, FILE_VERSION))}",
         f"/DBundleDir={bundle}", f"/DReleaseDir={release}", f"/DBuildIcon={staging / 'application.ico'}",
         str(PROJECT / "packaging/installer.iss")], log=build / "installer-build.log", timeout=600)
    zip_path = release / f"SeriesDownloader-{APP_VERSION}-windows-x64.zip"
    print("Creating the ZIP distribution...", flush=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(bundle.rglob("*")):
            if file.is_file():
                archive.write(file, Path("SeriesDownloader") / file.relative_to(bundle))
    for name in ("CHANGELOG.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(PROJECT / name, release / name)
    shutil.copy2(PROJECT / "packaging/QUICK_START.txt", release / "QUICK_START.txt")
    artifacts = sorted(path for path in release.iterdir() if path.is_file() and path.name != "SHA256SUMS.txt")
    (release / "SHA256SUMS.txt").write_text("".join(f"{digest(path)}  {path.name}\n" for path in artifacts), encoding="utf-8")
    print(f"Release ready: {release}", flush=True)


if __name__ == "__main__":
    main()
