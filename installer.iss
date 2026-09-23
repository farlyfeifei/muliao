; 幕僚 Muliáo — Inno Setup 安装器脚本
; 生成 dist\幕僚Muliao-Setup.exe
; 重新构建：先 build.bat（产出 dist\Muliáo.exe），再：
;   "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer.iss
;   （ISCC 路径因机器而异，build.bat 里会自动探测）

#define MyAppName "幕僚 Muliáo"
#define MyAppNameEn "Muliao"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Muliáo"
#define MyAppURL "http://127.0.0.1:8930"
#define MyAppExeName "Muliáo.exe"

[Setup]
; 固定 AppId（升级/卸载时识别同一程序，勿改）
AppId={{D0C8DD92-F4A6-4E0B-9998-9E7D55B75D89}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName=D:\Program Files\幕僚Muliáo
UsePreviousAppDir=no
DefaultGroupName={#MyAppName}
; 用户要求固定装到 D 盘。注意 {sd} 是 Windows 系统盘（本机=C:）不是 D:，
; Inno Setup 无 D 盘内置常量，故固定写入唯一权威目录；命令行覆盖也必须用单斜杠 /DIR=。
; 单文件 exe 无需管理员权限也能装到用户目录；优先当前用户安装，避免 UAC
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=幕僚Muliáo-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; 安装/卸载界面用英文（系统未带简体中文 .isl，避免找不到语言文件报错）
ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Start Muliáo automatically when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 静态前端已经打进单文件 exe（PyInstaller --add-data），这里不再单独装 static/

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; 开机自启：在用户启动目录建快捷方式（lowest 权限，无需管理员）
Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\Startup\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
