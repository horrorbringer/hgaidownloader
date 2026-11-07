import tempfile
import gc
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from tkinter import ttk

from library_store import LibraryStore
from safe_paths import safe_child, sanitize_filename
from series_gui_all import SeriesDownloaderApp
from download_core import download_options


class PathTests(unittest.TestCase):
    def test_dot_and_reserved_names_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for title in ('..', '.', '  .. ', 'CON', 'NUL.mp4', '../outside', 'bad\x00name'):
                target = safe_child(root, title)
                self.assertNotEqual(target, root)
                self.assertTrue(target.is_relative_to(root))
            self.assertEqual(sanitize_filename('CON'), '_CON')

    def test_existing_symlink_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'downloads'
            root.mkdir()
            try:
                (root / 'linked').symlink_to(directory, target_is_directory=True)
            except OSError:
                self.skipTest('Symlink creation unavailable')
            with self.assertRaises(ValueError):
                safe_child(root, 'linked')


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class GuiRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('Desktop display unavailable')
        self.addCleanup(self.destroy_root)
        self.settings = patch('series_gui_all.SETTINGS_FILE', self.base / 'settings.json')
        self.settings.start()
        self.addCleanup(self.settings.stop)
        with patch.object(SeriesDownloaderApp, 'detect_ffmpeg_version'):
            self.app = SeriesDownloaderApp(self.root, self.base / 'library.db')
        self.root.update()

    def destroy_root(self):
        self.app.progress.stop()
        self.app.stop_progress_animation()
        for job in self.root.tk.call('after', 'info'):
            self.root.after_cancel(job)
        self.root.destroy()
        self.app = None
        self.root = None
        gc.collect()

    def test_enter_on_cancel_never_deletes(self):
        path = self.base / 'dummy.mp4'
        path.write_bytes(b'dummy')
        with patch.object(self.app, 'start_deletion') as delete:
            self.app.confirm_delete_paths([(1, path)])
            self.root.update()
            cancel = next(w for w in descendants(self.root)
                          if isinstance(w, ttk.Button) and w.cget('text') == 'Cancel')
            cancel.focus_force()
            self.root.update()
            cancel.event_generate('<Return>')
            self.root.update()
            delete.assert_not_called()
        self.assertTrue(path.exists())

    def test_enter_on_delete_commits_recycle_default(self):
        path = self.base / 'dummy.mp4'
        path.write_bytes(b'dummy')
        with patch.object(self.app, 'start_deletion') as delete:
            self.app.confirm_delete_paths([(1, path)])
            self.root.update()
            button = next(w for w in descendants(self.root)
                          if isinstance(w, ttk.Button) and w.cget('text') == 'Delete files')
            button.focus_force()
            self.root.update()
            button.event_generate('<Return>')
            self.root.update()
            self.assertEqual(delete.call_args.args, ([(1, path)], True, False))
            self.assertEqual(delete.call_args.kwargs['identities'][1].size, 5)

    def test_confirmation_snapshot_preserves_replacement_file(self):
        path = self.base / 'episode.mp4'
        path.write_bytes(b'original')
        self.app.confirm_delete_paths([(1, path)])
        self.root.update()
        replacement = self.base / 'replacement.mp4'
        replacement.write_bytes(b'replaced')
        replacement.replace(path)
        delete_button = next(w for w in descendants(self.root)
                             if isinstance(w, ttk.Button) and w.cget('text') == 'Delete files')
        with patch('file_lifecycle.recycle_file') as recycle, \
                patch.object(self.app, 'show_deletion_summary') as summary:
            delete_button.invoke()
            deadline = time.monotonic() + 5
            while self.app.delete_running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertFalse(self.app.delete_running)
            recycle.assert_not_called()
            result = summary.call_args.args[0][0][1]
            self.assertFalse(result.success)
            self.assertIn('confirm deletion again', result.error)
        self.assertEqual(path.read_bytes(), b'replaced')

    def test_retry_delete_requires_fresh_confirmation(self):
        from file_lifecycle import DeleteResult
        path = self.base / 'episode.mp4'
        path.write_bytes(b'video')
        self.app.show_deletion_summary([(1, DeleteResult(path, 0, False, 'File changed'))],
                                       1, 1.0, True, False)
        button = next(w for w in descendants(self.root)
                      if isinstance(w, ttk.Button) and w.cget('text') == 'Retry all failed')
        with patch.object(self.app, 'confirm_delete_paths') as confirm, \
                patch.object(self.app, 'start_deletion') as start:
            button.invoke()
            confirm.assert_called_once_with([(1, path)])
            start.assert_not_called()

    def test_retry_keeps_original_series_and_destination(self):
        self.app.current_series_url = 'https://example.com/series-A'
        self.app.current_output_root = str(self.base / 'original')
        self.app.url_var.set('https://example.com/series-B')
        self.app.output_var.set(str(self.base / 'other'))
        self.app.failed_episodes = {2}
        with patch('series_gui_all.threading.Thread') as thread:
            self.app.retry_failed()
            args = thread.call_args.kwargs['args']
            self.assertEqual(args[0], ['https://example.com/series-A'])
            self.assertEqual(args[1], (self.base / 'original').resolve())

    def test_scan_button_starts_scan_without_directory_creation_or_ffprobe(self):
        self.app.url_var.set('https://example.com/scan')
        output = self.base / 'not-created'
        self.app.output_var.set(str(output))
        with patch('series_gui_all.shutil.which', return_value=None), \
                patch('series_gui_all.threading.Thread') as worker:
            self.app.scan_btn.invoke()
        self.assertTrue(self.app.scanning)
        self.assertTrue(worker.call_args.kwargs['args'][-1])
        self.assertFalse(output.exists())
        self.assertTrue(self.app.scan_btn.instate(['disabled']))
        self.app.emit(('scan_done', 'Scan complete'))
        with patch('series_gui_all.messagebox.showinfo') as info:
            self.app.poll_logs()
            info.assert_not_called()
        self.assertFalse(self.app.running)
        self.assertFalse(self.app.scanning)
        self.assertFalse(self.app.scan_btn.instate(['disabled']))

    def test_enter_in_url_field_scans_instead_of_downloading(self):
        with patch.object(self.app, 'scan_episodes') as scan, \
                patch.object(self.app, 'start_download') as download:
            self.app.url_entry.focus_force()
            self.root.update()
            self.app.url_entry.event_generate('<Return>')
            self.root.update()
            scan.assert_called_once()
            download.assert_not_called()

    def test_cancel_before_scan_worker_starts_restores_controls(self):
        url = 'https://example.com/scan'
        self.app.url_var.set(url)
        self.app.output_var.set(str(self.base / 'not-created'))
        with patch('series_gui_all.threading.Thread'):
            self.app.scan_episodes()
        self.app.cancel_download()
        self.app.run_queue([url], self.base, True, None, False, False, False, False, scan_only=True)
        with patch('series_gui_all.messagebox.showinfo') as info:
            self.app.poll_logs()
            info.assert_not_called()
        self.assertFalse(self.app.running)
        self.assertIn('Scan cancelled', self.app.status_var.get())
        self.assertFalse(self.app.scan_btn.instate(['disabled']))

    def test_select_pending_excludes_existing_and_manually_marked_episodes(self):
        for number, state in enumerate(('Ready', 'Already downloaded', 'Marked downloaded', 'Failed'), 1):
            item = f'ep-{number}'
            self.app.episode_list.insert('', 'end', iid=item,
                values=('☐', f'Episode {number}', state, '—', '—', '—'))
            self.app.episode_order.append(item)
        self.app.select_pending()
        self.assertEqual(set(self.app.episode_list.selection()), {'ep-1', 'ep-4'})

    def test_library_reopens_paths_status_and_priority(self):
        path = self.base / 'episode.mp4'
        path.write_bytes(b'video')
        self.app.current_series_url = 'https://example.com/show'
        self.app.current_output_root = str(self.base)
        self.app.current_series_folder = str(self.base)
        self.app.episode_list.insert('', 'end', iid='ep-3',
                                    values=('☐', 'Third episode', 'Done', '100%', '5 B', str(path)))
        self.app.episode_order.append('ep-3')
        self.app.saved_files[3] = path
        self.app.episode_priorities[3] = 'High'
        self.app.save_current_library()
        reopened = LibraryStore(self.base / 'library.db')
        self.assertEqual(reopened.get(self.app.current_series_url)['episodes'][0]['file'], str(path))
        self.app.load_library_series(self.app.current_series_url)
        self.assertEqual(self.app.saved_files[3], path)
        self.assertEqual(self.app.episode_priorities[3], 'High')
        self.assertEqual(self.app.episode_list.set('ep-3', 'state'), 'Done')

    def test_library_keeps_other_episodes_and_separates_series(self):
        store = LibraryStore(self.base / 'other.db')
        store.save('https://example.com/A', 'A', str(self.base), '',
                   [{'number': 1, 'state': 'Done'}])
        store.save('https://example.com/A', 'A', str(self.base), '',
                   [{'number': 2, 'state': 'Waiting'}])
        store.save('https://example.com/B', 'B', str(self.base), '',
                   [{'number': 1, 'state': 'Failed'}])
        self.assertEqual([r['number'] for r in store.get('https://example.com/A')['episodes']], [1, 2])
        self.assertEqual(store.get('https://example.com/B')['episodes'][0]['state'], 'Failed')
        store.remove_episode('https://example.com/A', 1)
        self.assertEqual([r['number'] for r in store.get('https://example.com/A')['episodes']], [2])

    def test_switching_series_cannot_reuse_previous_file_associations(self):
        self.app.current_series_url = 'https://example.com/A'
        self.app.current_output_root = str(self.base)
        self.app.episode_list.insert('', 'end', iid='ep-1',
                                    values=('☐', 'Episode 1', 'Done', '100%', '5 B', 'old.mp4'))
        self.app.episode_order.append('ep-1')
        self.app.saved_files[1] = self.base / 'old.mp4'
        self.app.log_queue.put(('series_context', ('https://example.com/B', str(self.base))))
        self.app.poll_logs()
        self.assertEqual(self.app.current_series_url, 'https://example.com/B')
        self.assertFalse(self.app.saved_files)
        self.assertFalse(self.app.episode_order)
        self.app.load_library_series('https://example.com/A')
        self.assertTrue(self.app.library_can_save)
        self.assertEqual(self.app.current_series_url, 'https://example.com/A')

    def test_queue_switch_preserves_previous_title_and_ignores_stale_events(self):
        first, second = 'https://example.com/A', 'https://example.com/B'
        self.app.current_series_url = first
        self.app.current_output_root = str(self.base)
        self.app.series_name_var.set('Original A title')
        self.app.save_current_library()
        self.app.set_worker_source(second)
        self.app.emit(('series_context', (second, str(self.base))))
        self.app.emit(('series_name', 'Correct B title'))
        self.app.set_worker_source(first)
        self.app.emit(('series_name', 'Delayed A title'))
        self.app.emit(('episode_state', (1, 'Failed')))
        self.app.poll_logs()
        self.assertEqual(self.app.library.get(first)['title'], 'Original A title')
        self.assertEqual(self.app.series_name_var.get(), 'Correct B title')
        self.assertFalse(self.app.failed_episodes)

    def test_run_queue_does_not_replace_previous_title_with_queue_caption(self):
        first, second = 'https://example.com/A', 'https://example.com/B'
        self.app.current_series_url = first
        self.app.current_output_root = str(self.base)
        self.app.series_name_var.set('Original A title')
        self.app.save_current_library()

        def worker(url, *_args, **_kwargs):
            self.app.emit(('series_context', (url, str(self.base))))
            self.app.emit(('series_name', 'Correct B title'))
            return 'Finished: 0 new, 0 already downloaded, 0 failed or skipped'

        with patch.object(self.app, 'worker', side_effect=worker), \
                patch('series_gui_all.messagebox.showinfo'):
            self.app.run_queue([second], self.base, True, None, False, False, False, False)
            self.app.poll_logs()
        self.assertEqual(self.app.library.get(first)['title'], 'Original A title')

    def test_literal_template_tokens_are_preserved_in_paths(self):
        import yt_dlp
        target = self.base / 'Drama %(title)s' / '01_%(id)s.%(ext)s'
        with yt_dlp.YoutubeDL(download_options(target, 'https://example.com/')) as downloader:
            filename = downloader.prepare_filename({'id': 'different', 'title': 'remote', 'ext': 'mp4'})
        self.assertEqual(Path(filename), target.with_name('01_%(id)s.mp4'))

    def test_checkpoint_dialog_recycles_only_selected_partials(self):
        from file_lifecycle import DeleteResult
        self.app.current_series_folder = str(self.base)
        files = []
        for identifier in ('a' * 24, 'b' * 24):
            folder = self.base / '.series-partials' / identifier
            folder.mkdir(parents=True)
            path = folder / 'episode.part'
            path.write_bytes(b'partial')
            files.append(path)
        media = self.base / 'completed.mp4'
        media.write_bytes(b'completed')
        self.app.show_checkpoints()
        self.root.update()
        window = next(w for w in self.root.winfo_children()
                      if isinstance(w, tk.Toplevel) and w.title() == 'Partial downloads')
        rows = next(w for w in descendants(window) if isinstance(w, ttk.Treeview))
        rows.selection_set('a' * 24)
        button = next(w for w in descendants(window)
                      if isinstance(w, ttk.Button) and w.cget('text') == 'Recycle selected')

        def recycle(path, *, recycle, expected=None):
            self.assertTrue(recycle)
            self.assertIsNotNone(expected)
            size = path.stat().st_size
            path.unlink()
            return DeleteResult(path, size, True)

        with patch('series_gui_all.messagebox.askyesno', return_value=True), \
                patch('checkpoint_storage.delete_file', side_effect=recycle):
            button.invoke()
            deadline = time.monotonic() + 5
            while self.app.delete_running and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertFalse(self.app.delete_running)
        self.assertFalse(files[0].exists())
        self.assertTrue(files[1].exists())
        self.assertTrue(media.exists())

    def test_existing_video_preview_links_verified_files_without_moving_them(self):
        path = self.base / '01_Original title.mkv'
        path.write_bytes(b'existing video')
        url = 'https://example.com/existing'
        self.app.url_var.set(url)
        self.app.output_var.set(str(self.base))
        with patch('series_gui_all.filedialog.askdirectory', return_value=str(self.base)), \
                patch('series_gui_all.video_is_playable', return_value=True):
            self.app.link_existing_videos()
            window = next(w for w in self.root.winfo_children()
                          if isinstance(w, tk.Toplevel) and w.title() == 'Existing videos')
            button = next(w for w in descendants(window)
                          if isinstance(w, ttk.Button) and w.cget('text') == 'Link selected videos')
            deadline = time.monotonic() + 5
            while button.instate(['disabled']) and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            self.assertFalse(button.instate(['disabled']))
            button.invoke()
            self.root.update()
        self.assertEqual(self.app.current_series_url, url)
        self.assertEqual(self.app.saved_files[1], path.resolve())
        self.assertEqual(self.app.episode_list.set('ep-1', 'state'), 'Already downloaded')
        self.assertEqual(path.read_bytes(), b'existing video')
        self.assertEqual(self.app.library.get(url)['episodes'][0]['file'], str(path.resolve()))

    def test_existing_video_preview_rejects_a_different_series_claim(self):
        path = self.base / '01_Existing.mp4'
        path.write_bytes(b'existing video')
        first, second = 'https://example.com/A', 'https://example.com/B'
        self.app.library.save(second, 'B', str(self.base), '', [{'number': 1, 'file': str(path)}])
        self.app.url_var.set(first)
        self.app.output_var.set(str(self.base))
        with patch('series_gui_all.filedialog.askdirectory', return_value=str(self.base)), \
                patch('series_gui_all.video_is_playable', return_value=True):
            self.app.link_existing_videos()
            window = next(w for w in self.root.winfo_children()
                          if isinstance(w, tk.Toplevel) and w.title() == 'Existing videos')
            button = next(w for w in descendants(window)
                          if isinstance(w, ttk.Button) and w.cget('text') == 'Link selected videos')
            deadline = time.monotonic() + 5
            while button.instate(['disabled']) and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            button.invoke()
            self.root.update()
            self.assertIsNone(self.app.library.get(first))
            self.assertTrue(window.winfo_exists())
        self.assertEqual(path.read_bytes(), b'existing video')

    def test_download_uses_secure_resumable_options(self):
        with patch('series_gui_all.download_with_progress') as download:
            self.app.download_m3u8('https://example.com/a.m3u8', self.base / 'a.%(ext)s',
                                   'https://example.com/', 1, 0, 1)
            download.assert_called_once()
            options = download_options(self.base / 'a.%(ext)s', 'https://example.com/')
            self.assertFalse(options['nocheckcertificate'])
            self.assertTrue(options['continuedl'])
            self.assertTrue(options['hls_prefer_native'])
            self.assertFalse(options['skip_unavailable_fragments'])


if __name__ == '__main__':
    unittest.main()
