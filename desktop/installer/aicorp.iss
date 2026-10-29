; AICorp for Windows - installer (Inno Setup 6). Per-user install: no administrator rights.
; Build: see build-installer.ps1 (it passes the version and the exe path).
; The uninstaller removes only the app files. It never touches WSL, Linux, or any AICorp data; the small settings file is kept
; unless the user answers Yes to the question at uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef ExePath
  #define ExePath "..\dist\AICorp.exe"
#endif

[Setup]
AppId={{6B0E1C52-7D3A-4B58-9C41-5F2A8E9D3C17}
AppName=AICorp
AppVersion={#AppVersion}
AppVerName=AICorp {#AppVersion}
AppPublisher=AICorp contributors
AppPublisherURL=https://github.com/mdirfanali-dev/ai-corp
AppSupportURL=https://github.com/mdirfanali-dev/ai-corp/issues
AppUpdatesURL=https://github.com/mdirfanali-dev/ai-corp/releases
DefaultDirName={autopf}\AICorp
DefaultGroupName=AICorp
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
LicenseFile=..\..\LICENSE
OutputBaseFilename=AICorp-Setup-{#AppVersion}
OutputDir=..\dist
SetupIconFile=..\assets\aicorp.ico
UninstallDisplayIcon={app}\AICorp.exe
UninstallDisplayName=AICorp
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#ExePath}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\NOTICE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\AICorp"; Filename: "{app}\AICorp.exe"
Name: "{group}\Uninstall AICorp"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AICorp"; Filename: "{app}\AICorp.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AICorp.exe"; Description: "Start AICorp"; Flags: nowait postinstall skipifsilent

[Messages]
FinishedLabel=AICorp is installed.%n%nIt needs WSL2 with Ubuntu and a copy of AICorp inside Linux. On the first start it checks your computer and tells you what is missing; it never installs WSL or asks for administrator rights by itself.

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  SettingsDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    SettingsDir := ExpandConstant('{userappdata}\AICorp');
    if DirExists(SettingsDir) then
      if MsgBox('Also remove the AICorp app settings (which Linux distribution and folder to use)?' + #13#10 + #13#10 +
                'Your AICorp data (employees, projects, logs, documents, backups) lives inside Linux and is never touched by this uninstaller.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(SettingsDir, True, True, True);
  end;
end;
