; Inno Setup script for the Home Camera Windows installer.
; Built by GitHub Actions (see .github/workflows/build.yml). To build by hand on Windows:
;   1. From the pc folder: pyinstaller --noconfirm installer/camhub.spec
;   2. Put ffmpeg.exe and ffprobe.exe in pc\installer\ffmpeg\
;   3. "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 installer\HomeCamera.iss
;
; Silent install (for testing): HomeCameraSetup.exe /VERYSILENT /VIEWERPASSWORD=secret123
;   optional: /CAMERAIP=192.168.1.50 /CAMERAUSER=admin /CAMERAPASSWORD=... /STORAGE=D:\Clips

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "Home Camera"
#define Port "8080"

[Setup]
AppId={{6B2E7C55-3E0B-4B83-9C1D-6F3B0E4A9D21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Home Camera
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=homecamera.ico
UninstallDisplayIcon={app}\camhub.exe
UninstallDisplayName={#AppName}
OutputDir=output
OutputBaseFilename=HomeCameraSetup
Compression=lzma2/max
SolidCompression=yes
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Add a Home Camera icon to the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\camhub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "ffmpeg\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "ffmpeg\ffprobe.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "register-task.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "restart.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "find-camera.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "change-password.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "homecamera.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\HomeCamera"

[Icons]
Name: "{group}\Home Camera"; Filename: "{code:ViewerProgram}"; Parameters: "{code:ViewerParams}"; IconFilename: "{app}\homecamera.ico"
Name: "{group}\Find camera again"; Filename: "{app}\find-camera.cmd"; IconFilename: "{app}\homecamera.ico"
Name: "{group}\Change password"; Filename: "{app}\change-password.cmd"; IconFilename: "{app}\homecamera.ico"
Name: "{group}\Restart Home Camera"; Filename: "{app}\restart.cmd"; IconFilename: "{app}\homecamera.ico"
Name: "{group}\Settings and logs folder"; Filename: "{commonappdata}\HomeCamera"
Name: "{group}\Recordings folder"; Filename: "{code:StoragePath}"
Name: "{group}\Uninstall Home Camera"; Filename: "{uninstallexe}"
Name: "{commondesktop}\Home Camera"; Filename: "{code:ViewerProgram}"; Parameters: "{code:ViewerParams}"; IconFilename: "{app}\homecamera.ico"; Tasks: desktopicon

[Run]
Filename: "{code:ViewerProgram}"; Parameters: "{code:ViewerParams}"; Description: "Open Home Camera now"; Flags: postinstall nowait skipifsilent shellexec

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""Home Camera"""; Flags: runhidden; RunOnceId: "EndTask"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM camhub.exe"; Flags: runhidden; RunOnceId: "Kill"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""Home Camera"""; Flags: runhidden; RunOnceId: "DeleteTask"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Home Camera"""; Flags: runhidden; RunOnceId: "Firewall"

[UninstallDelete]
Type: filesandordirs; Name: "{commonappdata}\HomeCamera\logs"

[Code]
var
  PasswordPage: TInputQueryWizardPage;
  CameraPage: TInputQueryWizardPage;
  StoragePage: TInputDirWizardPage;
  SetupResult: String;

function ConfigFile: String;
begin
  Result := ExpandConstant('{commonappdata}\HomeCamera\config.json');
end;

function IsUpgrade: Boolean;
begin
  Result := FileExists(ConfigFile);
end;

function EdgePath: String;
begin
  Result := ExpandConstant('{commonpf32}\Microsoft\Edge\Application\msedge.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{commonpf64}\Microsoft\Edge\Application\msedge.exe');
end;

{ The viewer opens in its own Edge window, like an app. Falls back to the default browser. }
function ViewerProgram(Param: String): String;
begin
  if FileExists(EdgePath) then Result := EdgePath
  else Result := 'http://localhost:{#Port}/';
end;

function ViewerParams(Param: String): String;
begin
  if FileExists(EdgePath) then Result := '--app=http://localhost:{#Port}/'
  else Result := '';
end;

function StoragePath(Param: String): String;
begin
  if StoragePage <> nil then Result := StoragePage.Values[0]
  else Result := 'C:\CameraRecordings';
end;

function JsonEscape(S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if C = '\' then Result := Result + '\\'
    else if C = '"' then Result := Result + '\"'
    else if Ord(C) < 32 then Result := Result + ' '
    else Result := Result + C;
  end;
end;

procedure InitializeWizard;
var
  Hint: String;
begin
  if IsUpgrade then
    Hint := 'Home Camera is already set up. Leave these empty to keep your current password.'
  else
    Hint := 'You''ll use this password to sign in on this PC and in the phone app. Use at least 8 characters.';
  PasswordPage := CreateInputQueryPage(wpSelectTasks,
    'Choose a password', 'Protects your cameras on the PC and phone', Hint);
  PasswordPage.Add('Password:', True);
  PasswordPage.Add('Type it again:', True);

  CameraPage := CreateInputQueryPage(PasswordPage.ID,
    'Your camera', 'Setup will look for the camera''s video on your network',
    'Find the camera''s IP address in the Hiseeu app (device info) or in your router''s list of devices. ' +
    'Leave the IP address empty to skip this and set up the camera later.');
  CameraPage.Add('Camera IP address (for example 192.168.1.50):', False);
  CameraPage.Add('Camera user name:', False);
  CameraPage.Add('Camera password:', True);
  CameraPage.Values[1] := 'admin';

  StoragePage := CreateInputDirPage(CameraPage.ID,
    'Where to keep recordings', 'Clips are saved here when motion is detected',
    'Choose a folder with plenty of space. Clips older than 30 days are deleted automatically.',
    False, '');
  StoragePage.Add('');
  StoragePage.Values[0] := 'C:\CameraRecordings';

  { Answers given on the command line (used for silent installs) fill in the pages. }
  PasswordPage.Values[0] := ExpandConstant('{param:VIEWERPASSWORD|}');
  PasswordPage.Values[1] := PasswordPage.Values[0];
  CameraPage.Values[0] := ExpandConstant('{param:CAMERAIP|}');
  CameraPage.Values[1] := ExpandConstant('{param:CAMERAUSER|admin}');
  CameraPage.Values[2] := ExpandConstant('{param:CAMERAPASSWORD|}');
  StoragePage.Values[0] := ExpandConstant('{param:STORAGE|C:\CameraRecordings}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { On an upgrade the camera and folder are already set; only offer the password page. }
  if IsUpgrade and ((PageID = CameraPage.ID) or (PageID = StoragePage.ID)) then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  { A silent install can't show messages, so never hold it on a page. }
  if WizardSilent then Exit;
  if CurPageID = PasswordPage.ID then
  begin
    if (PasswordPage.Values[0] = '') and (PasswordPage.Values[1] = '') and IsUpgrade then
      Exit;
    if Length(PasswordPage.Values[0]) < 8 then
    begin
      MsgBox('Please use at least 8 characters.', mbError, MB_OK);
      Result := False;
    end
    else if PasswordPage.Values[0] <> PasswordPage.Values[1] then
    begin
      MsgBox('The two passwords don''t match.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { Stop a running copy so its files can be replaced. }
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "Home Camera"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM camhub.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

function Answer(Name, PageValue: String): String;
begin
  { Values given on the command line (silent install) win over the wizard pages. }
  Result := ExpandConstant('{param:' + Name + '|}');
  if Result = '' then Result := PageValue;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AnswersFile, ResultFile, Json, Pw, CamIp: String;
  Code, SetupCode: Integer;
  Lines: AnsiString;
begin
  if CurStep <> ssPostInstall then Exit;

  Pw := Answer('VIEWERPASSWORD', PasswordPage.Values[0]);
  CamIp := Answer('CAMERAIP', CameraPage.Values[0]);
  Json := '{"password": "' + JsonEscape(Pw) + '"';
  if not IsUpgrade then
  begin
    Json := Json + ', "storage": "' + JsonEscape(Answer('STORAGE', StoragePage.Values[0])) + '"';
    Json := Json + ', "port": {#Port}';
  end;
  if CamIp <> '' then
    Json := Json +
      ', "camera_ip": "' + JsonEscape(CamIp) + '"' +
      ', "camera_user": "' + JsonEscape(Answer('CAMERAUSER', CameraPage.Values[1])) + '"' +
      ', "camera_password": "' + JsonEscape(Answer('CAMERAPASSWORD', CameraPage.Values[2])) + '"';
  Json := Json + '}';

  AnswersFile := ExpandConstant('{tmp}\answers.json');
  ResultFile := ExpandConstant('{tmp}\result.txt');
  SaveStringToFile(AnswersFile, Json, False);

  WizardForm.StatusLabel.Caption := 'Setting up, and looking for your camera. This can take a minute...';
  Exec(ExpandConstant('{app}\camhub.exe'),
    '--config "' + ConfigFile + '" setup --answers "' + AnswersFile + '" --result "' + ResultFile + '"',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, SetupCode);
  DeleteFile(AnswersFile);
  if LoadStringFromFile(ResultFile, Lines) then SetupResult := Lines;
  if SetupCode = 3 then
    SetupResult := 'Home Camera is installed, but it couldn''t find your camera''s video yet.' + #13#10#13#10 +
      'Check the camera is on, then open Home Camera, go to Settings > Cameras, ' +
      'check the IP address and password, and press Find and save.';

  WizardForm.StatusLabel.Caption := 'Starting Home Camera...';
  Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\tools\register-task.ps1') +
    '" -AppDir "' + ExpandConstant('{app}') + '" -Port {#Port}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN "Home Camera"', '', SW_HIDE, ewWaitUntilTerminated, Code);

  if SetupCode = 3 then
    SuppressibleMsgBox(SetupResult, mbInformation, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if SuppressibleMsgBox('Also delete Home Camera''s settings (password and camera details)?' + #13#10#13#10 +
        'Your recordings are kept either way.', mbConfirmation, MB_YESNO, IDNO) = IDYES then
      DelTree(ExpandConstant('{commonappdata}\HomeCamera'), True, True, True);
  end;
end;
