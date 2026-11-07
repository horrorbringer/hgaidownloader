"""Installed storage must preserve old profiles, including committed WAL data."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import app_runtime


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.legacy = self.base / "legacy"
        self.legacy.mkdir()
        self.destination = self.base / "profile"
        environment = os.environ.copy()
        environment.pop("SERIES_DOWNLOADER_DATA_DIR", None)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        location = patch("app_runtime.user_data_dir", return_value=self.destination)
        location.start()
        self.addCleanup(location.stop)

    def test_import_preserves_sources_and_committed_wal_records(self):
        settings = self.legacy / app_runtime.SETTINGS_NAME
        settings.write_text('{"history": []}', encoding="utf-8")
        with closing(sqlite3.connect(self.legacy / app_runtime.LIBRARY_NAME)) as original:
            original.execute("PRAGMA journal_mode=WAL")
            original.execute("CREATE TABLE saved (title TEXT)")
            original.execute("INSERT INTO saved VALUES ('Existing series')")
            original.commit()
            app_runtime.prepare_user_data(self.legacy)
            with closing(sqlite3.connect(self.destination / app_runtime.LIBRARY_NAME)) as imported:
                self.assertEqual(imported.execute("SELECT title FROM saved").fetchone(), ("Existing series",))
            self.assertEqual(original.execute("SELECT count(*) FROM saved").fetchone(), (1,))
        self.assertEqual((self.destination / settings.name).read_bytes(), settings.read_bytes())
        self.assertTrue(settings.exists())
        self.assertFalse(list(self.destination.glob(".import-*")))

    def test_existing_profile_is_never_replaced(self):
        self.destination.mkdir()
        for name in (app_runtime.SETTINGS_NAME, app_runtime.LIBRARY_NAME):
            (self.destination / name).write_bytes(b"existing user data")
            (self.legacy / name).write_bytes(b"older data")
        app_runtime.prepare_user_data(self.legacy)
        for name in (app_runtime.SETTINGS_NAME, app_runtime.LIBRARY_NAME):
            self.assertEqual((self.destination / name).read_bytes(), b"existing user data")

    def test_damaged_legacy_database_remains_available_to_startup_recovery(self):
        from library_store import LibraryStore, LibraryRecoveryRequired
        original = self.legacy / app_runtime.LIBRARY_NAME
        original.write_bytes(b"not a database")
        app_runtime.prepare_user_data(self.legacy)
        self.assertEqual(original.read_bytes(), b"not a database")
        imported = self.destination / app_runtime.LIBRARY_NAME
        self.assertEqual(imported.read_bytes(), original.read_bytes())
        with self.assertRaises(LibraryRecoveryRequired):
            LibraryStore(imported)
        self.assertFalse(list(self.destination.glob(".import-*")))

    def test_explicit_test_profile_does_not_import_personal_data(self):
        (self.legacy / app_runtime.SETTINGS_NAME).write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"SERIES_DOWNLOADER_DATA_DIR": str(self.destination)}):
            app_runtime.prepare_user_data(self.legacy)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_frozen_tools_override_machine_installations(self):
        with patch("app_runtime.sys.frozen", True, create=True), \
             patch("app_runtime.resource_dir", return_value=self.base), \
             patch.dict(os.environ, {"PATH": "original-path", "PLAYWRIGHT_BROWSERS_PATH": "old-browser"}):
            app_runtime.configure_bundled_tools()
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(self.base / "browsers"))
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(self.base / "media-tools" / "bin"))


if __name__ == "__main__":
    unittest.main()
