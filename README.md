# Series Downloader

A Windows desktop downloader for multi-episode series. The app uses the existing
Python/Tkinter download core with Playwright, yt-dlp, and FFmpeg.

## Run

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python series_gui_all.py
```

Use Python 3.12 or newer. Install FFmpeg and FFprobe on `PATH` for HLS downloads
and output verification.

## Episode controls

- Click **Scan Episodes** beside the URL field, or press Enter in that field, to
  load the episode list without starting downloads. Scanning checks registered
  existing files and labels missing episodes **Ready**. It applies the episode
  range when one is entered, supports Cancel, and saves the list to the library.
- Use **Select Pending** to select visible episodes that are not downloaded or
  manually marked downloaded. Review the selection, then click **Download Selected**.
  **Start Download** and **Run Queue** remain explicit actions that start downloads.
- Scanning does not create a download directory, write episode HTML snapshots,
  or visit every episode's player. Recognizable media and manifest requests from
  the page are blocked while its episode metadata is loaded. Stream URLs become
  available during media resolution when downloading, rather than during this scan.
- Paste one series URL into the main field or use **Add multiple URLs** for a queue.
- Enter an episode range such as `1,3-5` or leave it blank for all episodes.
- Select rows with the checkbox column, Shift-click, or the master checkbox heading.
- Use the batch bar or right-click menu to download, mark, copy links, set priority,
  export JSON/CSV/M3U8, or delete saved files.
- `Esc` deselects rows, `Ctrl+E` exports JSON, and `Shift+Delete` opens cleanup
  confirmation. Within confirmation, `Shift+Delete` commits the displayed choice.
- Enter in cleanup confirmation activates the focused button; Cancel receives the
  initial focus. Retry actions use the displayed series and its original save root.
- Priority changes affect order; each episode is navigated to and its available
  episode identity is checked before a stream is accepted.

## Existing videos

Before downloading an episode, the app checks its registered library path and the
source-owned series directory. Episode numbers are matched independently of the
display label, so a renamed label or an existing MKV/MOV/M4V/AVI/WebM/TS file can be
reused after FFprobe verification. Partial files and invalid-download backups are
excluded. Files assigned to a different source are not reused.

For older folders that are not registered, click **Existing videos...**, choose the
folder, verify the series URL, and select **Link selected videos**. The preview checks
video metadata and recognizes names such as `01_...`, `Episode 1`, `S01E01`, and
`第1集`. Choose one rendition per episode. Unnumbered or unverified files cannot be
linked automatically. Linking records the existing paths without moving the files.

Verified existing episodes appear as **Already downloaded** and are skipped. If an
existing output fails verification, it stays in place and a fresh download uses a
`.recovered` filename beside it. Final publication never overwrites a file that
already exists, including one created by another process during the download.
FFprobe is required; nonempty files alone are not accepted as verified videos.
Verification checks container/video metadata and available duration, rather than
decoding every frame.

## Library and download recovery

The Library button opens saved series from `series_library.sqlite3`. Episode titles,
statuses, paths, and priorities persist across restarts; the latest series is restored
at startup. Settings and manually marked episodes remain in `series_gui_settings.json`.

Download directories include a source ID and `.series-source.json` ownership record.
Series with identical titles use separate directories. Existing folders without an
ownership record are preserved; new downloads use a source-owned directory rather
than automatically adopting legacy files.

If the library is damaged or cannot be opened, the recovery screen provides read-only
diagnostics, a temporary session, or **Back up and rebuild**. Rebuild preserves the
original database and sidecars in a timestamped backup directory before creating an
empty library. A temporary session does not save library changes across restarts.

On-demand HLS downloads use yt-dlp's native fragment checkpoints. Cancellation keeps
`.part` and `.ytdl` files under `.series-partials` inside the series folder. Retrying
the same stream continues completed fragments. Different manifest URLs and codec
fallbacks use separate checkpoint directories so their data cannot be mixed. Expired
or changed signed URLs start a new checkpoint. Live or unsupported HLS features may
require FFmpeg fallback and cannot guarantee fragment resume.

Use **Partial downloads** to inspect checkpoint sizes and modification times. Select
only the checkpoints you want to recycle; cleanup is available while downloads are
idle and supports stopping after the current file. Removing a checkpoint makes that
download restart from the beginning. Completed media and unselected checkpoints are
excluded from cleanup.

HTTPS certificate verification is enabled for browser ingestion and downloads.
Untrusted titles are sanitized and resolved destinations must stay within the selected
save directory.
Literal yt-dlp template characters in titles and paths are escaped so they cannot
change the output location or filename.
The downloader and checkpoint cleanup both reject symlinks and Windows junctions
in checkpoint storage before operating on it.

## File cleanup

Cleanup defaults to the system Recycle Bin or Trash. The confirmation shows every
path and the total file size. Each result is reported separately; failed files
remain in place and can be retried. Windows Restart Manager identifies applications
that hold a file open when available. Permanent deletion requires clearing the
Recycle Bin option in the confirmation dialog. Moving files to the Recycle Bin does
not free disk space until the Recycle Bin is emptied.

Confirmation captures each file's identity, size, and modification timestamps.
Deletion checks that snapshot immediately before acting; a changed or replaced file
is left in place. Retrying a failed deletion opens a fresh confirmation dialog.

On Windows, recycling requires pywin32 and Send2Trash's IFileOperation backend,
including its recycling flags and operation callbacks. Missing dependencies or
recycling failures are reported without a fallback to permanent deletion.

The app does not detach arbitrary process handles or kill processes. Those operations
can corrupt the owning application or lose unsaved work. Close the listed application
and use **Retry failed** instead.

On macOS and Linux, Trash support uses `Send2Trash` from `requirements.txt`. The
Windows styling falls back to Tk's regular window surface on those platforms.

Library updates acquire SQLite's writer lock before reading and merging records,
so simultaneous saves preserve updates to different episodes. Duplicate episode
numbers in stored inventories are rejected and routed through startup recovery.

## Implementation status

This implements the existing desktop app's batch selection, context actions,
exports, priorities, persistent downloaded flags, batch URL entry, and file cleanup
flow. It is not yet a complete GA implementation of the PRD:

- Elevated handle detachment and process termination are not implemented.
- Deletion progress is reported per file. The displayed processing rate is file
  size divided by elapsed time, rather than measured disk write throughput.
- The interface remains Tkinter with Windows backdrop hints, rather than a full
  Fluent 2 / WinUI component implementation.

Validation: `python -m unittest discover -s tests -v`. The manual GUI benchmark is
`python tests/benchmark_gui.py`; one local 1,000-row run measured 37 ms for selection
and 29 ms for filtering. These measurements are not a cross-platform performance
guarantee.
