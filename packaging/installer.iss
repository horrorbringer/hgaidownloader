#ifndef AppVersion
  #error AppVersion must be supplied by the release builder
#endif

[Setup]
AppId={{A6C49B58-44DF-41DB-8FBA-6891C6657364}
AppName=Series Downloader
AppVersion={#AppVersion}
AppVerName=Series Downloader {#AppVersion}
VersionInfoVersion={#NumericVersion}
DefaultDirName={localappdata}\Programs\SeriesDownloader
DefaultGroupName=Series Downloader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#ReleaseDir}
OutputBaseFilename=SeriesDownloader-{#AppVersion}-windows-x64-setup
SetupIconFile={#BuildIcon}
UninstallDisplayIcon={app}\SeriesDownloader.exe
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=SeriesDownloader.exe
RestartApplications=no
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Series Downloader"; Filename: "{app}\SeriesDownloader.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Series Downloader"; Filename: "{app}\SeriesDownloader.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\SeriesDownloader.exe"; Description: "Launch Series Downloader"; Flags: nowait postinstall skipifsilent

; User profiles and downloaded media are deliberately outside {app} and remain
; in place during upgrades and uninstall. Do not add [UninstallDelete] entries.
