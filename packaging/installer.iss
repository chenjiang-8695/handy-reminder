; Inno Setup 安装脚本 —— 用 Inno Setup 6+ 编译:
; 注意: 本文件必须存成 **不带 BOM 的 UTF-8** —— 带 BOM 时 Inno/ISPP 会报
;       "Text is not inside a section" (BOM 被当成第一行的正文)。
;     iscc packaging\installer.iss /DAppVersion=2.5.4
; 产物: packaging\out\HandyReminder-<版本>-setup.exe
;
; 关键设计: 装到 {localappdata}\Programs\ 而不是 Program Files ——
;   1. 不需要管理员权限 (自启项写在 HKCU, 装到 HKLM 会权限错配)
;   2. 避免"程序在临时/受保护目录"这类被安全软件针对的形态 (第 10 项实测过自启项被摘)

#ifndef AppVersion
  #define AppVersion "2.5.4"
#endif
#define AppName "HandyReminder"
#define AppNameCN "智能手账式悬浮提醒助手"
#define Publisher "待填: 公司名"
#define ExeName "HandyReminder.exe"

[Setup]
AppId={{8F3C1A52-4B7E-4C1D-9A6B-2E5D7C4F1B93}
AppName={#AppNameCN}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppNameCN}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=out
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppNameCN}
; 这是个常驻托盘程序, 正常运行时就开着 —— 升级/卸载前必须先把运行的实例关掉,
; 否则文件被占用, 表现为"装到一半报错"或"卸载后目录还在"。
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#ExeName}
SetupIconFile=assets\icon.ico

[Languages]
; 中文语言包随仓库一起带, 不依赖 Inno Setup 安装目录里的语言包
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"
Name: "autostart"; Description: "开机自动启动(可在托盘菜单里随时关闭)"; GroupDescription: "附加选项:"

[Files]
; 打包产物整目录搬入
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppNameCN}"; Filename: "{app}\{#ExeName}"
Name: "{group}\卸载 {#AppNameCN}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppNameCN}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
; 勾了"开机自启"就用程序自己的接口去写, 保证与代码里的注册表键名一致
Filename: "{app}\{#ExeName}"; Parameters: "--autostart on"; Flags: runhidden; Tasks: autostart; StatusMsg: "正在设置开机自启..."
Filename: "{app}\{#ExeName}"; Description: "立即运行 {#AppNameCN}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 顺序要紧: 先结束正在运行的实例 —— 托盘程序本来就常年开着, 它会锁住 {app} 里的文件,
; 不先杀掉就会看到"卸载完目录还在"或"升级时装不进去"。
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#ExeName} /F"; Flags: runhidden; RunOnceId: "KillRunningApp"
; 卸载前先摘掉自启项, 否则会留下一个指向已删除 exe 的启动项
Filename: "{app}\{#ExeName}"; Parameters: "--autostart off"; Flags: runhidden; RunOnceId: "RemoveAutostart"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 提醒数据默认保留 (用户的提醒不该被卸载程序单方面删掉), 只做提示
    if DirExists(ExpandConstant('{localappdata}\HandyReminder')) then
      if MsgBox('是否同时删除提醒数据(便签历史与设置)?' + #13#10 +
                '选择"否"将保留, 下次安装可继续使用。',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(ExpandConstant('{localappdata}\HandyReminder'), True, True, True);
  end;
end;
