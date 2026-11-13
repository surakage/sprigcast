; SprigCast installer (Inno Setup 6). Built by build_windows.ps1, which passes AppVersion and BuildDir.
; Installs for the current user only, so it needs no administrator rights.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F0D2B1E-5C83-4E8B-9D57-3A2C5F1B7E44}
AppName=SprigCast
AppVersion={#AppVersion}
AppPublisher=Slothbloom
DefaultDirName={localappdata}\Programs\SprigCast
DefaultGroupName=SprigCast
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=SprigCast-Setup-{#AppVersion}
SetupIconFile={#BuildDir}\sprigcast.ico
UninstallDisplayIcon={app}\sprigcast.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#BuildDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\sprigcast.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\SprigCast"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m sprigcast.app.main"; IconFilename: "{app}\sprigcast.ico"; WorkingDir: "{app}"
Name: "{autodesktop}\SprigCast"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m sprigcast.app.main"; IconFilename: "{app}\sprigcast.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m sprigcast.app.main"; Description: "Start SprigCast"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the "Start with Windows" entry, if it was switched on, so nothing points at a removed program.
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v SprigCast /f"; Flags: runhidden; RunOnceId: "RemoveAutostart"
