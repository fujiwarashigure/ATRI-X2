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
;   ``%LOCALAPPDATA%\ATRI-X\app``；用户数据默认在 ``%APPDATA%\ATRI-X``。
; - **数据与配置目录可选**（1.1.8 新增）：向导在「选择程序目录」之后多一页，默认
;   ``<安装目录>\data``（自包含：整个安装目录可一起备份/拷走）。选择写进
;   ``install.json`` 的 ``"dataDir"`` 与 ``HKCU\Software\ATRI-X\DataDir``（卸载器读
;   注册表，路径含中文也不会乱码），运行期由 ``atri_x.config.home`` 统一解析。
; - 安装标记/数据目录由 **Python 助手**（``atri-x.exe -m atri_x.dist.install finalize``）
;   写：Inno 的 [Code] 只能读写 ANSI 字符串，写不出带非 ASCII 路径的 UTF-8 JSON。
; - **旧数据迁移**：若旧位置（``%APPDATA%\ATRI-X``）有数据、新目录为空且两者不同，
;   安装后询问一次，调 ``atri-x.exe -m atri_x.dist.install migrate --from ... --to ...
;   --move``（先复制并逐文件核对，成功才删旧目录）。静默安装自动迁移。
; - 安装后写 ``install.json`` 标记（``"installer":"inno"``）：``atri-x ... status``
;   因此能区分「Inno 安装」与「便携/脚本安装」，并提示用卸载器而非脚本卸载。
; - 卸载前用 taskkill 结束可能还在跑的 GUI 与控制台服务（后台服务不持有 GUI 互斥量，
;   CloseApplications 管不到它），否则 ``atri-x.exe`` 被占用会导致卸载删不干净。
; - **卸载时的数据**：数据目录在安装目录内（自包含）→ 随安装目录一起删；在外面 →
;   交互式询问一次，静默卸载保留。
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
; 数据目录要跨语言保存（卸载器是 Pascal，读不了 UTF-8 JSON）：写注册表字符串，
; 路径含中文也安全；ssPostInstall 里会用最终选择覆盖默认值。
Root: HKCU; Subkey: "Software\ATRI-X"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\ATRI-X"; ValueType: string; ValueName: "DataDir"; ValueData: "{app}\data"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\atri-x-gui.exe"; Description: "启动 {#APP_NAME}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 后台服务（atri-x.exe service run）不持有 GUI 互斥量，CloseApplications 管不到。
Filename: "{sys}\taskkill.exe"; Parameters: "/IM atri-x-gui.exe /F"; Flags: runhidden; RunOnceId: "killgui"
Filename: "{sys}\taskkill.exe"; Parameters: "/IM atri-x.exe /F"; Flags: runhidden; RunOnceId: "killconsole"

[UninstallDelete]
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\data-dir.txt"
; 内置协议端的运行期配置（NapCat 的 onebot11.json / webui.json 等）是首次启动时生成的，
; 不在 [Files] 清单里；不删掉会让卸载后安装目录残留内容。
Type: filesandordirs; Name: "{app}\qqproto"
Type: dirifempty; Name: "{app}"
Type: dirifempty; Name: "{userappdata}\Microsoft\Windows\Start Menu\Programs\ATRI-X"

[Code]
var
  DataDirPage: TInputDirWizardPage;
  AutoDataDir: String;
  InstalledDataDir: String;

