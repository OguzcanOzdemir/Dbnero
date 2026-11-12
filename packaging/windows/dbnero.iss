; Inno Setup script for the Windows installer.
; Built by .github/workflows/release.yml:  iscc /DAppVersion=0.1.0 packaging\windows\dbnero.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8E3B2F3A-6C1D-4E7B-9C55-0D7B6A1E4D21}
AppName=dbnero
AppVersion={#AppVersion}
AppPublisher=dbnero contributors
AppPublisherURL=https://github.com/OguzcanOzdemir/Dbnero
DefaultDirName={autopf}\dbnero
DefaultGroupName=dbnero
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\out
OutputBaseFilename=dbnero-windows-x64-setup
SetupIconFile=..\icons\dbnero.ico
UninstallDisplayIcon={app}\dbnero.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Users accept the Terms of Use (which build on the MIT license) during setup.
LicenseFile=..\..\src\dbnero\legal\TERMS.txt

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\dbnero\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\dbnero"; Filename: "{app}\dbnero.exe"
Name: "{autodesktop}\dbnero"; Filename: "{app}\dbnero.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\dbnero.exe"; Description: "Start dbnero"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the Task Scheduler entry that runs scheduled backups (if any).
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /F /TN ""dbnero scheduled backups"""; Flags: runhidden; RunOnceId: "RemoveScheduledBackupsTask"
