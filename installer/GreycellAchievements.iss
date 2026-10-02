; Greycell Achievements installer (Inno Setup 6).
;
;   powershell -ExecutionPolicy Bypass -File scripts\build-setup.ps1
;
; One user, no administrator: the app goes to %LOCALAPPDATA%\Programs\Greycell
; Achievements, with a Start menu entry and an uninstaller in Windows Settings.
; Achievements live in the profile folder (%USERPROFILE%\OpenAchievements, settings in
; %APPDATA%\OpenAchievements) and
; installing, updating or uninstalling never touches it. The app updates itself
; by downloading a newer copy of this installer and running it with /SILENT:
; then only the progress window shows and the new version starts in the tray.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif

[Setup]
AppId={{6E0C2C1B-5B7E-4C1E-9A43-7D2F1B8E4A10}
AppName=Greycell Achievements
AppVersion={#AppVersion}
AppVerName=Greycell Achievements {#AppVersion}
AppPublisher=Greycell Open
AppPublisherURL=https://greycell.app/achievements/
AppSupportURL=https://github.com/greycell-open/greycell-achievements
AppUpdatesURL=https://greycell.app/achievements/
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Greycell Achievements
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=GreycellAchievementsSetup
SetupIconFile=..\src\openachievements\web\favicon.ico
UninstallDisplayIcon={app}\GreycellAchievements.exe
UninstallDisplayName=Greycell Achievements
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\GreycellAchievements.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Greycell Achievements"; Filename: "{app}\GreycellAchievements.exe"
Name: "{autodesktop}\Greycell Achievements"; Filename: "{app}\GreycellAchievements.exe"; Tasks: desktopicon

[Run]
; A normal install offers to open the app; an update (/SILENT) starts it in the tray.
Filename: "{app}\GreycellAchievements.exe"; Description: "Open Greycell Achievements"; Flags: nowait postinstall skipifsilent
Filename: "{app}\GreycellAchievements.exe"; Parameters: "--background"; Flags: nowait; Check: WizardSilent

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunName = 'GreycellAchievements';

{ Stop copies of the app running from this folder, and only those. }
procedure StopRunningCopies();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -Command "Get-Process GreycellAchievements -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -like ''' + ExpandConstant('{app}') + '\*'' } | Stop-Process -Force"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningCopies();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningCopies();
  Result := True;
end;

{ Start with Windows is the app's own setting; remove it only if it points here. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Value: String;
begin
  if CurUninstallStep = usPostUninstall then
    if RegQueryStringValue(HKCU, RunKey, RunName, Value) then
      if Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Value)) > 0 then
        RegDeleteValue(HKCU, RunKey, RunName);
end;
