; DAClaw Windows installer.
;
; Build with:
;   ISCC.exe /DAppVersion=0.1.0 /DSourceDir="C:\path\to\payload" DAClaw.iss

#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

#ifndef SourceDir
#define SourceDir "build\payload"
#endif

#ifndef OutputDir
#define OutputDir "dist"
#endif

[Setup]
AppId={{A9125E3C-F1D1-4D06-9E43-8E55B9D2BE53}
AppName=DAClaw
AppVersion={#AppVersion}
AppPublisher=DA-Open
AppPublisherURL=https://github.com/DA-Open/DAClaw
AppSupportURL=https://github.com/DA-Open/DAClaw/issues
AppUpdatesURL=https://github.com/DA-Open/DAClaw/releases
DefaultDirName={localappdata}\DAClaw\app
DefaultGroupName=DAClaw
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=DAClaw-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\web\public\favicon.ico
SetupLogging=yes
InfoBeforeFile=installer-prerequisites.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\DAClaw"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\launch-daclaw.ps1"" -AppDir ""{app}"""; WorkingDir: "{app}"; IconFilename: "{app}\web\public\favicon.ico"
Name: "{group}\Uninstall DAClaw"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DAClaw"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\launch-daclaw.ps1"" -AppDir ""{app}"""; WorkingDir: "{app}"; IconFilename: "{app}\web\public\favicon.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\setup-runtime.ps1"" -AppDir ""{app}"" -SkipWebBuild -NoShortcut"; WorkingDir: "{app}"; StatusMsg: "Preparing the DAClaw Python environment..."; Flags: runhidden waituntilterminated
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\windows\launch-daclaw.ps1"" -AppDir ""{app}"""; WorkingDir: "{app}"; Description: "Launch DAClaw"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '{app}\venv\*' } | Stop-Process -Force -ErrorAction SilentlyContinue"""; Flags: runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\node_modules"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{localappdata}\DAClaw\logs"

[Messages]
WelcomeLabel1=Welcome to the DAClaw Setup Wizard
WelcomeLabel2=This installs DAClaw as a local web dashboard. The installer creates a DAClaw-only Python virtual environment inside the install folder.
FinishedHeadingLabel=DAClaw setup is complete
FinishedLabel=DAClaw is installed. You can launch it from the Desktop or Start Menu.
