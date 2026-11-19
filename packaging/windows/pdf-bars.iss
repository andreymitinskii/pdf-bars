[Setup]
AppId={{426E7F00-B148-4B7D-BA95-682EC6593CA2}
AppName=pdf bars
AppVersion=1.7.2
AppPublisher=pdf bars contributors
AppPublisherURL=https://github.com/andreymitinskii/pdf-bars
DefaultDirName={code:DefaultInstallDirectory}
UsePreviousAppDir=no
DefaultGroupName=pdf bars
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
LanguageDetectionMethod=uilanguage
OutputDir=..\..\release\1.7.2
OutputBaseFilename=pdf-bars-1.7.2-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
UninstallDisplayIcon={app}\pdf-bars.exe
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
en.PerUserFolder=Windows protects Program Files. Installation without administrator rights must use a user folder. The recommended folder has been selected. To install for all users, run this installer with /ALLUSERS and approve the administrator request.
ru.PerUserFolder=Windows защищает Program Files. Для установки без прав администратора нужна пользовательская папка. Рекомендуемая папка уже выбрана. Для установки всем пользователям запустите установщик с /ALLUSERS и подтвердите запрос администратора.

[Files]
Source: "..\..\dist\pdf-bars\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\pdf bars"; Filename: "{app}\pdf-bars.exe"
Name: "{autodesktop}\pdf bars"; Filename: "{app}\pdf-bars.exe"

[Registry]
Root: HKA; Subkey: "Software\Classes\pdf-bars.Document"; ValueType: string; ValueData: "PDF document"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\pdf-bars.Document\DefaultIcon"; ValueType: string; ValueData: "{app}\pdf-bars.exe,0"
Root: HKA; Subkey: "Software\Classes\pdf-bars.Document\shell\open\command"; ValueType: string; ValueData: """{app}\pdf-bars.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "pdf-bars.Document"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\pdf-bars.exe"; Description: "pdf bars"; Flags: postinstall nowait skipifsilent

[Code]
function DefaultInstallDirectory(Param: String): String;
begin
  if IsAdminInstallMode then
    Result := ExpandConstant('{autopf}\pdf bars')
  else
    Result := ExpandConstant('{localappdata}\Programs\pdf bars');
end;

function WithinFolder(Path, Folder: String): Boolean;
begin
  Path := AddBackslash(Lowercase(ExpandFileName(Path)));
  Folder := AddBackslash(Lowercase(ExpandFileName(Folder)));
  Result := Pos(Folder, Path) = 1;
end;

function ProtectedUserInstall: Boolean;
begin
  Result := (not IsAdminInstallMode) and
    (WithinFolder(WizardDirValue, ExpandConstant('{commonpf}')) or
     WithinFolder(WizardDirValue, ExpandConstant('{commonpf32}')) or
     WithinFolder(WizardDirValue, ExpandConstant('{win}')));
  if IsWin64 and (not IsAdminInstallMode) then
    Result := Result or WithinFolder(WizardDirValue, ExpandConstant('{commonpf64}'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and ProtectedUserInstall and (not WizardSilent) then begin
    WizardForm.DirEdit.Text := DefaultInstallDirectory('');
    MsgBox(CustomMessage('PerUserFolder'), mbInformation, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if ProtectedUserInstall then
    Result := CustomMessage('PerUserFolder');
end;
