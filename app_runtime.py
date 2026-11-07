"""Resource discovery and persistent storage for source and installed builds."""

import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

SETTINGS_NAME = "series_gui_settings.json"
LIBRARY_NAME = "series_library.sqlite3"


def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def user_data_dir() -> Path:
    override = os.environ.get("SERIES_DOWNLOADER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "SeriesDownloader"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SeriesDownloader"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "series-downloader"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "SeriesDownloader"


def prepare_user_data(legacy_dir: Path | None = None) -> Path:
    """Import legacy files once, without moving or overwriting any user data.

    SQLite's backup API includes committed WAL data from a running legacy app.
    A same-directory hard link publishes each imported file without overwriting
    files created by another process during startup.
    """
    destination = user_data_dir()
    destination.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SERIES_DOWNLOADER_DATA_DIR"):
        return destination
    legacy = legacy_dir or (Path(sys.executable).parent if getattr(sys, "frozen", False)
                            else Path(__file__).resolve().parent)
    if legacy.resolve() == destination.resolve():
        return destination
    for name in (SETTINGS_NAME, LIBRARY_NAME):
        source, target = legacy / name, destination / name
        if target.exists() or not source.is_file():
            continue
        handle, temporary_name = tempfile.mkstemp(prefix=".import-", dir=destination)
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            if name == LIBRARY_NAME:
                try:
                    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
                        with closing(sqlite3.connect(temporary)) as copy:
                            original.backup(copy)
                except sqlite3.DatabaseError as error:
                    if (getattr(error, "sqlite_errorcode", 0) & 0xff) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                        raise
                    # Keep startup recovery available for a damaged legacy file.
                    # The original database and its sidecars remain untouched.
                    shutil.copyfile(source, temporary)
            else:
                shutil.copyfile(source, temporary)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def configure_bundled_tools() -> None:
    if not getattr(sys, "frozen", False):
        return
    resources = resource_dir()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(resources / "browsers")
    tools = resources / "media-tools" / "bin"
    os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
