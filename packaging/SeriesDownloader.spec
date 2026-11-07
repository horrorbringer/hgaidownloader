# Run through scripts/build_release.py so dependency paths are explicit.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project = Path(SPECPATH).parent
staging = Path(os.environ["SERIES_BUILD_STAGING"])
datas = [
    (str(project / "icons"), "icons"),
    (str(staging / "browsers"), "browsers"),
    (str(staging / "media-tools"), "media-tools"),
    (str(staging / "licenses"), "licenses"),
]
datas += collect_data_files("yt_dlp")
analysis = Analysis(
    [str(project / "app_launcher.py")],
    pathex=[str(project)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("yt_dlp", filter=lambda name: not name.startswith("yt_dlp.__pyinstaller"))
                  + ["win32com.shell.shell", "win32com.shell.shellcon"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True,
    name="SeriesDownloader", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    icon=str(staging / "application.ico"),
    version=str(staging / "version-info.txt"),
)
collection = COLLECT(executable, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name="SeriesDownloader")
