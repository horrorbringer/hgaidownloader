"""Transactional local library, keyed by source URL and episode number."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class LibraryRecoveryRequired(Exception):
    def __init__(self, path, reason):
        self.path = Path(path)
        super().__init__(f'{path}: {reason}')


class LibraryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('SQLite integrity check failed')
                db.execute('CREATE TABLE IF NOT EXISTS series '
                           '(id TEXT PRIMARY KEY, url TEXT UNIQUE, payload TEXT, updated INTEGER)')
                for (payload,) in db.execute('SELECT payload FROM series'):
                    self.decode(payload)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
            raise LibraryRecoveryRequired(self.path, str(exc)) from exc

    @staticmethod
    def decode(payload):
        data = json.loads(payload)
        for key in ('url', 'title', 'output_root', 'folder'):
            if not isinstance(data[key], str):
                raise ValueError(f'Invalid library field: {key}')
        if not isinstance(data['episodes'], list):
            raise ValueError('Invalid episode inventory')
        seen = set()
        for record in data['episodes']:
            number = record['number']
            if type(number) is not int or number < 1:
                raise ValueError('Invalid episode number')
            if number in seen:
                raise ValueError(f'Duplicate episode number: {number}')
            seen.add(number)
            for key, default in dict(title=f'Episode {number}', state='Waiting', percent='—',
                                     file='', priority='Normal').items():
                record.setdefault(key, default)
                if not isinstance(record[key], str):
                    raise ValueError(f'Invalid episode field: {key}')
        return data

    def save(self, url: str, title: str, output_root: str, folder: str, episodes: list[dict]):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            # Acquire the writer lock before reading the value to be merged.
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT payload FROM series WHERE url=?', (url,)).fetchone()
            old = self.decode(previous[0]) if previous else {}
            merged = {row['number']: row for row in old.get('episodes', [])}
            merged.update({row['number']: row for row in episodes})
            payload = json.dumps(dict(url=url, title=title, output_root=output_root,
                                      folder=folder or old.get('folder', ''),
                                      episodes=sorted(merged.values(), key=lambda r: r['number'])),
                                 ensure_ascii=False)
            self.decode(payload)
            db.execute('INSERT INTO series VALUES (?, ?, ?, ?) '
                       'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated=excluded.updated',
                       (hashlib.sha256(url.encode()).hexdigest(), url, payload, time.time_ns()))

    def remove_episode(self, url: str, number: int):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM series WHERE url=?', (url,)).fetchone()
            if row:
                payload = self.decode(row[0])
                payload['episodes'] = [r for r in payload['episodes'] if r['number'] != number]
                db.execute('UPDATE series SET payload=?, updated=? WHERE url=?',
                           (json.dumps(payload, ensure_ascii=False), time.time_ns(), url))

    def get(self, url: str):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute('SELECT payload FROM series WHERE url=?', (url,)).fetchone()
        return self.decode(row[0]) if row else None

    def all(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = db.execute('SELECT payload FROM series ORDER BY updated DESC, rowid DESC').fetchall()
        return [self.decode(row[0]) for row in rows]


def library_diagnostics(path: Path) -> str:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            checks = db.execute('PRAGMA quick_check').fetchall()
            count = db.execute('SELECT count(*) FROM series').fetchone()[0]
            for (payload,) in db.execute('SELECT payload FROM series'):
                LibraryStore.decode(payload)
        return f'Integrity: {checks}\nSeries records: {count}'
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        return f'Diagnostics: {type(exc).__name__}: {exc}'


def rebuild_library(path: Path) -> tuple[LibraryStore, Path]:
    """Explicit recovery: preserve the database and sidecars before rebuilding."""
    path = path.resolve()
    backup = path.with_name(f'{path.stem}.backup-{time.time_ns()}')
    backup.mkdir()
    report = library_diagnostics(path)
    for original in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
        if original.exists():
            original.replace(backup / original.name)
    (backup / 'diagnostics.txt').write_text(report, encoding='utf-8')
    return LibraryStore(path), backup
