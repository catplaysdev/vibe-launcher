; Inno Setup script for Vibe Launcher (build with packaging/build.ps1, or: ISCC /DAppVersion=1.2.3 /DSourceDir=<folder> installer.iss).
; Per-user install (no administrator rights), so the launcher's own updater can run it silently.
; User data lives in %LOCALAPPDATA%\VibeLauncher and is deliberately left alone by install, update and uninstall.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist-app\Vibe Launcher"
#endif
#define AppName "Vibe Launcher"
#define AppExe "Vibe Launcher.exe"

[Setup]
AppId={{6B1F6A52-3C0E-4A57-9D0C-5E7D2B8A41F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Vibe Launcher contributors
AppPublisherURL=https://github.com/catplaysdev/vibe-launcher
AppSupportURL=https://github.com/catplaysdev/vibe-launcher/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
LicenseFile=..\LICENSE
SetupIconFile=vibe-launcher.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist-app\installer
OutputBaseFilename=Vibe-Launcher-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=Vibe Launcher.exe,vibe-launcher-cli.exe
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
// An update started from inside the launcher runs silently: start the new version once the files are in place.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and WizardSilent then
    ExecAsOriginalUser(ExpandConstant('{app}\{#AppExe}'), '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;
