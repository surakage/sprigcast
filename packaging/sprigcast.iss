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
AppVerName=SprigCast {#AppVersion}
AppCopyright=Slothbloom
AppPublisherURL=https://github.com/surakage/sprigcast
AppSupportURL=https://github.com/surakage/sprigcast/issues
AppUpdatesURL=https://github.com/surakage/sprigcast/releases
UninstallDisplayName=SprigCast
VersionInfoVersion={#AppVersion}
VersionInfoProductName=SprigCast
VersionInfoDescription=SprigCast Setup
; Brand art drawn by make_wizard_images.py, one file per screen scaling (Setup picks the closest).
WizardImageFile={#BuildDir}\wizard\side-*.bmp
WizardSmallImageFile={#BuildDir}\wizard\top-*.bmp
; Welcome, folder, Extras, then straight to installing: Extras' Next button reads Install.
DisableWelcomePage=no
DisableReadyPage=yes

[Messages]
SetupWindowTitle=%1 Setup
WelcomeLabel1=Welcome to SprigCast
WelcomeLabel2=A light, open-source studio for streaming and recording, with horizontal and vertical canvases at once.%n%nVersion {#AppVersion}
WizardSelectDir=Install location
SelectDirDesc=Where SprigCast goes.
SelectDirLabel3=SprigCast installs for your account only, so it needs no administrator rights.
SelectDirBrowseLabel=Click Next to use this folder, or Browse to pick another.
WizardInstalling=Installing
InstallingLabel=Setting up SprigCast. This takes a moment.
FinishedHeadingLabel=SprigCast is ready
FinishedLabelNoIcons=SprigCast is installed.
FinishedLabel=SprigCast is installed. Find it in the Start menu anytime.
ClickFinish=

[Files]
Source: "{#BuildDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\sprigcast.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\SprigCast"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m sprigcast.app.main"; IconFilename: "{app}\sprigcast.ico"; WorkingDir: "{app}"
Name: "{autodesktop}\SprigCast"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m sprigcast.app.main"; IconFilename: "{app}\sprigcast.ico"; WorkingDir: "{app}"; Check: WantsDesktopShortcut

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m sprigcast.app.main"; Description: "Start SprigCast now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the "Start with Windows" entry, if it was switched on, so nothing points at a removed program.
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v SprigCast /f"; Flags: runhidden; RunOnceId: "RemoveAutostart"

[Code]
// ---- Look: the Night Canopy header, the Extras page and the Finish page link ----
const
  HeaderColor = $0011130E;  // #0e1311, matches make_wizard_images.py HEADER
  HeaderText = $00F3F7F2;   // #f2f7f3
  HeaderMuted = $00929A8B;  // #8b9a92
  NoteColor = $00666666;
  LinkColor = $00436B1D;    // #1d6b43, brand dark green (reads on white)
  ChangelogUrl = 'https://github.com/surakage/sprigcast/blob/main/CHANGELOG.md';

var
  ExtrasPage: TWizardPage;
  DesktopBox, CameraBox: TNewCheckBox;

function WantsDesktopShortcut: Boolean;
begin
  Result := DesktopBox.Checked;
end;

function WantsCamera: Boolean;
begin
  Result := CameraBox.Checked;
end;

// One Extras option: a bold check box with a short grey line under it. Returns the next free top.
function AddOption(var Box: TNewCheckBox; const Caption, Detail: String; Top: Integer; Checked: Boolean): Integer;
var
  Note: TNewStaticText;
begin
  Box := TNewCheckBox.Create(ExtrasPage);
  Box.Parent := ExtrasPage.Surface;
  Box.Caption := Caption;
  Box.Font.Style := [fsBold];
  Box.Top := Top;
  Box.Width := ExtrasPage.SurfaceWidth;
  Box.Height := ScaleY(20);
  Box.Checked := Checked;

  Note := TNewStaticText.Create(ExtrasPage);
  Note.Parent := ExtrasPage.Surface;
  Note.Left := ScaleX(20);
  Note.Top := Box.Top + Box.Height + ScaleY(2);
  Note.Width := ExtrasPage.SurfaceWidth - Note.Left;
  Note.AutoSize := False;
  Note.WordWrap := True;
  Note.Font.Color := NoteColor;
  Note.Caption := Detail;
  Note.AdjustHeight();
  Result := Note.Top + Note.Height + ScaleY(18);
end;

procedure OpenChangelog(Sender: TObject);
var
  Code: Integer;
begin
  ShellExec('open', ChangelogUrl, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure InitializeWizard;
var
  Top: Integer;
  Link: TNewStaticText;
begin
  // A dark header band with the app icon, like the app itself.
  WizardForm.MainPanel.Color := HeaderColor;
  WizardForm.PageNameLabel.Font.Color := HeaderText;
  WizardForm.PageDescriptionLabel.Font.Color := HeaderMuted;

  if WizardForm.PrevAppDir <> '' then
  begin
    WizardForm.WelcomeLabel1.Caption := 'Update SprigCast';
    WizardForm.WelcomeLabel2.Caption := 'Version {#AppVersion}. Your blooms, sprigs and settings stay as they are.';
  end;

  ExtrasPage := CreateCustomPage(wpSelectDir, 'Extras', 'Pick what to add.');
  Top := ScaleY(4);
  Top := AddOption(DesktopBox, 'Desktop shortcut', 'Start SprigCast from your desktop.', Top,
                   GetPreviousData('Desktop', '1') = '1');
  AddOption(CameraBox, 'SprigCast Camera',
            'Use SprigCast as a webcam in Zoom, Discord, TikTok LIVE Studio and other apps. ' +
            'Windows asks for permission once. You can also add it later in Settings.', Top,
            GetPreviousData('Camera', '1') = '1');

  // A "What's new" link under the Finish text.
  Link := TNewStaticText.Create(WizardForm);
  Link.Parent := WizardForm.FinishedPage;
  Link.Left := WizardForm.FinishedLabel.Left;
  Link.Top := WizardForm.FinishedPage.Height - ScaleY(36);
  Link.Caption := 'What''s new in {#AppVersion}';
  Link.Font.Color := LinkColor;
  Link.Font.Style := [fsUnderline];
  Link.Cursor := crHand;
  Link.OnClick := @OpenChangelog;
end;

function OneOrZero(Value: Boolean): String;
begin
  if Value then Result := '1' else Result := '0';
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'Desktop', OneOrZero(DesktopBox.Checked));
  SetPreviousData(PreviousDataKey, 'Camera', OneOrZero(CameraBox.Checked));
end;

// ---- SprigCast Camera ----
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
  if WantsCamera() or (RegisteredCamera() <> '') then
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
