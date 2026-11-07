import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from checkpoint_storage import inventory, selected_files, recycle_checkpoints
from file_lifecycle import DeleteResult, file_identity


class CheckpointStorageTests(unittest.TestCase):
    def test_replaced_checkpoint_is_not_recycled(self):
        with tempfile.TemporaryDirectory() as directory:
            series = Path(directory)
            checkpoint = series / '.series-partials' / ('d' * 24)
            checkpoint.mkdir(parents=True)
            path = checkpoint / 'episode.part'
            path.write_bytes(b'original')
            identities = {path: file_identity(path)}
            replacement = checkpoint / 'new.part'
            replacement.write_bytes(b'replaced')
            replacement.replace(path)
            with patch('file_lifecycle.recycle_file') as recycle:
                results = list(recycle_checkpoints(series, [path], threading.Event(), identities))
            self.assertFalse(results[0].success)
            self.assertTrue(path.exists())
            recycle.assert_not_called()
    def test_only_selected_checkpoint_files_are_recycled(self):
        with tempfile.TemporaryDirectory() as directory:
            series = Path(directory)
            first = series / '.series-partials' / ('a' * 24)
            second = series / '.series-partials' / ('b' * 24)
            first.mkdir(parents=True)
            second.mkdir()
            one, two, media = first / 'one.part', second / 'two.part', series / 'saved.mp4'
            for path in (one, two, media):
                path.write_bytes(b'video')
            self.assertEqual(sum(row.size for row in inventory(series)), 10)
            paths = selected_files(series, ['a' * 24])
            self.assertEqual(paths, [one])

            def recycle(path, *, recycle, expected=None):
                self.assertTrue(recycle)
                path.unlink()
                return DeleteResult(path, 5, True)

            with patch('checkpoint_storage.delete_file', side_effect=recycle):
                results = list(recycle_checkpoints(series, paths, threading.Event()))
            self.assertTrue(results[0].success)
            self.assertTrue(two.exists())
            self.assertTrue(media.exists())

    def test_identifiers_cannot_escape_checkpoint_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                selected_files(Path(directory), ['../outside'])

    def test_cancelled_cleanup_leaves_files_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            series = Path(directory)
            checkpoint = series / '.series-partials' / ('c' * 24)
            checkpoint.mkdir(parents=True)
            path = checkpoint / 'keep.part'
            path.write_bytes(b'video')
            cancelled = threading.Event()
            cancelled.set()
            self.assertEqual(list(recycle_checkpoints(series, [path], cancelled)), [])
            self.assertTrue(path.exists())
