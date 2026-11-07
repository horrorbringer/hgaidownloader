"""Safe, per-file cleanup primitives for the desktop UI.

The default operation uses the system trash service. Windows uses IFileOperation
with explicit recycling flags and its progress sink. No permanent-delete fallback.
"""

from __future__ import annotations

import ctypes
import os
import sys
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LockingProcess:
    name: str
    pid: int


@dataclass(frozen=True)
class DeleteResult:
    path: Path
    size: int
    success: bool
    error: str = ""
    lockers: tuple[LockingProcess, ...] = ()


@dataclass(frozen=True)
class FileIdentity:
    resolved_path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


class FileChangedError(ValueError):
    pass


def file_identity(path: Path) -> FileIdentity:
    path = Path(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Not a regular file: {path}')
    return FileIdentity(str(path.resolve()), info.st_dev, info.st_ino, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns)


def file_size(path: Path) -> int:
    """Return a regular file's size without following symbolic links."""
    return file_identity(path).size


def recycle_file(path: Path) -> None:
    """Move one file to the system Recycle Bin / Trash, or fail closed."""
    path = path.absolute()
    try:
        if sys.platform == "win32":
            # Require the IFileOperation implementation; never silently fall back
            # to SHFileOperation when pywin32 is missing.
            from send2trash.win.modern import send2trash
        else:
            from send2trash import send2trash
    except ImportError as exc:
        raise RuntimeError("Trash support requires Send2Trash and pywin32 on Windows") from exc
    send2trash(str(path))
    if path.exists():
        raise OSError(f"Trash service did not remove the file: {path}")


def locking_processes(path: Path) -> tuple[LockingProcess, ...]:
    """Ask Windows Restart Manager which applications currently use a file."""
    if sys.platform != "win32":
        return ()

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [
            ("Process", RM_UNIQUE_PROCESS), ("strAppName", wintypes.WCHAR * 256),
            ("strServiceShortName", wintypes.WCHAR * 64), ("ApplicationType", wintypes.DWORD),
            ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    manager = ctypes.windll.rstrtmgr
    handle = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if manager.RmStartSession(ctypes.byref(handle), 0, key):
        return ()
    try:
        paths = (wintypes.LPCWSTR * 1)(str(path.absolute()))
        if manager.RmRegisterResources(handle, 1, paths, 0, None, 0, None):
            return ()
        needed = wintypes.UINT()
        count = wintypes.UINT()
        reasons = wintypes.DWORD()
        code = manager.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                                 None, ctypes.byref(reasons))
        if code not in (0, 234) or not needed.value:
            return ()
        records = (RM_PROCESS_INFO * needed.value)()
        count.value = needed.value
        if manager.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                             records, ctypes.byref(reasons)):
            return ()
        return tuple(LockingProcess(record.strAppName or "Unknown application",
                                    record.Process.dwProcessId)
                     for record in records[:count.value])
    finally:
        manager.RmEndSession(handle)


def delete_file(path: Path, *, recycle: bool = True, expected: FileIdentity | None = None) -> DeleteResult:
    path = Path(path).absolute()
    try:
        current = file_identity(path)
        if expected is not None and current != expected:
            raise FileChangedError('File changed after confirmation. Review it and confirm deletion again.')
        size = current.size
        if recycle:
            recycle_file(path)
        else:
            path.unlink()
        return DeleteResult(path, size, True)
    except FileChangedError as exc:
        return DeleteResult(path, 0, False, str(exc))
    except Exception as exc:
        try:
            lockers = locking_processes(path)
        except Exception:
            lockers = ()
        return DeleteResult(path, 0, False, str(exc), lockers)


def open_recycle_bin() -> None:
    if sys.platform == "win32":
        os.startfile("shell:RecycleBinFolder")
    else:
        raise RuntimeError("Open the system Trash using your file manager")


def delete_batch(targets, stop_event, *, recycle=True, on_start=None, identities=None):
    """Yield per-file results; cancellation takes effect after the active file."""
    for number, path in targets:
        if stop_event.is_set():
            return
        if on_start:
            on_start(number)
        expected = identities[number] if identities is not None else None
        yield number, delete_file(path, recycle=recycle, expected=expected)
