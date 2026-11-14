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
; SprigCast holds this mutex while it runs (app/main.py): Setup and Uninstall ask you to close it first,
; so files aren't left behind and a running copy doesn't outlive its uninstall.
AppMutex=SprigCastAppMutex
CloseApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "camera"; Description: "Set up the SprigCast Camera, so Discord, Zoom and TikTok LIVE Studio can use SprigCast as a webcam (Windows asks for permission once)"; GroupDescription: "Virtual camera:"; Check: IsWindows11

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

[Code]
// The SprigCast Camera lives in Program Files and is registered machine-wide (the Windows Frame Server
// loads it), which needs administrator rights. The rest of SprigCast installs per user without them, so
// only this one step asks Windows for permission, and only when the camera is new or changed. The app's
// own Settings > Output → Virtual camera button stays as a repair path (output/virtualcam.py does the same).
const
  CameraClsid = '{6F3C5A0E-2B9D-4C6B-9F4E-5A1D7C2E8B31}';

function IsWindows11: Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major > 10) or ((Version.Major = 10) and (Version.Build >= 22000));
end;

function CameraDir: String;
begin
  Result := ExpandConstant('{commonpf64}\SprigCast Camera');
end;

function BundledCamera: String;
begin
  Result := ExpandConstant('{app}\python\Lib\site-packages\sprigcast\camera\sprigcast_camera.dll');
end;

function RegisteredCamera: String;
begin
  if not RegQueryStringValue(HKLM64, 'Software\Classes\CLSID\' + CameraClsid + '\InprocServer32', '', Result) then
    Result := '';
  if (Result <> '') and not FileExists(Result) then
    Result := '';
end;

function CameraIsCurrent: Boolean;
var
  Registered: String;
begin
  Registered := RegisteredCamera();
  Result := (Registered <> '') and FileExists(BundledCamera()) and
            (GetSHA256OfFile(Registered) = GetSHA256OfFile(BundledCamera()));
end;

function RunElevated(const Command: String): Boolean;
var
  Code: Integer;
begin
  // True once Windows started it (the user said Yes); ShellExec can't report cmd's own exit code.
  Result := ShellExec('runas', ExpandConstant('{cmd}'), '/c ' + Command, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure SetUpCamera;
var
  Dll: String;
begin
  if not FileExists(BundledCamera()) or CameraIsCurrent() then
    exit;
  Dll := CameraDir() + '\sprigcast_camera.dll';
  // A loaded DLL can't be overwritten: unregister first, rename the old file aside, then copy and register.
  if not RunElevated('mkdir "' + CameraDir() + '" 2>nul & regsvr32 /u /s "' + Dll + '" 2>nul & ' +
                     'move /y "' + Dll + '" "' + Dll + '.old" 2>nul & copy /y "' + BundledCamera() + '" "' + Dll +
                     '" && regsvr32 /s "' + Dll + '"') then
    Log('SprigCast Camera: not set up (permission declined or it failed); Settings > Output > Virtual camera can retry');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    exit;
  // A camera that's already set up is kept current on every upgrade, even if the box is unticked.
  if WizardIsTaskSelected('camera') or (RegisteredCamera() <> '') then
    SetUpCamera();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Registered: String;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  Registered := RegisteredCamera();
  if Registered <> '' then
    RunElevated('regsvr32 /u /s "' + Registered + '"');
end;
