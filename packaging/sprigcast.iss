; SprigCast installer (Inno Setup 6.7 or newer, for its dark wizard). Built by build_windows.ps1, which passes AppVersion and BuildDir.
; Installs for the current user only, so it needs no administrator rights.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppShown
  #define AppShown AppVersion
#endif
; SprigCast holds this mutex while it runs (APP_MUTEX in app/main.py).
#define AppMutexName "SprigCastAppMutex"

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
; Dark all the way through, in the app's Night Canopy colours, with no white page or divider lines.
WizardStyle=modern dark hidebevels
WizardBackColor=#0e1311
WizardImageBackColor=#0e1311
WizardSmallImageBackColor=#0e1311
; While SprigCast runs, Setup and Uninstall ask you to close it first, so files aren't left behind and a running
; copy doesn't outlive its uninstall. A silent Setup closes it itself (InitializeSetup).
AppMutex={#AppMutexName}
CloseApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppVerName=SprigCast {#AppShown}
AppCopyright=Slothbloom
AppPublisherURL=https://sprigcast.slothbloom.com
AppSupportURL=https://discord.gg/fgMD33XrhM
AppUpdatesURL=https://sprigcast.slothbloom.com
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
WelcomeLabel2=A light, open-source studio for streaming and recording, with horizontal and vertical canvases at once.%n%n{#AppShown}
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
ClickNext=

[InstallDelete]
; An update clears out the SprigCast code before copying the new one in, so files taken out of SprigCast
; (a retired store plugin such as Auto clip, a removed module) don't linger from the last version: the store
; lists every plugin file it finds. Only SprigCast's own Python and art go; the camera folder stays (its
; DLL may be in use, #272) and so does everything else in {app}. tests/test_packaging.py keeps this list in step.
Type: files; Name: "{app}\python\Lib\site-packages\sprigcast\*.py"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\__pycache__"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\app"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\audio"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\chat"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\control"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\core"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\data"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\output"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\render"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\sources"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\store"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\sprigcast\tools"

[Files]
Source: "{#BuildDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BuildDir}\sprigcast.ico"; DestDir: "{app}"; Flags: ignoreversion
; The licenses (packaging/notices.py) also at the top of SprigCast's folder, where people look for them. The copies
; in python\ stay: Help → About → Licenses opens that one.
Source: "{#BuildDir}\python\LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BuildDir}\python\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\SprigCast"; Filename: "{app}\python\SprigCast.exe"; IconFilename: "{app}\sprigcast.ico"; WorkingDir: "{app}"
Name: "{autodesktop}\SprigCast"; Filename: "{app}\python\SprigCast.exe"; IconFilename: "{app}\sprigcast.ico"; WorkingDir: "{app}"; Check: WantsDesktopShortcut

[Run]
Filename: "{app}\python\SprigCast.exe"; Description: "Start SprigCast now"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
; An update from inside SprigCast (app/updates.py) runs Setup silently with /restartapp=1: start it again
; once everything, the camera step included, is done.
Filename: "{app}\python\SprigCast.exe"; WorkingDir: "{app}"; Flags: nowait postinstall skipifnotsilent; Check: RestartAfterUpdate

[UninstallRun]
; Remove the "Start with Windows" entry, if it was switched on, so nothing points at a removed program.
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v SprigCast /f"; Flags: runhidden; RunOnceId: "RemoveAutostart"
; The capture helper can outlive a closed SprigCast for a moment; stop it so its files can be removed.
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im SprigCastCapture.exe"; Flags: runhidden; RunOnceId: "StopCaptureHelper"

[UninstallDelete]
; Uninstall removes only the files Setup copied, so files SprigCast made later in its own folder (Python's
; compiled __pycache__ files, mostly) kept {app} alive and the next Setup asked "Folder exists". {app}\python
; is always SprigCast's own, so it goes whole; {app} itself goes only once empty, so nothing of yours
; is touched if you installed into a folder that holds other things. Settings, gardens and logs live
; elsewhere and go only when "Also delete my SprigCast data" is ticked.
Type: filesandordirs; Name: "{app}\python"
Type: dirifempty; Name: "{app}"

[Code]
// ---- Look: the Night Canopy header, the Extras page and the Finish page link ----
const
  HeaderColor = $0011130E;  // #0e1311, matches WizardBackColor and make_wizard_images.py PAGE
  HeaderText = $00F3F7F2;   // #f2f7f3
  HeaderMuted = $00929A8B;  // #8b9a92
  NoteColor = $00929A8B;    // #8b9a92, muted text on the dark page
  LinkColor = $008FD84F;    // #4fd88f, the app's accent green
  ChangelogUrl = 'https://sprigcast.slothbloom.com/changelog';

var
  ExtrasPage: TWizardPage;
  DesktopBox, CameraBox: TNewCheckBox;

function WantsDesktopShortcut: Boolean;
begin
  Result := DesktopBox.Checked;
end;

function RestartAfterUpdate: Boolean;
begin
  Result := ExpandConstant('{param:restartapp|0}') = '1';
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
    WizardForm.WelcomeLabel2.Caption := '{#AppShown}. Your blooms, sprigs and settings stay as they are.';
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

// No Ready page, so Extras is the last step before installing: its button says Install.
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = ExtrasPage.ID then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
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
  // The DirectShow camera apps list, under DirectShow's video capture category (both bitnesses).
  CameraListing = 'Software\Classes\CLSID\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\Instance\{622E03C7-86C4-4F91-82C0-6EB79B42DCBF}';

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

function CameraListed: Boolean;
begin
  Result := RegKeyExists(HKLM64, CameraListing) and
            (not FileExists(BundledCamera32()) or RegKeyExists(HKLM32, CameraListing));
end;

// Set up once and kept: registered, listed for 64-bit and 32-bit apps, and the same files this Setup ships.
// An update with the same camera changes nothing; a new camera, or a half-done one, is (re)done.
function CameraIsCurrent: Boolean;
var
  Registered: String;
begin
  Registered := RegisteredCamera();
  Result := (Registered <> '') and CameraListed() and FileExists(BundledCamera()) and
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

// cmd steps that put one DLL in place and register it (the same steps as output/virtualcam.py). A
// loaded DLL can't be overwritten or deleted, only renamed: clear leftovers no app still holds, stage the
// new copy, rename the current DLL aside under a fresh name (a fixed ".old" may itself still be loaded,
// which left the old 64-bit DLL in place), move the new one in, and always register what's there.
function InstallDll(const Src, Dll, Stamp: String): String;
begin
  Result := 'del /q "' + Dll + '.*.old" "' + Dll + '.old" 2>nul & ' +
            'copy /y "' + Src + '" "' + Dll + '.new" && (move /y "' + Dll + '" "' + Dll + '.' + Stamp + '.old" 2>nul & ' +
            'move /y "' + Dll + '.new" "' + Dll + '") & regsvr32 /s "' + Dll + '"';
end;

procedure SetUpCamera;
var
  Command, Stamp: String;
begin
  if not FileExists(BundledCamera()) or CameraIsCurrent() then
    exit;
  // An install nobody is watching (a test run: /VERYSILENT /NOCAMERA=1) can't answer Windows' permission prompt:
  // it waited about two minutes for it and then went on (PC 2026-11-21). The in-app updater passes it too (it sat
  // 2 minutes there, 2026-11-27): starting Virtual Cam asks for a changed camera with context instead.
  if ExpandConstant('{param:NOCAMERA|0}') <> '0' then
  begin
    Log('SprigCast Camera: not set up (/NOCAMERA=1); Settings > Output > Virtual camera can set it up');
    exit;
  end;
  Stamp := GetDateTimeString('yyyymmddhhnnss', #0, #0);
  Command := 'mkdir "' + CameraDir() + '" 2>nul & ' + InstallDll(BundledCamera(), CameraDir() + '\sprigcast_camera.dll', Stamp);
  if FileExists(BundledCamera32()) then
    Command := Command + ' & ' + InstallDll(BundledCamera32(), CameraDir() + '\sprigcast_camera32.dll', Stamp);
  if not RunElevated(Command) then
  begin
    Log('SprigCast Camera: not set up (permission declined); Settings > Output > Virtual camera can retry');
    exit;
  end;
  // Check the result: both cameras registered and current. Say so if not, rather than leave it half done.
  if not CameraIsCurrent() then
  begin
    Log('SprigCast Camera: set up incomplete (registered: ' + RegisteredCamera() + ')');
    SuppressibleMsgBox('SprigCast Camera couldn''t be fully set up. Close apps that use cameras, then use ' +
                       'Settings > Output > Virtual camera in SprigCast to finish.', mbError, MB_OK, IDOK);
  end;
end;

// ---- An update deletes the files the last version installed and this one doesn't (OBS's removed_files) ----
var
  OldFiles: TStringList;

function FileList(): String;
begin
  Result := ExpandConstant('{app}\python\sprigcast-files.txt');
end;

// A safe relative path inside the python folder (never absolute, never ..).
function SafeListed(Name: String): Boolean;
begin
  Result := (Name <> '') and (Pos('..', Name) = 0) and (Pos(':', Name) = 0) and (Name[1] <> '\') and
            (Name[1] <> '/');
end;

procedure RemoveDroppedFiles();
var
  NewFiles: TStringList;
  I, Gone: Integer;
  Name: String;
begin
  if (OldFiles = nil) or not FileExists(FileList()) then
    exit;
  NewFiles := TStringList.Create;
  try
    NewFiles.LoadFromFile(FileList());
    NewFiles.Sorted := True;  // IndexOf then searches by halves, ignoring case as Windows does
    Gone := 0;
    for I := 0 to OldFiles.Count - 1 do
    begin
      Name := Trim(OldFiles[I]);
      if SafeListed(Name) and (NewFiles.IndexOf(Name) < 0) then
        // A file in use (the camera DLL inside another app) can't go now: it's left, as before.
        if DeleteFile(ExpandConstant('{app}\python\') + Name) then
          Gone := Gone + 1;
    end;
    Log(Format('Update: %d files the last version had and this one doesn''t were removed', [Gone]));
  finally
    NewFiles.Free;
    OldFiles.Free;
    OldFiles := nil;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    // Before anything is copied: what the version being replaced installed (none on a first install)
    if FileExists(FileList()) then
    begin
      OldFiles := TStringList.Create;
      OldFiles.LoadFromFile(FileList());
    end;
    exit;
  end;
  if CurStep <> ssPostInstall then
    exit;
  RemoveDroppedFiles();
  // A camera that's already set up is kept current on every upgrade, even if the box is unticked.
  if WantsCamera() or (RegisteredCamera() <> '') then
    SetUpCamera();
end;

// ---- Uninstall: keep your settings for next time, or delete everything ----
var
  DeleteData: Boolean;

// A silent install (the in-app updater, a script) can't answer "SprigCast is running": with its message boxes
// suppressed the AppMutex check cancels Setup. So a silent Setup closes SprigCast itself first: asked nicely,
// then made to after 20 s. Setup checks AppMutex after this returns. A normal install still asks you.
function SprigCastRunning: Boolean;
begin
  Result := CheckForMutexes('{#AppMutexName}');
end;

procedure WaitWhileRunning(Tenths: Integer);
begin
  while SprigCastRunning and (Tenths > 0) do
  begin
    Sleep(100);
    Tenths := Tenths - 1;
  end;
end;

function InitializeSetup: Boolean;
var
  Code: Integer;
begin
  Result := True;
  if not (WizardSilent and SprigCastRunning) then
    Exit;
  Log('SprigCast is running: closing it for a silent install');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/im SprigCast.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  WaitWhileRunning(200);
  if SprigCastRunning then
  begin
    Log('SprigCast didn''t close in 20 s: ending it');
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /t /im SprigCast.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
    WaitWhileRunning(100);
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im SprigCastCapture.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

// Asks once, before anything is removed. Unticked (the default) keeps settings and blooms, so a reinstall
// picks up where you left off. A silent uninstall keeps them too. Keys and sign-ins always go.
function InitializeUninstall: Boolean;
var
  Form: TSetupForm;
  Intro, Note: TNewStaticText;
  Box: TNewCheckBox;
  Ok, Cancel: TNewButton;
begin
  Result := True;
  DeleteData := False;
  if UninstallSilent then
    exit;
  Form := CreateCustomForm(ScaleX(440), ScaleY(190), False, True);
  try
    Form.Caption := 'Uninstall SprigCast';
    Form.Color := HeaderColor;

    Intro := TNewStaticText.Create(Form);
    Intro.Parent := Form;
    Intro.Left := ScaleX(20);
    Intro.Top := ScaleY(20);
    Intro.Width := Form.ClientWidth - ScaleX(40);
    Intro.AutoSize := False;
    Intro.WordWrap := True;
    Intro.Height := ScaleY(34);
    Intro.Font.Color := HeaderText;
    Intro.Caption := 'SprigCast and its saved stream keys and sign-ins will be removed. Your settings stay, so a reinstall picks up where you left off.';

    Box := TNewCheckBox.Create(Form);
    Box.Parent := Form;
    Box.Left := ScaleX(20);
    Box.Top := Intro.Top + Intro.Height + ScaleY(12);
    Box.Width := Form.ClientWidth - ScaleX(40);
    Box.Height := ScaleY(20);
    Box.Font.Color := HeaderText;
    Box.Caption := 'Also delete my SprigCast data';
    Box.Checked := False;

    Note := TNewStaticText.Create(Form);
    Note.Parent := Form;
    Note.Left := ScaleX(38);
    Note.Top := Box.Top + Box.Height + ScaleY(2);
    Note.Width := Form.ClientWidth - ScaleX(58);
    Note.AutoSize := False;
    Note.WordWrap := True;
    Note.Height := ScaleY(34);
    Note.Font.Color := NoteColor;
    Note.Caption := 'Settings, blooms, plugins, pairings and logs. Your recordings and screenshots stay.';

    Ok := TNewButton.Create(Form);
    Ok.Parent := Form;
    Ok.Caption := 'Uninstall';
    Ok.ModalResult := mrOk;
    Ok.Default := True;
    Ok.Width := ScaleX(90);
    Ok.Height := ScaleY(26);
    Ok.Top := Form.ClientHeight - Ok.Height - ScaleY(16);
    Ok.Left := Form.ClientWidth - ScaleX(20) - 2 * Ok.Width - ScaleX(8);

    Cancel := TNewButton.Create(Form);
    Cancel.Parent := Form;
    Cancel.Caption := 'Cancel';
    Cancel.ModalResult := mrCancel;
    Cancel.Cancel := True;
    Cancel.Width := Ok.Width;
    Cancel.Height := Ok.Height;
    Cancel.Top := Ok.Top;
    Cancel.Left := Form.ClientWidth - ScaleX(20) - Cancel.Width;

    Form.ActiveControl := Ok;
    Result := Form.ShowModal() = mrOk;
    DeleteData := Result and Box.Checked;
  finally
    Form.Free();
  end;
end;

procedure DeleteSprigCastData;
begin
  // Settings, blooms, plugins, models, pairings and the web page cache.
  DelTree(ExpandConstant('{localappdata}\Slothbloom\SprigCast'), True, True, True);
  DelTree(ExpandConstant('{userappdata}\Slothbloom\SprigCast'), True, True, True);
  RemoveDir(ExpandConstant('{localappdata}\Slothbloom'));
  RemoveDir(ExpandConstant('{userappdata}\Slothbloom'));
  // The control API's connection file (Stream Deck plugin and other tools read it).
  DelTree(ExpandConstant('{userappdata}\SprigCast'), True, True, True);
  // Logs. Looks you made and unpacked overlay media stay with your files in Documents\SprigCast.
  DelTree(ExpandConstant('{userdocs}\SprigCast\Logs'), True, True, True);
  RemoveDir(ExpandConstant('{userdocs}\SprigCast'));
  // Window layout, choices and other settings Qt keeps in the registry.
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Slothbloom\SprigCast');
  RegDeleteKeyIfEmpty(HKCU, 'Software\Slothbloom');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Registered: String;
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Stream keys, sign-ins and pairings live in Credential Manager. They always go, ticked or not (no
    // secrets outlive SprigCast), removed by its own Python while it's still here.
    Exec(ExpandConstant('{app}\python\python.exe'), '-I -m sprigcast.tools.forget_keys', '',
         SW_HIDE, ewWaitUntilTerminated, Code);
    Registered := RegisteredCamera();
    if Registered <> '' then
      RunElevated('regsvr32 /u /s "' + Registered + '" & regsvr32 /u /s "' + CameraDir() + '\sprigcast_camera32.dll" 2>nul');
  end
  else if (CurUninstallStep = usPostUninstall) and DeleteData then
    DeleteSprigCastData();
end;
