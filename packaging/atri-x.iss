; ATRI-X Windows 安装包脚本（Inno Setup 6.7+ / 7.x）
;
; 由 ``packaging/build_installer.py`` 调用，所有可变项都通过 /D 定义传入：
;   /DSOURCE_DIR=<dist\windows\atri-x>
;   /DOUTPUT_DIR=<dist\installer>
;   /DAPP_VERSION=0.9.0
;   /DOUTPUT_BASENAME=ATRI-X-0.9.0-setup
;
; 设计要点：
; - **用户级安装**（PrivilegesRequired=lowest，不弹 UAC），目录与
;   ``atri-x.exe -m atri_x.dist.install install`` 保持一致：
;   ``%LOCALAPPDATA%\ATRI-X\app``；用户数据仍在 ``%APPDATA%\ATRI-X``，卸载默认保留。
; - 安装后写 ``install.json`` 标记（``"installer":"inno"``）：``atri-x ... status``
;   因此能区分「Inno 安装」与「便携/脚本安装」，并提示用卸载器而非脚本卸载。
; - 卸载前用 taskkill 结束可能还在跑的 GUI 与控制台服务（后台服务不持有 GUI 互斥量，
;   CloseApplications 管不到它），否则 ``atri-x.exe`` 被占用会导致卸载删不干净。
; - 默认勾选「开机自启」：写 HKCU Run，值名与运行时 ``atri_x/gui/autostart.py`` 一致
;   （``ATRI-X``），卸载时自动删除。
; - **品牌素材走相对路径**（``..\atri_x\gui\brand\app.ico``、``assets\wizard-*.png``）：
;   Inno 按脚本所在目录（源目录）解析，所以不会因为 ISCC 的当前工作目录不同而找不到文件。

#ifndef APP_VERSION
  #define APP_VERSION "0.0.0"
#endif
#ifndef SOURCE_DIR
  #error 缺少 /DSOURCE_DIR：请用 packaging/build_installer.py 构建
#endif
#ifndef OUTPUT_DIR
  #define OUTPUT_DIR "."
#endif
#ifndef OUTPUT_BASENAME
  #define OUTPUT_BASENAME "ATRI-X-" + APP_VERSION + "-setup"
#endif

#define APP_NAME "ATRI-X"
#define APP_PUBLISHER "ATRI-X"
#define APP_ID "{{9B0F1C2A-5E44-4B7C-9A51-2D6E7F8A1B33}"

