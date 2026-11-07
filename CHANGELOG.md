# Changelog

## 0.1.0-beta.2

- Automatically checks public GitHub releases in the background at startup.
- Adds an Updates window with manual checks, cancellable downloads, progress,
  release notes, and an explicit Install and restart action.
- Verifies installer size, SHA-256 checksums, and optional GitHub asset digests;
  rechecks the downloaded file before executing it without a shell.
- Pins update URLs to this repository and restricts HTTPS redirects to GitHub's
  download hosts. Draft, incomplete, older, and inappropriate-channel releases
  are not offered as updates.
- Blocks installation during downloads/cleanup or when profile saving fails.
- Preserves existing per-user installation paths and restarts the updated app.

## 0.1.0-beta.1

- First Windows x64 packaged preview, with an installer and a ZIP distribution.
- Includes Chromium, FFmpeg, FFprobe, Python, and the desktop application's dependencies.
- Settings, library, and logs are stored under the user's application data directory.
- Existing source-folder settings and library are copied on first source launch;
  originals are preserved and existing destination data is never overwritten.
- Downloads default to `Downloads\SeriesDownloader` in the user's home directory.
- Frozen download worker startup is supported without opening duplicate windows.

This is an unsigned beta. Website compatibility depends on the source website.
