"""Local HLS integration test: stop, reopen the downloader, and continue fragments."""
import functools
import http.server
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from download_core import download_with_progress, download_options
from series_gui_all import SeriesDownloaderApp, video_is_playable


class ResumeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_interrupted_hls_continues_without_refetching_completed_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc=size=160x90:rate=25', '-t', '8', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-g', '25', '-sc_threshold', '0',
                            '-hls_time', '1', '-hls_list_size', '0', '-hls_segment_filename',
                            str(base / 'segment-%03d.ts'), str(base / 'index.m3u8')], check=True)
            requests = []

            class Handler(http.server.SimpleHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    if self.path.endswith('.ts'):
                        time.sleep(.1)
                    super().do_GET()

                def log_message(self, *_args):
                    pass

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                       functools.partial(Handler, directory=directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                app = SeriesDownloaderApp.__new__(SeriesDownloaderApp)
                app.cancel_event = threading.Event()
                app.log_queue = queue.Queue()

                def cancel_after_checkpoint(data):
                    if data.get('fragment_index', 0) >= 2:
                        app.cancel_event.set()

                def options_for_test(target, referer):
                    options = download_options(target, referer)
                    options['concurrent_fragment_downloads'] = 1
                    return options

                url = f'http://127.0.0.1:{server.server_port}/index.m3u8'
                literal_folder = base / 'Drama %(title)s'
                literal_folder.mkdir()
                target = literal_folder / 'output_%(id)s.%(ext)s'
                with patch('download_core.download_options', side_effect=options_for_test):
                    with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                        download_with_progress(url, target, url, app.cancel_event, cancel_after_checkpoint)
                self.assertTrue(list(base.rglob('output*.part')))
                self.assertTrue(list(base.rglob('output*.ytdl')))
                app.cancel_event.clear()
                requests.clear()
                # Another manifest/codec gets an independent checkpoint directory.
                app.download_m3u8(url + '?codec=alternate', target, url, 1, 0, 1, 8)
                self.assertIn('/segment-000.ts', requests)
                self.assertTrue(list(base.rglob('output*.ytdl')))
                # Preserve the completed alternate rendition before publishing
                # the original; production never overwrites an existing video.
                (literal_folder / 'output_%(id)s.mp4').rename(literal_folder / 'alternate.mp4')
                requests.clear()
                app.download_m3u8(url, target, url, 1, 0, 1, 8)
                self.assertNotIn('/segment-000.ts', requests)
                self.assertNotIn('/segment-001.ts', requests)
                self.assertTrue(video_is_playable(literal_folder / 'output_%(id)s.mp4', 8))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