[Setup]
AppId={#APP_ID}
AppName={#APP_NAME}
AppVersion={#APP_VERSION}
AppVerName={#APP_NAME} {#APP_VERSION}
AppPublisher={#APP_PUBLISHER}
AppSupportURL=https://github.com/fujiwarashigure/ATRI-X
DefaultDirName={localappdata}\ATRI-X\app
DefaultGroupName={#APP_NAME}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir={#OUTPUT_DIR}
OutputBaseFilename={#OUTPUT_BASENAME}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
; 必须显式打开「欢迎」页：Inno 6/7 默认 DisableWelcomePage=yes，而左侧宣传图**只在
; 「欢迎」和「完成」两页显示**——不打开的话整个安装过程左侧几乎都是空的。
DisableWelcomePage=no
; ---- 品牌素材（都由 packaging/make_assets.py 从品牌源图生成）----
; 相对路径按 Inno 的「源目录」解析，也就是本脚本所在的 packaging/。
; SetupIconFile：安装包 setup.exe 自己的图标（不设就是 Inno 的默认图标）。
; WizardImageFile：向导「欢迎」与「安装完成」两页左侧的宣传图，比例必须 164:314；
;   给 100%/200%/250% 三档，ISCC 会按当前 DPI 挑最接近的一档，避免高 DPI 下拉伸发糊。
; WizardSmallImageFile：向导右上角的小图（58×58，Inno 7 起小图区是 58×58）。
SetupIconFile=..\atri_x\gui\brand\app.ico
WizardImageFile=assets\wizard-image.png,assets\wizard-image-2x.png,assets\wizard-image-250.png
WizardSmallImageFile=assets\wizard-small.png,assets\wizard-small-2x.png
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\atri-x-gui.exe
UninstallDisplayName={#APP_NAME}
AppMutex=ATRI-X-GUI
CloseApplications=force
RestartApplications=no
SetupLogging=yes
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#ifdef WITH_ZH
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "开机自动启动 ATRI-X（隐藏到托盘，后台服务继续运行）"; GroupDescription: "启动选项"

[Files]
; 整个 PyInstaller onedir 产物（含 _internal；内置 DSH 内核在 _internal\runtime 下）。
Source: "{#SOURCE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 不用 {group}/{autoprograms}：它们走 SHGetFolderPath，在受隔离的账户/精简环境里
; 可能返回 0x80070005 并让安装失败。{userappdata} 来自环境变量，稳定可用。
Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\ATRI-X\{#APP_NAME}"; Filename: "{app}\atri-x-gui.exe"; WorkingDir: "{app}"
Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\ATRI-X\{cm:UninstallProgram,{#APP_NAME}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#APP_NAME}"; Filename: "{app}\atri-x-gui.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; 与 atri_x/gui/autostart.py 使用同一个值名（ATRI-X），两条路径互相认。
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ATRI-X"; ValueData: """{app}\atri-x-gui.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\atri-x-gui.exe"; Description: "启动 {#APP_NAME}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 后台服务（atri-x.exe service run）不持有 GUI 互斥量，CloseApplications 管不到。
Filename: "{sys}\taskkill.exe"; Parameters: "/IM atri-x-gui.exe /F"; Flags: runhidden; RunOnceId: "killgui"
Filename: "{sys}\taskkill.exe"; Parameters: "/IM atri-x.exe /F"; Flags: runhidden; RunOnceId: "killconsole"

[UninstallDelete]
Type: files; Name: "{app}\install.json"
; 内置协议端的运行期配置（NapCat 的 onebot11.json / webui.json 等）是首次启动时生成的，
; 不在 [Files] 清单里；不删掉会让卸载后安装目录残留内容。
Type: filesandordirs; Name: "{app}\qqproto"
Type: dirifempty; Name: "{app}"
Type: dirifempty; Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\ATRI-X"

[Code]
const
  UNINSTALL_DATA_PROMPT =
    '是否同时删除用户数据？' + #13#10 + #13#10 +
    '包含配置、会话数据库 .env 与日志（%APPDATA%\ATRI-X）。' + #13#10 +
    '选「否」则保留，重新安装后可以继续使用原有会话。';

function JsonEscape(const Value: string): string;
var
  Text: string;
begin
  Text := Value;
  StringChangeEx(Text, '\', '\\', True);
  StringChangeEx(Text, '"', '\"', True);
  StringChangeEx(Text, #13#10, '\n', True);
  StringChangeEx(Text, #10, '\n', True);
  StringChangeEx(Text, #13, '\n', True);
  Result := Text;
end;

procedure WriteInstallMarker;
var
  Target, Marker, Json, GroupLnks, DesktopLnk, Uninst: string;
begin
  Log('ATRI-X: WriteInstallMarker begin');
  Target := ExpandConstant('{app}');
  Marker := AddBackslash(Target) + 'install.json';
  Uninst := ExpandConstant('{uninstallexe}');
  GroupLnks := JsonEscape(ExpandConstant('{userappdata}\Microsoft\Windows\Start Menu\Programs\ATRI-X\ATRI-X.lnk'));
  DesktopLnk := JsonEscape(ExpandConstant('{%USERPROFILE}\Desktop\ATRI-X.lnk'));
  Json :=
    '{' +
    '"installer":"inno",' +
    '"app":"ATRI-X",' +
    '"version":"' + '{#APP_VERSION}' + '",' +
    '"target":"' + JsonEscape(Target) + '",' +
    '"uninstaller":"' + JsonEscape(Uninst) + '",' +
    '"dataDir":"' + JsonEscape(ExpandConstant('{userappdata}\ATRI-X')) + '",' +
    '"shortcuts":["' + GroupLnks + '","' + DesktopLnk + '"],' +
    '"installedAt":"' + GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + '"' +
    '}';
  SaveStringToFile(Marker, Json, False);
  Log('ATRI-X: install marker written to ' + Marker);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  Log('ATRI-X: CurStepChanged step=' + IntToStr(Ord(CurStep)));
  if (CurStep = ssPostInstall) or (CurStep = ssDone) then
    WriteInstallMarker;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{userappdata}\ATRI-X');
  // 静默卸载保持数据（运维脚本不该误删会话）；交互式卸载询问一次。
  if (not UninstallSilent) and DirExists(DataDir) then
  begin
    if MsgBox(UNINSTALL_DATA_PROMPT, mbConfirmation, MB_YESNO) = IDYES then
      DelTree(DataDir, True, True, True);
  end;
end;
