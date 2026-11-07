import tempfile
import unittest
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from existing_media import (episode_number, episode_candidates, available_output_template,
                            inspect_folder, conflicting_claims, registered_episode_paths)
from download_core import publish_without_overwrite
from library_store import LibraryStore
from series_gui_all import find_existing_episode, video_is_playable


class ExistingMediaTests(unittest.TestCase):
    def test_episode_names_and_changed_labels(self):
        for name in ('01_Old label.mp4', '1.mkv', 'Episode 01.mov', 'EP01.m4v', 'S01E01.avi', '第1集.webm'):
            self.assertEqual(episode_number(Path(name)), 1, name)
        self.assertIsNone(episode_number(Path('movie trailer.mp4')))

    def test_literal_brackets_and_label_changes_reuse_existing_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / '01_Old [label].mkv'
            video.write_bytes(b'video')
            with patch('series_gui_all.video_is_playable', return_value=True):
                self.assertEqual(find_existing_episode(folder, 1, 'New label'), video)

    def test_registered_file_can_have_an_arbitrary_name_and_location(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            folder = base / 'new'
            folder.mkdir()
            registered = base / 'my movie.mov'
            registered.write_bytes(b'video')
            with patch('series_gui_all.video_is_playable', return_value=True):
                self.assertEqual(find_existing_episode(folder, 7, 'Episode 7',
                                 registered_paths=[registered]), registered)

    def test_partial_invalid_and_wrong_episode_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ('01_Episode.invalid-123.mp4', '01_Episode.downloading.mp4',
                         '01_Episode.mp4.part', '02_Episode.mp4'):
                (folder / name).write_bytes(b'video')
            self.assertEqual(episode_candidates(folder, 1, 'Episode'), [])

    def test_existing_unusable_output_is_preserved_beside_recovery_download(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            original = folder / '01_Episode 1.mp4'
            original.write_bytes(b'incomplete')
            self.assertEqual(available_output_template(folder, 1, 'Episode 1').name,
                             '01_Episode 1.recovered.%(ext)s')
            self.assertEqual(original.read_bytes(), b'incomplete')
            self.assertEqual(available_output_template(folder, 1, 'Episode 1').name,
                             '01_Episode 1.recovered.%(ext)s')

    def test_publish_never_replaces_existing_video(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            completed, existing = base / 'new.mp4', base / 'existing.mp4'
            completed.write_bytes(b'new video')
            existing.write_bytes(b'existing video')
            with self.assertRaises(FileExistsError):
                publish_without_overwrite(completed, existing)
            self.assertEqual(existing.read_bytes(), b'existing video')
            self.assertEqual(completed.read_bytes(), b'new video')

    def test_other_series_claims_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            video = base / '01_Episode.mp4'
            video.write_bytes(b'video')
            store = LibraryStore(base / 'library.db')
            store.save('https://example.com/B', 'B', str(base), '', [{'number': 1, 'file': str(video)}])
            stored = {'episodes': [{'number': 1, 'file': str(video)}]}
            conflicts = conflicting_claims(store, 'https://example.com/A')
            self.assertEqual(registered_episode_paths(stored, 1, 'https://example.com/A', conflicts), [])

    def test_import_preview_verifies_numbered_videos(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '01_Episode.mp4').write_bytes(b'video')
            (base / '02_Episode.mp4').write_bytes(b'broken')
            (base / 'unidentified.mp4').write_bytes(b'video')
            rows = inspect_folder(base, lambda path: path.read_bytes() == b'video')
            self.assertEqual(sum(row.verified for row in rows), 1)
            self.assertTrue(any(row.status == 'No episode number' for row in rows))
            self.assertTrue(any(row.status == 'Verification failed' for row in rows))

    def test_nonempty_file_is_not_verified_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'episode.mp4'
            path.write_bytes(b'not a video')
            with patch('series_gui_all.shutil.which', return_value=None):
                self.assertFalse(video_is_playable(path))

    def test_incomplete_existing_video_does_not_pass_duration_check(self):
        info = {'format': {'duration': '80'}, 'streams': [{'codec_name': 'h264'}]}
        with patch('series_gui_all.shutil.which', return_value='ffprobe'), \
                patch('series_gui_all.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(info))):
            self.assertFalse(video_is_playable(Path('episode.mp4'), 100))
