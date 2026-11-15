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
Name: "desktopicon"; Description: "Desktop shortcut"
Name: "camera"; Description: "SprigCast Camera: use SprigCast as a webcam"

[Messages]
WizardSelectTasks=Extras
SelectTasksDesc=Pick what to add.
SelectTasksLabel2=

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
// The SprigCast Camera lives in Program Files and is registered machine-wide (a DirectShow camera every
// app can list, 64-bit and 32-bit, plus a Windows 11 media source), which needs administrator rights. The rest of SprigCast installs per user without them, so
// only this one step asks Windows for permission, and only when the camera is new or changed. The app's
// own Settings > Output > Virtual camera button stays as a repair path (output/virtualcam.py does the same).
const
  CameraClsid = '{6F3C5A0E-2B9D-4C6B-9F4E-5A1D7C2E8B31}';

function CameraDir: String;
begin
  Result := ExpandConstant('{commonpf64}\SprigCast Camera');
end;

function BundledCamera: String;
begin
  Result := ExpandConstant('{app}\python\Lib\site-packages\sprigcast\camera\sprigcast_camera.dll');
end;

function BundledCamera32: String;
begin
  Result := ExpandConstant('{app}\python\Lib\site-packages\sprigcast\camera\sprigcast_camera32.dll');
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
            (GetSHA256OfFile(Registered) = GetSHA256OfFile(BundledCamera())) and
            (not FileExists(BundledCamera32()) or
             (FileExists(CameraDir() + '\sprigcast_camera32.dll') and
              (GetSHA256OfFile(CameraDir() + '\sprigcast_camera32.dll') = GetSHA256OfFile(BundledCamera32()))));
end;

function RunElevated(const Command: String): Boolean;
var
  Code: Integer;
begin
  // True once Windows started it (the user said Yes); ShellExec can't report cmd's own exit code.
  Result := ShellExec('runas', ExpandConstant('{cmd}'), '/c ' + Command, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

// cmd steps that put one DLL in place and register it. A loaded DLL can't be overwritten: unregister
// first, rename the old file aside, then copy and register. regsvr32 runs the 32-bit one itself.
function InstallDll(const Src, Dll: String): String;
begin
  Result := 'regsvr32 /u /s "' + Dll + '" 2>nul & move /y "' + Dll + '" "' + Dll + '.old" 2>nul & ' +
            'copy /y "' + Src + '" "' + Dll + '" && regsvr32 /s "' + Dll + '"';
end;

procedure SetUpCamera;
var
  Command: String;
begin
  if not FileExists(BundledCamera()) or CameraIsCurrent() then
    exit;
  Command := 'mkdir "' + CameraDir() + '" 2>nul & ' + InstallDll(BundledCamera(), CameraDir() + '\sprigcast_camera.dll');
  if FileExists(BundledCamera32()) then
    Command := Command + ' & ' + InstallDll(BundledCamera32(), CameraDir() + '\sprigcast_camera32.dll');
  if not RunElevated(Command) then
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
    RunElevated('regsvr32 /u /s "' + Registered + '" & regsvr32 /u /s "' + CameraDir() + '\sprigcast_camera32.dll" 2>nul');
end;
