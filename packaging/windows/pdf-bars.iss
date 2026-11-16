[Setup]
AppId={{426E7F00-B148-4B7D-BA95-682EC6593CA2}
AppName=pdf bars
AppVersion=1.7
AppPublisher=pdf bars contributors
AppPublisherURL=https://github.com/andreymitinskii/pdf-bars
DefaultDirName={localappdata}\Programs\pdf bars
DefaultGroupName=pdf bars
PrivilegesRequired=lowest
OutputDir=..\..\release\1.7
OutputBaseFilename=pdf-bars-1.7-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
UninstallDisplayIcon={app}\pdf-bars.exe
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "..\..\dist\pdf-bars\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\pdf bars"; Filename: "{app}\pdf-bars.exe"
Name: "{autodesktop}\pdf bars"; Filename: "{app}\pdf-bars.exe"

[Registry]
Root: HKCU; Subkey: "Software\Classes\pdf-bars.Document"; ValueType: string; ValueData: "PDF document"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\pdf-bars.Document\DefaultIcon"; ValueType: string; ValueData: "{app}\pdf-bars.exe,0"
Root: HKCU; Subkey: "Software\Classes\pdf-bars.Document\shell\open\command"; ValueType: string; ValueData: """{app}\pdf-bars.exe"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "pdf-bars.Document"; ValueData: ""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\pdf-bars.exe"; Description: "pdf bars"; Flags: postinstall nowait skipifsilent