function TrimTrail(const Value: string): string;
begin
  Result := Value;
  while (Length(Result) > 3) and
        ((Result[Length(Result)] = '\') or (Result[Length(Result)] = '/')) do
    SetLength(Result, Length(Result) - 1);
end;

function ReadDataDirFromRegistry: string;
var
  Value: string;
begin
  Result := '';
  if RegQueryStringValue(HKCU, 'Software\ATRI-X', 'DataDir', Value) then
    Result := TrimTrail(Trim(Value));
end;

function DirHasEntries(const Directory: string): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if not DirExists(Directory) then
    exit;
  if not FindFirst(AddBackslash(Directory) + '*', FindRec) then
    exit;
  repeat
    if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
      Result := True;
  until Result or (not FindNext(FindRec));
  FindClose(FindRec);
end;

function PathIsUnder(const Child, Parent: string): Boolean;
var
  C: string;
  P: string;
begin
  C := LowerCase(TrimTrail(Child));
  P := LowerCase(TrimTrail(Parent));
  Result := (Length(C) > Length(P)) and (Copy(C, 1, Length(P) + 1) = P + '\');
end;

function RunAppCommand(const Arguments: string): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(AddBackslash(ExpandConstant('{app}')) + 'atri-x.exe', Arguments,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not Result then
  begin
    Log('ATRI-X: 启动 atri-x.exe 失败：' + Arguments);
    exit;
  end;
  Log('ATRI-X: atri-x.exe ' + Arguments + ' -> exit=' + IntToStr(ResultCode));
  Result := ResultCode = 0;
end;

function RunDataMigration(const SourceDir, TargetDir: string): Boolean;
begin
  Result := RunAppCommand(' -m atri_x.dist.install migrate --from "' + SourceDir +
    '" --to "' + TargetDir + '" --move');
end;

function RunFinalize(const DataDir: string; const Migrated: Boolean): Boolean;
var
  Arguments: string;
begin
  Arguments := ' -m atri_x.dist.install finalize --data-dir "' + DataDir + '"';
  if Migrated then
    Arguments := Arguments + ' --migrated';
  Result := RunAppCommand(Arguments);
end;

procedure WriteRegistryDataDir(const DataDir: string);
begin
  RegWriteStringValue(HKCU, 'Software\ATRI-X', 'DataDir', DataDir);
end;

procedure InitializeWizard;
var
  PreviousDataDir: string;
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    '数据与配置目录',
    'ATRI-X 的配置与用户数据放在哪里？',
    '配置（app.toml / services.toml / .env）、会话数据库、成员档案、DSH 状态、技能与日志都会放在这里。' + #13#10 +
    '默认放在安装目录下的 data 子目录——整个安装目录可以一起备份或拷走；' +
    '想沿用旧位置，请点「浏览」选择 %APPDATA%\ATRI-X。',
    False, SetupMessage(msgNewFolderName));
  DataDirPage.Add('数据目录：');
  PreviousDataDir := ReadDataDirFromRegistry;
  AutoDataDir := '';
  if (PreviousDataDir <> '') and DirExists(PreviousDataDir) then
    DataDirPage.Values[0] := PreviousDataDir
  else if WizardSilent then
    // 静默安装不走向导：留空，ssPostInstall 里兜底 <安装目录>\data。
    DataDirPage.Values[0] := ''
  else
  begin
    // Inno 7 不允许在 InitializeWizard 里展开 {app}（目录页还没初始化）；
    // 这里先用默认安装位置作占位值，离开「选择程序目录」页时再按实际安装目录改写。
    AutoDataDir := ExpandConstant('{localappdata}\ATRI-X\app\data');
    DataDirPage.Values[0] := AutoDataDir;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // 静默安装没有交互：跳过数据目录页（否则空值会触发 Inno 的路径校验而中止安装），
  // 数据目录由 ssPostInstall 按 <安装目录>\data 兜底。
  Result := (PageID = DataDirPage.ID) and WizardSilent;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  NewDefault: string;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    // 这里 {app} 已初始化（用户选完程序目录）；静默安装不会走向导，最终兜底在
    // CurStepChanged(ssPostInstall) 里（空值 → <安装目录>\data）。
    NewDefault := AddBackslash(WizardDirValue) + 'data';
    if (Trim(DataDirPage.Values[0]) = '') or
       ((AutoDataDir <> '') and (DataDirPage.Values[0] = AutoDataDir)) then
      DataDirPage.Values[0] := NewDefault;
    AutoDataDir := NewDefault;
  end;
  if CurPageID = DataDirPage.ID then
  begin
    if TrimTrail(Trim(DataDirPage.Values[0])) = '' then
    begin
      MsgBox('请选择一个数据与配置目录。', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: string;
  LegacyDir: string;
  FinalizeOk: Boolean;
  Migrate: Boolean;
  Migrated: Boolean;
begin
  if CurStep <> ssPostInstall then
    exit;
  DataDir := TrimTrail(Trim(DataDirPage.Values[0]));
  if DataDir = '' then
    DataDir := AddBackslash(ExpandConstant('{app}')) + 'data';
  LegacyDir := TrimTrail(ExpandConstant('{userappdata}\ATRI-X'));
  ForceDirectories(DataDir);
  // 1) 先写安装标记（UTF-8 JSON 只能由 Python 写）：即使后面迁移失败，程序也知道
  //    安装器选的数据目录；若这一步失败就跳过迁移，保持旧位置可用。
  FinalizeOk := RunFinalize(DataDir, False);
  Migrated := False;
  if not FinalizeOk then
    MsgBox('写入安装标记失败，ATRI-X 将按默认位置读取数据。', mbError, MB_OK)
  else
  begin
    // 2) 旧数据迁移：旧位置有东西、目标还是空的、且不是同一个位置。
    Migrate := False;
    if DirExists(LegacyDir) and
       (LowerCase(LegacyDir) <> LowerCase(DataDir)) and
       DirHasEntries(LegacyDir) and
       (not DirHasEntries(DataDir)) then
    begin
      if WizardSilent then
        Migrate := True
      else
        Migrate := MsgBox(
          '检测到旧的用户数据：' + #13#10 + LegacyDir + #13#10 + #13#10 +
          '是否把它迁移到新的数据目录？' + #13#10 + DataDir + #13#10 + #13#10 +
          '会先完整复制并逐文件核对，成功后才删除旧目录；失败则保留旧目录。',
          mbConfirmation, MB_YESNO) = IDYES;
    end;
    if Migrate then
    begin
      if RunDataMigration(LegacyDir, DataDir) then
      begin
        Migrated := True;
        RunFinalize(DataDir, True);
      end
      else
      begin
        MsgBox(
          '旧数据迁移失败，ATRI-X 将仍使用原位置：' + #13#10 + LegacyDir,
          mbError, MB_OK);
        DataDir := LegacyDir;
        ForceDirectories(DataDir);
        RunFinalize(DataDir, False);
      end;
    end;
  end;
  // 3) 卸载器要读的最终数据目录（注册表字符串，Unicode 安全）。
  WriteRegistryDataDir(DataDir);
end;

function InitializeUninstall(): Boolean;
begin
  InstalledDataDir := ReadDataDirFromRegistry;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  Target: string;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := InstalledDataDir;
  if DataDir = '' then
    DataDir := TrimTrail(ExpandConstant('{userappdata}\ATRI-X'));
  if not DirExists(DataDir) then
    exit;
  Target := ExpandConstant('{app}');
  if LowerCase(TrimTrail(DataDir)) = LowerCase(TrimTrail(Target)) then
    exit;
  if PathIsUnder(DataDir, Target) then
  begin
    // 数据在安装目录里（自包含安装）：必须一起删，否则卸载后残留。
    if not DelTree(DataDir, True, True, True) then
      MsgBox('删除安装目录内的数据目录失败：' + #13#10 + DataDir, mbError, MB_OK);
    exit;
  end;
  if PathIsUnder(Target, DataDir) then
  begin
    MsgBox('数据目录包含了程序目录，卸载不会自动删除它：' + #13#10 + DataDir,
      mbInformation, MB_OK);
    exit;
  end;
  // 静默卸载保持数据（运维脚本不该误删会话）；交互式卸载询问一次。
  if UninstallSilent then
    exit;
  if MsgBox(
    '是否同时删除用户数据？' + #13#10 + #13#10 +
    '数据目录：' + DataDir + #13#10 +
    '包含配置、会话数据库 .env 与日志。选「否」则保留，重新安装后可以继续使用原有会话。',
    mbConfirmation, MB_YESNO) = IDYES then
    DelTree(DataDir, True, True, True);
end;
