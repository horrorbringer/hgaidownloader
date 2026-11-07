param(
    [string]$Python = "python",
    [string]$FFmpegRoot = "",
    [string]$ISCC = ""
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    $releasePython = Join-Path $projectRoot ".venv-release\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $releasePython)) {
        & $Python -m venv .venv-release
        if ($LASTEXITCODE -ne 0) { throw "Could not create the release environment" }
    }
    & $releasePython -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Release dependency installation failed" }
    $buildArguments = @("scripts/build_release.py")
    if ($FFmpegRoot) { $buildArguments += @("--ffmpeg-root", $FFmpegRoot) }
    if ($ISCC) { $buildArguments += @("--iscc", $ISCC) }
    & $releasePython @buildArguments
    if ($LASTEXITCODE -ne 0) { throw "Release build failed" }
} finally {
    Pop-Location
}
