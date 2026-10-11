; SysAgent Inno Setup 脚本（可选路径：有 Inno Setup 6 编译器的环境）
; 编译: ISCC.exe installer\setup.iss
; 默认输出: dist\SysAgent-Setup.exe
; 若使用本脚本，请在 [Files] 里带上 installed.flag，使数据目录落到 %APPDATA%\SysAgent

#define MyAppName "SysAgent"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "SysAgent Project"

[Setup]
AppId={{8F3C2A11-SysAgent-0001-4B7E-9C2D-7A61B0E4D991}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\SysAgent
DefaultGroupName=SysAgent
PrivilegesRequired=admin
OutputBaseFilename=SysAgent-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\dist\SysAgent-Portable\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "installed.flag"; DestDir: "{app}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "autostart"; Description: "开机自动启动服务"; GroupDescription: "附加任务:"

[Icons]
Name: "{group}\启动 SysAgent"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"
Name: "{group}\SysAgent 控制台"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"
Name: "{group}\卸载 SysAgent"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SysAgent"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SysAgent"; ValueData: "{app}\SysAgent.exe"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\启动.bat"; Description: "启动 SysAgent"; Flags: nowait postinstall skipifsilent
