# Third-party software

The release contains independent third-party software. Its license terms and
copyright notices continue to apply. Collected license files are in
`_internal/licenses`; browser notices are retained with the browser files.

| Software | Upstream / source | License |
| --- | --- | --- |
| Python | https://www.python.org/ | PSF and included notices |
| Tcl / Tk | https://www.tcl-lang.org/ | Tcl / Tk licenses |
| Playwright and driver | https://github.com/microsoft/playwright-python and https://github.com/microsoft/playwright | Apache-2.0 and driver third-party notices |
| Chromium | https://chromium.googlesource.com/chromium/src/ | BSD and included third-party licenses |
| yt-dlp | https://github.com/yt-dlp/yt-dlp | Unlicense; included dependencies retain their own terms |
| Send2Trash | https://github.com/arsenetar/send2trash | BSD-3-Clause |
| pywin32 | https://github.com/mhammond/pywin32 | PSF license |
| greenlet | https://github.com/python-greenlet/greenlet | MIT / PSF |
| pyee | https://github.com/jfhbrook/pyee | MIT |
| typing_extensions | https://github.com/python/typing_extensions | PSF |
| PyInstaller bootloader | https://github.com/pyinstaller/pyinstaller | GPL with distribution exception |
| FFmpeg and FFprobe | https://ffmpeg.org/ and https://www.gyan.dev/ffmpeg/builds/ | See the bundled build's LICENSE and README |

The build records installed Python package versions and the FFmpeg configuration
in `BUILD_INFO.json`. The unmodified FFmpeg vendor LICENSE and README are included
in `_internal/media-tools`. That README identifies its exact source revision and
external library versions. FFmpeg is invoked as a separate executable.

This is a local beta distribution. Before public redistribution, provide the
complete corresponding source and build materials required by the licenses of
the selected FFmpeg build and other copyleft components; upstream links alone
are not a substitute for those obligations. A future public release also needs
an explicit license choice for the application itself.
