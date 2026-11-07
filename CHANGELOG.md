# Changelog

## 0.1.0-beta.1

- First Windows x64 packaged preview, with an installer and a ZIP distribution.
- Includes Chromium, FFmpeg, FFprobe, Python, and the desktop application's dependencies.
- Settings, library, and logs are stored under the user's application data directory.
- Existing source-folder settings and library are copied on first source launch;
  originals are preserved and existing destination data is never overwritten.
- Downloads default to `Downloads\SeriesDownloader` in the user's home directory.
- Frozen download worker startup is supported without opening duplicate windows.

This is an unsigned beta. Website compatibility depends on the source website.
