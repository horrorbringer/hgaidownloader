import functools
import http.server
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from library_store import LibraryStore
from series_gui_all import SeriesDownloaderApp
from series_identity import series_folder, folder_owner


class FolderIdentityTests(unittest.TestCase):
    def test_same_title_uses_different_source_owned_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = series_folder(root, 'Same title', 'https://example.com/A')
            second = series_folder(root, 'Same title', 'https://example.com/B')
            self.assertNotEqual(first, second)
            self.assertEqual(folder_owner(first), 'https://example.com/A')
            self.assertEqual(series_folder(root, 'Changed title', 'https://example.com/A',
                             {'folder': str(first), 'output_root': str(root)}), first)

    def test_legacy_unowned_files_are_preserved_without_adopting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'Same title'
            old.mkdir()
            media = old / '01_Episode 1.mp4'
            media.write_bytes(b'old content')
            new = series_folder(root, 'Same title', 'https://example.com/B',
                                {'folder': str(old), 'output_root': str(root)})
            self.assertNotEqual(new, old)
            self.assertEqual(media.read_bytes(), b'old content')


class WorkerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.requests = []
        requests = self.requests

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                super().do_GET()

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                      functools.partial(Handler, directory=self.directory.name))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        for name, content in [('index.html', self.page(1)), ('second.html', self.page(2)),
                              ('single.html', self.page(1, False)), ('other.html', self.page(1, False)),
                              ('1.m3u8', '#EXTM3U\n'), ('2.m3u8', '#EXTM3U\n')]:
            (self.base / name).write_text(content, encoding='utf-8')
        self.app = SeriesDownloaderApp.__new__(SeriesDownloaderApp)
        self.app.library = LibraryStore(self.base / 'library.db')
        self.app.cancel_event = threading.Event()
        self.app.pause_event = threading.Event()
        self.app.log_queue = queue.Queue()
        self.app.marked_episodes = {}
        self.calls = []

    def page(self, episode, grid=True):
        links = ('<div data-web-play-ep-grid><a data-ep-id="1" href="/index.html">Episode 1</a>'
                 '<a data-ep-id="2" href="/second.html">Episode 2</a></div>') if grid else ''
        return f'''<html><head><title>Same title</title><meta name="description" content="Test"></head>
        <body>{links}<script id="videoInitialData" type="application/json">
        {{"ep":{episode},"videoSrc":"{self.origin}/{episode}.m3u8"}}</script>
        <script>fetch('/{episode}.m3u8')</script></body></html>'''

    def downloaded(self, stream, target, referer, number, *_args):
        saved = Path(str(target).replace('%(ext)s', 'mp4'))
        saved.write_bytes(b'test media')
        self.calls.append((number, stream, saved))
        return saved

    def test_urgent_episode_downloads_its_own_stream_first(self):
        url = self.origin + '/index.html'
        self.app.library.save(url, 'Same title', str(self.base / 'output'), '',
                              [{'number': 2, 'priority': 'Urgent'}])
        with patch.object(self.app, 'download_with_retries', side_effect=self.downloaded):
            result = self.app.worker(url, self.base / 'output', True, finalize=False, translate_folder=False)
        self.assertIn('2 new', result)
        self.assertEqual([(n, url) for n, url, _ in self.calls],
                         [(2, self.origin + '/2.m3u8'), (1, self.origin + '/1.m3u8')])

    def test_same_title_never_skips_another_series_file(self):
        with patch.object(self.app, 'download_with_retries', side_effect=self.downloaded), \
                patch('series_gui_all.video_is_playable', return_value=True):
            for page in ('single.html', 'other.html'):
                result = self.app.worker(self.origin + '/' + page, self.base / 'output', True,
                                         finalize=False, translate_folder=False)
                self.assertIn('1 new', result)
        self.assertNotEqual(self.calls[0][2].parent, self.calls[1][2].parent)

    def test_existing_registered_video_is_skipped_without_downloading(self):
        url = self.origin + '/single.html'
        old = self.base / 'legacy'
        old.mkdir()
        video = old / '01_Older label.mkv'
        video.write_bytes(b'existing video')
        self.app.library.save(url, 'Same title', str(self.base / 'output'), '',
                              [{'number': 1, 'file': str(video)}])
        with patch('series_gui_all.video_is_playable', return_value=True), \
                patch.object(self.app, 'download_with_retries') as download:
            result = self.app.worker(url, self.base / 'output', True, finalize=False, translate_folder=False)
        self.assertIn('1 already downloaded', result)
        download.assert_not_called()
        self.assertEqual(video.read_bytes(), b'existing video')

    def test_invalid_existing_video_is_kept_when_downloading_recovery(self):
        url = self.origin + '/single.html'
        folder = series_folder(self.base / 'output', 'Same title', url)
        original = folder / '01_Episode 1.mp4'
        original.write_bytes(b'incomplete original')
        with patch('series_gui_all.video_is_playable', return_value=False), \
                patch.object(self.app, 'download_with_retries', side_effect=self.downloaded):
            result = self.app.worker(url, self.base / 'output', True, finalize=False, translate_folder=False)
        self.assertIn('1 new', result)
        self.assertIn('.recovered.', self.calls[0][2].name)
        self.assertEqual(original.read_bytes(), b'incomplete original')

    def test_single_video_range_one_downloads_episode_one(self):
        with patch.object(self.app, 'download_with_retries', side_effect=self.downloaded):
            result = self.app.worker(self.origin + '/single.html', self.base / 'output', True,
                                     retry_only={1}, finalize=False, translate_folder=False)
        self.assertIn('1 new', result)
        self.assertEqual(self.calls[0][0], 1)

    def test_single_video_out_of_range_does_not_download_fallback(self):
        with patch.object(self.app, 'download_with_retries', side_effect=self.downloaded):
            result = self.app.worker(self.origin + '/single.html', self.base / 'output', True,
                                     retry_only={2}, finalize=False, translate_folder=False)
        self.assertTrue(result.startswith('Stopped:'))
        self.assertFalse(self.calls)

    def test_scan_lists_episodes_without_downloads_or_output_files(self):
        output = self.base / 'scan-output'
        with patch.object(self.app, 'download_with_retries') as download, \
                patch.object(self.app, 'resolve_episode_source') as resolve, \
                patch('series_gui_all.translate_folder_name') as translate:
            result = self.app.worker(self.origin + '/index.html', output, True,
                                     finalize=True, scan_only=True)
        self.assertIn('Scan complete: 2 episodes', result)
        self.assertIn('2 ready', result)
        self.assertFalse(output.exists())
        download.assert_not_called()
        resolve.assert_not_called()
        translate.assert_not_called()
        self.assertNotIn('/second.html', self.requests)
        self.assertFalse(any(url.endswith('.m3u8') for url in self.requests))
        events = list(self.app.log_queue.queue)
        self.assertTrue(any(isinstance(e, tuple) and e[0] == 'scan_done' for e in events))
        self.assertEqual({e[1][0] for e in events if isinstance(e, tuple) and e[0] == 'episode_state'
                          and e[1][1] == 'Ready'}, {1, 2})

    def test_scan_marks_registered_existing_video_and_leaves_missing_ready(self):
        url = self.origin + '/index.html'
        output = self.base / 'scan-output'
        video = self.base / '01_Existing.mp4'
        video.write_bytes(b'existing video')
        self.app.library.save(url, 'Series', str(output), '', [{'number': 1, 'file': str(video)}])
        with patch('series_gui_all.video_is_playable', return_value=True), \
                patch.object(self.app, 'download_with_retries') as download:
            result = self.app.worker(url, output, True, finalize=False, scan_only=True)
        self.assertIn('1 already downloaded', result)
        self.assertIn('1 ready', result)
        download.assert_not_called()
        self.assertEqual(video.read_bytes(), b'existing video')
        self.assertFalse(output.exists())

    def test_explicit_wrong_episode_metadata_rejects_captured_stream(self):
        from unittest.mock import Mock
        page = Mock()
        page.url = self.origin + '/second.html'
        page.evaluate.return_value = {'ep': 1, 'url': self.origin + '/1.m3u8'}
        with self.assertRaisesRegex(RuntimeError, 'expected 2'):
            self.app.resolve_episode_source(page, {'epNum': 2, 'href': '/second.html'},
                                            self.origin, [self.origin + '/1.m3u8'], set())


if __name__ == '__main__':
    unittest.main()
