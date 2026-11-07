# Building a Windows release

## Requirements

- Windows x64 and 64-bit Python 3.12 (including Tcl/Tk).
- FFmpeg's vendor distribution, with `bin/ffmpeg.exe`, `bin/ffprobe.exe`, `LICENSE`,
  and `README.txt`. The local beta uses Gyan's 9.0.2 full build already installed
  on the build machine. Keep the exact vendor archive and corresponding source
  material when preparing a public distribution.
- Internet access for Python dependencies and browser/compiler downloads.
- Several GB of free build space.

## Build

From PowerShell in the repository:

```powershell
.\scripts\build_release.ps1
```

If the script execution policy prevents running a local build script:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

To specify the FFmpeg vendor folder or an existing Inno Setup compiler:

```powershell
.\scripts\build_release.ps1 -FFmpegRoot 'C:\tools\ffmpeg-9.0.2-full_build' -ISCC 'C:\tools\Inno Setup\ISCC.exe'
```

The wrapper creates an isolated `.venv-release` and installs exact versions from
`requirements-build.txt` and `requirements-release.txt`. It does not change the
development environment. The builder runs the regression suite, installs the
pinned Chromium revisions, collects vendor/license files, builds a windowed
PyInstaller folder bundle, and runs offline diagnostics against that executable.
The diagnostic environment removes Python and FFmpeg from PATH and supplies an
invalid external browser cache to verify that included dependencies are used.

Unless `-ISCC` is supplied, it downloads Inno Setup 7.1.1 from the official GitHub
release, verifies its pinned SHA-256, and installs the compiler as the current
user under `build/tools/innosetup-7.1.1`. This registers the compiler's own
uninstaller/file association. It does not require administrator rights.

The resulting app installer uses a stable AppId, installs per user under
`%LOCALAPPDATA%\Programs\SeriesDownloader`, creates a Start menu shortcut, and
supports upgrades and uninstall. User profiles and downloaded media stay outside
the installation folder. The ZIP build also uses this same persistent profile.

Outputs are in `release/<version>/`:

- `SeriesDownloader-<version>-windows-x64-setup.exe`
- `SeriesDownloader-<version>-windows-x64.zip`
- `SHA256SUMS.txt`, quick start, changelog, and third-party notices
- `SMOKE_TEST.json`, including browser, GUI, media-tool, and HLS resume checks

`dist/SeriesDownloader` is the unpacked application. Logs and intermediate files
are under `build/`. Neither output directory nor `.venv-release` is committed.

## Verification

The build fails on regression or frozen-app test failures. Its offline smoke test
opens both browser modes, constructs the GUI with bundled icons, creates local
HLS media, launches an actual frozen download worker, cancels it, and resumes
without requesting its first completed segment again. FFprobe verifies the result.
It also moves only its own disposable diagnostic file to the Recycle Bin.
This establishes packaging and local download behavior; it does not test every
website or prove compatibility on a separate clean Windows machine.

Regression results are reused only while the application/test source, icons,
Python version, pinned requirements, and saved passing log are unchanged. To force
a fresh regression run, remove `build/regression-check.json` before rebuilding.

To exercise the setup EXE, run:

```powershell
python scripts/verify_installer.py
```

This installs into a temporary folder under `build/`, runs the installed app's
offline diagnostics, reinstalls it, and uninstalls it. It verifies a synthetic
profile remains byte-for-byte unchanged. It refuses to replace an existing
registered installation. This check creates/removes the test installation's
Start menu shortcut and uninstall registration. Detailed logs remain under
`build/installer-check-*`; a passing report is saved as `INSTALLER_TEST.json`.
If you add that report after the build, regenerate `SHA256SUMS.txt` before sharing.

You can rerun diagnostics without opening the normal app:

```powershell
$env:SERIES_DOWNLOADER_DATA_DIR = "$env:TEMP\series-downloader-diagnostics"
$check = Start-Process -FilePath '.\dist\SeriesDownloader\SeriesDownloader.exe' -ArgumentList '--self-test', '--report', "$env:TEMP\series-downloader-check.json" -WindowStyle Hidden -Wait -PassThru
$check.ExitCode
Remove-Item Env:SERIES_DOWNLOADER_DATA_DIR
Get-Content "$env:TEMP\series-downloader-check.json"
```

Before calling a release stable, test on a clean Windows machine: install,
scan/download a permitted series, cancel/resume, restart, upgrade with a saved
library, and uninstall while preserving the profile and downloads.

## Public release

This task produces an **unsigned local beta**. It does not publish or upload
anything. For public distribution:

1. Choose the application's license and satisfy bundled dependency source and
   notice requirements. See `THIRD_PARTY_NOTICES.md`, especially FFmpeg's GPL
   distribution requirements.
2. Update `app_version.py`, the changelog, and quick-start version. Rebuild and
   complete clean-machine acceptance checks.
3. Sign the application executable with the publisher's code-signing certificate
   before compiling the installer/ZIP, then sign the installer and timestamp both
   signatures. Verify signatures and regenerate checksums after signing.
4. Commit the reviewed source and tag its version, for example
   `v0.1.0-beta.1`. Publish the installer, ZIP, checksums, and release notes together.
   Retain the previous release and back up profiles before database migrations.

Implementation references: [Playwright packaging](https://playwright.dev/python/docs/library#pyinstaller),
[PyInstaller multiprocessing](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#multi-processing),
[Inno Setup](https://jrsoftware.org/isinfo.php), and
[Gyan FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/).
