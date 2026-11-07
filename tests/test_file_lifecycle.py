import tempfile
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from file_lifecycle import LockingProcess, delete_file, delete_batch, DeleteResult, file_identity


class DeleteFileTests(unittest.TestCase):
    def test_replacement_file_is_preserved_for_reconfirmation(self):
        for recycle in (True, False):
            with self.subTest(recycle=recycle), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'episode.mp4'
                path.write_bytes(b'original')
                original = file_identity(path)
                replacement = path.with_name('replacement.mp4')
                replacement.write_bytes(b'replaced')
                replacement.replace(path)
                with patch('file_lifecycle.recycle_file') as trash:
                    result = delete_file(path, recycle=recycle, expected=original)
                self.assertFalse(result.success)
                self.assertIn('confirm deletion again', result.error)
                self.assertEqual(path.read_bytes(), b'replaced')
                trash.assert_not_called()

    def test_modified_file_is_preserved_for_reconfirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'episode.mp4'
            path.write_bytes(b'original')
            original = file_identity(path)
            path.write_bytes(b'changed and longer')
            result = delete_file(path, recycle=False, expected=original)
            self.assertFalse(result.success)
            self.assertTrue(path.exists())

    def test_unchanged_confirmed_file_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'episode.mp4'
            path.write_bytes(b'original')
            result = delete_file(path, recycle=False, expected=file_identity(path))
            self.assertTrue(result.success)

    def test_cancel_finishes_active_file_and_preserves_remaining_files(self):
        stop = threading.Event()
        paths = [(1, Path('first.mp4')), (2, Path('second.mp4'))]

        def remove(path, **_kwargs):
            stop.set()
            return DeleteResult(path, 5, True)

        with patch('file_lifecycle.delete_file', side_effect=remove) as delete:
            results = list(delete_batch(paths, stop))
        self.assertEqual([n for n, _ in results], [1])
        self.assertEqual(delete.call_count, 1)

    def test_unavailable_recycling_never_falls_back_to_unlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'keep.mp4'
            path.write_bytes(b'video')
            with patch('file_lifecycle.recycle_file', side_effect=RuntimeError('Unavailable')):
                result = delete_file(path)
            self.assertFalse(result.success)
            self.assertEqual(path.read_bytes(), b'video')

    @unittest.skipUnless(os.name == 'nt', 'Windows integration test')
    def test_native_recycle_preserves_locked_file_then_succeeds_after_unlock(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'locked-test.mp4'
            path.write_bytes(b'video')
            handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            try:
                result = delete_file(path)
                self.assertFalse(result.success)
                self.assertTrue(path.exists())
                self.assertTrue(any(p.pid == os.getpid() for p in result.lockers))
            finally:
                kernel.CloseHandle(handle)
            result = delete_file(path)
            self.assertTrue(result.success, result.error)
            self.assertFalse(path.exists())

    def test_default_uses_recycle_bin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mp4"
            path.write_bytes(b"video")
            with patch("file_lifecycle.recycle_file", side_effect=lambda target: target.unlink()) as recycle:
                result = delete_file(path)
            self.assertTrue(result.success)
            self.assertEqual(result.size, 5)
            recycle.assert_called_once_with(path.absolute())

    def test_recycle_failure_preserves_file_and_reports_locker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mp4"
            path.write_bytes(b"video")
            with patch("file_lifecycle.recycle_file", side_effect=OSError("sharing violation")), \
                    patch("file_lifecycle.locking_processes",
                          return_value=(LockingProcess("VLC", 1234),)):
                result = delete_file(path)
            self.assertFalse(result.success)
            self.assertTrue(path.exists())
            self.assertEqual(result.lockers[0].pid, 1234)

    def test_permanent_delete_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mp4"
            path.write_bytes(b"video")
            result = delete_file(path, recycle=False)
            self.assertTrue(result.success)
            self.assertFalse(path.exists())

    def test_lock_inspection_failure_does_not_hide_deletion_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.mp4"
            path.write_bytes(b"video")
            with patch("file_lifecycle.recycle_file", side_effect=OSError("denied")), \
                    patch("file_lifecycle.locking_processes", side_effect=ValueError("API failed")):
                result = delete_file(path)
            self.assertFalse(result.success)
            self.assertTrue(path.exists())
            self.assertIn("denied", result.error)

    def test_directory_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("file_lifecycle.recycle_file") as recycle:
                result = delete_file(Path(directory), recycle=False)
            self.assertFalse(result.success)
            self.assertTrue(Path(directory).is_dir())
            recycle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
