import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from download_core import download_with_progress
from library_store import LibraryStore, LibraryRecoveryRequired
from library_recovery import open_library


class CheckpointContainmentTests(unittest.TestCase):
    def test_checkpoint_directory_links_are_rejected_before_worker_start(self):
        for location in ('root', 'checkpoint'):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                series, outside = base / 'series', base / 'outside'
                series.mkdir()
                outside.mkdir()
                target = series / 'episode.%(ext)s'
                url = 'https://example.com/media.m3u8'
                root = series / '.series-partials'
                fingerprint = hashlib.sha256((str(target.absolute()) + '\0' + url).encode()).hexdigest()[:24]
                link = root if location == 'root' else root / fingerprint
                if location == 'checkpoint':
                    root.mkdir()
                if os.name == 'nt':
                    import _winapi
                    _winapi.CreateJunction(str(outside), str(link))
                else:
                    link.symlink_to(outside, target_is_directory=True)
                try:
                    with patch('download_core.multiprocessing.get_context') as worker:
                        with self.assertRaises(ValueError):
                            download_with_progress(url, target, url, threading.Event(), lambda _: None)
                        worker.assert_not_called()
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    if os.name == 'nt':
                        os.rmdir(link)
                    else:
                        link.unlink()


class LibraryConsistencyTests(unittest.TestCase):
    def test_concurrent_saves_preserve_both_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LibraryStore(Path(directory) / 'library.db')
            url = 'https://example.com/series'
            store.save(url, 'Series', directory, '', [{'number': 1}])
            original_connect = sqlite3.connect
            barrier = threading.Barrier(2)

            class FrozenRow:
                def __init__(self, row):
                    self.row = row

                def fetchone(self):
                    # Without the writer lock both readers see the old payload.
                    # With it, the first reader times out here and commits before
                    # the second connection can enter the read/merge transaction.
                    try:
                        barrier.wait(timeout=.3)
                    except threading.BrokenBarrierError:
                        pass
                    return self.row

            class Connection(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    cursor = super().execute(sql, parameters)
                    if sql.startswith('SELECT payload FROM series WHERE url='):
                        return FrozenRow(cursor.fetchone())
                    return cursor

            errors = []

            def update(number):
                try:
                    store.save(url, 'Series', directory, '', [{'number': number}])
                except Exception as error:
                    errors.append(error)

            with patch('library_store.sqlite3.connect',
                       side_effect=lambda *a, **kw: original_connect(*a, factory=Connection, **kw)):
                workers = [threading.Thread(target=update, args=(n,)) for n in (2, 3)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)
                self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertFalse(errors)
            self.assertEqual([r['number'] for r in store.get(url)['episodes']], [1, 2, 3])

    def test_duplicate_episode_records_enter_startup_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library.db'
            store = LibraryStore(path)
            url = 'https://example.com/series'
            store.save(url, 'Series', directory, '', [{'number': 1}])
            payload = dict(url=url, title='Series', output_root=directory, folder='',
                           episodes=[{'number': 1}, {'number': 1}])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE series SET payload=?', (json.dumps(payload),))
            with self.assertRaisesRegex(LibraryRecoveryRequired, 'Duplicate episode number'):
                LibraryStore(path)
            session = Mock()
            with patch('library_recovery.recover_library', return_value=session) as recover:
                self.assertIs(open_library(Mock(), path), session)
                recover.assert_called_once()
                self.assertIn('Duplicate episode number', recover.call_args.args[2])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(json.loads(db.execute('SELECT payload FROM series').fetchone()[0]), payload)
