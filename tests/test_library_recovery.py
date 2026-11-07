import tempfile
import gc
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk

from library_store import LibraryStore, LibraryRecoveryRequired, library_diagnostics, rebuild_library
from library_recovery import open_library


class LibraryRecoveryTests(unittest.TestCase):
    def test_corrupt_library_reports_recovery_without_changing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library.db'
            original = b'not a sqlite database'
            path.write_bytes(original)
            with self.assertRaises(LibraryRecoveryRequired):
                LibraryStore(path)
            self.assertIn('DatabaseError', library_diagnostics(path))
            self.assertEqual(path.read_bytes(), original)

    def test_explicit_rebuild_preserves_database_and_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library.db'
            original = b'damaged database'
            path.write_bytes(original)
            Path(str(path) + '-wal').write_bytes(b'original wal')
            store, backup = rebuild_library(path)
            self.assertEqual((backup / 'library.db').read_bytes(), original)
            self.assertEqual((backup / 'library.db-wal').read_bytes(), b'original wal')
            self.assertTrue((backup / 'diagnostics.txt').exists())
            self.assertEqual(store.all(), [])
            self.assertEqual(library_diagnostics(path).splitlines()[-1], 'Series records: 0')

    def test_startup_can_continue_with_temporary_library(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'library.db'
            path.write_bytes(b'corrupt')
            try:
                root = tk.Tk()
            except tk.TclError:
                self.skipTest('Desktop display unavailable')

            def continue_session():
                def visit(widget):
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Button) and child.cget('text') == 'Continue this session':
                            child.invoke()
                            return True
                        if visit(child):
                            return True
                    return False
                if not visit(root):
                    root.after(20, continue_session)

            root.after(20, continue_session)
            try:
                store = open_library(root, path)
                self.assertEqual(store.all(), [])
                self.assertNotEqual(store.path, path)
                self.assertEqual(path.read_bytes(), b'corrupt')
                store.temporary_directory.cleanup()
            finally:
                root.update_idletasks()
                root.destroy()
                root = None
                gc.collect()
