# Windows 桌面端与打包（PyInstaller）

把 `atri_x` 冻结成**不依赖 Python、不依赖 Docker** 的 Windows 可执行包，并提供桌面端、
托盘常驻、开机自启、安装/卸载与可选的内置 DSH 内核。目标用户是"双击就能用的 QQ 机器人
+ 本地助理"；需要容器化/多机部署时用 [`deploy/`](../deploy/README.md)，需要 Docker
Desktop 一键运维时用 [`deploy/windows/`](../deploy/windows/README.md)。

| 文件 | 角色 |
|---|---|
| `atri-x.spec` | PyInstaller 描述：收集 `atri_x` 全部子模块（延迟导入太多，静态分析不够）、打入两份数据资产、**保留 tkinter**（窗口版要用）、onedir 下出两个 exe 共用一个 `COLLECT`、可选打入 `runtime/` |
| `entry.py` | 冻结入口：`atri-x.exe` → `atri_x.__main__:main`；`atri-x-gui.exe` → `atri_x.gui.__main__`；两者都支持 `-m <模块>`（用 `runpy` 复刻 `python -m`） |
| `build.py` | 构建脚本：清产物 → 调 PyInstaller → **产物级核验**（跑 exe 本体，而不是只看构建退出码）；`--release` 一条命令再出便携版 zip + 安装包 |
| `build_portable.py` | 把 onedir 产物打成**解压即用**的便携版 zip（内置启动器/说明/构建元数据），并做**解压级核验**（解压 → 跑 exe/启动器 → 断言数据落在包内） |
| `build_installer.py` | 调 Inno Setup 把 onedir 产物打成一个 `setup.exe`，可选静默安装/卸载自检 |
| `assemble_kernel.py` | 组装内置 DSH 内核（node.exe + ACP 依赖闭包 + `kernel.json`），并**真启动 ACP 服务端握手核验** |
| `kernel_manifest.py` | `kernel.json` 清单读写（产物契约，与 `atri_x.sidecar.registry` 对应） |


面向用户的操作步骤（桌面端、接 QQ、托盘/自启、安装）见
[`docs/windows-app.md`](../docs/windows-app.md)。

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11 x64（PyInstaller 只能产**本机平台**的包） |
| Python | ≥ 3.11，且**带 `tkinter`**（窗口版必须；`python -c "import tkinter"` 能过） |
| PyInstaller | `python -m pip install pyinstaller`（6.22 已在 Python 3.14 上验证） |
| 内置内核（可选） | 一台装好 DSH 的机器（DSH 桌面端/CLI 的 `node_modules`）+ `node.exe` |

> 探测 PyInstaller 一律用 `python -m PyInstaller --version`。它的 console script 装在
> user-site 的 `Scripts/` 下，那个目录经常不在 `PATH` 上，
> `pyinstaller: command not found` **不能**当作"没装"的证据。

---

## 2. 构建

```bash
python packaging/build.py                    # onedir（默认）→ dist/windows/atri-x/
python packaging/build.py --onefile          # 单文件（只出控制台版）
python packaging/build.py --with-pillow      # 只保留 Pillow（图像编解码加速）
python packaging/build.py --with-extras      # 只保留 numpy/PyYAML
python packaging/build.py --with-all-optional  # 三个可选依赖全留（旧 --with-pillow 语义）
python packaging/build.py --check-env        # 只打印构建环境探测结果（Pillow 是否可导入等）
python packaging/build.py --no-gui           # 只出控制台版
python packaging/build.py --with-kernel      # 强制把 runtime/dsh-kernel 打进去
python packaging/build.py --no-kernel        # 即使 runtime/ 存在也不打
python packaging/build.py --verify-only      # 只核验已有产物
python packaging/build.py --no-verify        # 只构建
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--onefile` | 关 | 单文件 exe；启动时自解压，体积小但首启慢、杀软误报更多，且**依赖可写的 `%TEMP%`**（TEMP 不可用时直接启动失败，实测报 `Could not create temporary directory!`）。单文件模式**不出窗口版、不打内置内核** |
| `--name` | `atri-x` | 产物名（窗口版自动叫 `<name>-gui.exe`） |
| `--icon` | 无 | `.ico` 路径（两个 exe 共用） |
| `--no-gui` | 关 | 不生成窗口版 exe |
| `--with-kernel` / `--no-kernel` | 自动 | 默认看 `runtime/dsh-kernel/kernel.json` 是否存在；`--with-kernel` 时缺文件会直接报错 |
| `--with-installer` | 关 | 核验通过后再打 Inno Setup 安装包（见 §7；只支持 onedir） |
| `--installer-verify` | 关 | 打安装包并静默安装/卸载自检（隐含 `--with-installer`） |
| `--installer-output` | `dist/installer` | 安装包输出目录 |
| `--with-portable` | 关 | 核验通过后再打**便携版 zip**（解压即用，见 §8；只支持 onedir） |
| `--portable-verify` | 关 | 打便携版 zip 并**解压自检**（隐含 `--with-portable`） |
| `--portable-output` | `dist/portable` | 便携版 zip 输出目录 |
| `--release` | 关 | 一条命令出**两种交付物**：便携版 zip + 安装包，并各自自检（= `--with-portable --portable-verify --with-installer --installer-verify`） |
| `--with-pillow` | 关 | 只不排除 Pillow（其余可选依赖仍排除）。构建环境里 `import PIL` 不可用会以 exit 2 失败，除非同时给 `--allow-missing-pillow`（只警告） |
| `--with-extras` | 关 | 只不排除 numpy/PyYAML |
| `--with-all-optional` | 关 | 三个可选依赖全留（等价于旧的 `--with-pillow` 语义，也等价于 `ATRIX_PKG_KEEP_OPTIONAL=1`） |
| `--allow-missing-pillow` | 关 | 构建环境缺 Pillow 时降级为警告而不是失败（产物会退回纯标准库编解码） |
| `--check-env` | 关 | 只探测构建环境（Pillow 版本、计划排除的包）并退出，不构建 |
| `--dist` | `dist/windows` | 输出目录（已在 `.gitignore`） |
| `--work` | `build/pyinstaller` | PyInstaller 工作目录（已在 `.gitignore`） |

> 本目录的 `.py` **不在** `scripts/check_syntax.py` 的扫描范围（该脚本只扫
> `atri_x` / `tests` / `scripts`，`deploy/` 同理），改完请自己跑：
> `python -m py_compile packaging/build.py packaging/build_portable.py packaging/build_installer.py packaging/entry.py packaging/assemble_kernel.py packaging/kernel_manifest.py`

产物（onedir：不含内置内核时 40.1 MB / 50 个文件；含内置内核时 321.3 MB / 24510 个文件）：

```
dist/windows/atri-x/
├── atri-x.exe            ~10 MB 控制台版：service run / doctor / config check / -m <模块>
├── atri-x-gui.exe        ~10 MB 窗口版：桌面端（Tk + 托盘）
└── _internal/            python314.dll、标准库扩展、atri_x 模块、tkinter、数据资产
    └── runtime/          可选：内置 DSH 内核（node.exe + node_modules + kernel.json）

dist/portable/ATRI-X-<版本>-portable-win64.zip     # --with-portable / --release
dist/installer/ATRI-X-<版本>-setup.exe             # --with-installer / --release
```

---

## 3. 内置 DSH 内核（可选）

ATRI-X 通过 ACP 调 DSH（`dsh --profile acp`）。为了不在目标机器上另装 Node/dsh，
可以把内核打进包里：

```bash
python packaging/assemble_kernel.py --modules "C:\path\to\node_modules"
# → runtime/dsh-kernel/{kernel.json, node/node.exe, node_modules/...}
python packaging/build.py --with-kernel
```

`--modules` 指向**已装好 DSH** 的 `node_modules`（DSH 桌面端目录下就有）。脚本：

1. 跑 `node <dsh>/lib/bin.js --profile acp --dump-config`，取 acp profile 真实引用的包；
2. 按 `dependencies` 求闭包，剪掉 `@deepseek-ai/dsh-client-*` / `dsh-web*` /
   `dsh-host-frontend-static*`（ACP 用不到，省 ~50 MB）；
3. 拷 `node.exe` + 闭包，写 `kernel.json`（版本、包数、字节数、入口）；
4. **核验**：用产物自己的 node 起 ACP 服务端，发 `initialize` 帧，读回 `result` 才算过；
   启动期缺包就从源目录补拷后重试（`--heal-rounds`，默认 12）；
5. 核验通过写 `verified: true`；之后可随时 `python -m atri_x.sidecar verify --acp` 复验。

实测（DSH `0.2.0-rc.2` + Node `v24.19.0`）：384 个包 / 189.1 MB + `node.exe` 88.5 MB
（合计 277.6 MB），自动补拷 2 轮（22 + 1 个 peer 依赖）后 `initialize` 返回
`deepseek-harness-acp/0.0.1`。

运行期解析顺序（`atri_x/sidecar/registry.py`）：`ATRIX_DSH_KERNEL`（直接指内核目录）→
`ATRIX_RUNTIME_DIR`（运行时根，本地调试用）→ `<发行包>/runtime` → `<配置目录>/runtime`。
找到就回退优先级最高的那份，找不到就继续用 PATH 上的 `dsh`。

---

## 4. 产物级核验（`build.py` 自动做）

构建退出码为 0 **不代表**产物可用。脚本会跑 exe 本体：

| 步骤 | 证明什么 |
|---|---|
| `atri-x.exe --version` | 冻结入口/打包结构成立 |
| `atri-x.exe config check` | `assets_data` 真的进包、冻结路径能解出默认配置（漏 `--add-data` 时只有这一步会失败） |
| `atri-x.exe -m atri_x.platform.qq_onebot.smoke --help` | `-m` 转发可用（诊断入口都靠它） |
| `atri-x.exe -m atri_x.sidecar status --json` | 内置运行时是否可解析（打了内核时要求 `found: true`） |
| `atri-x-gui.exe --selftest` | tkinter 进包了、Tk 能建窗口、中文能渲染（只看退出码——窗口版没有控制台） |

除 `--version` 外，其余步骤把 `APPDATA` / `XDG_CONFIG_HOME` 指到
`build/pyinstaller-verify/`，不会往你真实的 `%APPDATA%\ATRI-X` 里写东西。

---

## 5. 冻结后的限制

| 限制 | 说明 | 对策 |
|---|---|---|
| **没有 Python 解释器** | exec 型 Hook / Go 源码 Skill / 任何 `python xxx.py` 子进程都找不到解释器 | 需要 Python Hook 就装系统 Python；或改用进程 Hook / Worker Hook 的可执行文件 |
| 排除 Pillow | 图像编解码退回纯 Python（慢但可用） | `--with-pillow` 重新打包；打包前可用 `--check-env` 确认构建环境真的装了 Pillow |
| 内置内核要自己组装 | 内核从"已装 DSH 的机器"拷，不联网安装 | 换 DSH 版本后重跑 `assemble_kernel.py`；不想要就 `--no-kernel` |
| 无自动更新 | 冻结包不能 `git pull` | 重装覆盖（`python -m atri_x.dist.install install --force`）或整目录替换 |
| 无代码签名 | SmartScreen 首次运行会警告 | 正式分发需买签名证书；`--onefile` 误报更多，优先 onedir |
| 托盘/单实例只在窗口版 | `atri-x-gui.exe` 有托盘与单实例；控制台版没有 | 用窗口版当日常入口 |
| 窗口版的 stdout | `console=False` 时没有控制台，`print` 会被吞 | 诊断请用 `atri-x.exe`（控制台版） |

---

## 6. 跑起来

```bash
# 桌面端（窗口版；关窗口进托盘，后台服务继续跑）
dist\windows\atri-x\atri-x-gui.exe

# 本地 TUI（前台，含已启用平台与 Cron）
dist\windows\atri-x\atri-x.exe run

# 无界面服务（headless 主平台 + [platform.cli] server + 已启用平台）
dist\windows\atri-x\atri-x.exe service run

# 部署验收 / QQ 协议层冒烟 / 内置内核核验
dist\windows\atri-x\atri-x.exe doctor --no-model
dist\windows\atri-x\atri-x.exe -m atri_x.platform.qq_onebot.smoke --json
dist\windows\atri-x\atri-x.exe -m atri_x.sidecar verify --acp

# 安装到 %LOCALAPPDATA%（快捷方式 + 卸载项 + 可选自启）
dist\windows\atri-x\atri-x.exe -m atri_x.dist.install install --autostart
```

配置与数据：`%APPDATA%\ATRI-X`（首次运行自动生成 `app.toml` / `services.toml` /
`state.toml` / `SOUL.md` / `.env.example`），日志按天写到同一目录下。
桌面端首次启动会自动把 `[platform.cli.server].enabled` 置 `true`（写前留 `.bak`），
并把本地 token 写进同目录 `.env` 的 `ATRIX_CLI_LOCAL_TOKEN`。
QQ OneBot 的配置与协议端（NapCat / LLOneBot）步骤见
[`docs/windows-app.md`](../docs/windows-app.md) §5。

---

## 7. 安装包（Inno Setup）

把 onedir 产物打成单个 `setup.exe`（用户级安装，不弹 UAC）：

```bash
# 1) 装 Inno Setup（用户级便携模式；不需要管理员）
#    官方安装包支持 /PORTABLE=1 /CURRENTUSER，装到任意目录
#"innosetup-7.1.0-x64.exe" /PORTABLE=1 /CURRENTUSER /VERYSILENT /DIR="C:\tools\InnoSetup7"

# 2) 打包（ISCC 按 --iscc → ATRIX_ISCC → packaging/tools/ISCC.exe → 常见安装路径 查找）
python packaging/build_installer.py
python packaging/build_installer.py --verify        # 静默安装/卸载自检
python packaging/build.py --with-kernel --installer # 构建完直接打安装包
python packaging/build.py --installer-verify        # 构建完打安装包并自检

# 产物
dist/installer/ATRI-X-<版本>-setup.exe
```

| 项 | 值 |
|---|---|
| 安装目录 | `%LOCALAPPDATA%\ATRI-X\app`（与 `atri-x -m atri_x.dist.install` 一致） |
| 用户数据 | `%APPDATA%\ATRI-X`（卸载默认保留；交互式卸载会问一次） |
| 权限 | `PrivilegesRequired=lowest`：全程 HKCU，不触发 UAC |
| 快捷方式 | 开始菜单 `ATRI-X`（指向 `atri-x-gui.exe`）+ 开始菜单卸载项；桌面图标是**可选**任务 |
| 开机自启 | **默认勾选**，写 HKCU `Run\ATRI-X`（与 `atri_x/gui/autostart.py` 同值名）；卸载自动删 |
| 安装标记 | 安装器在 `{app}\install.json` 写 `"installer":"inno"`；`atri-x ... status` 因此能提示用 `unins000.exe` 卸载，而不是脚本强删 |
| 卸载 | Inno 自带 `unins000.exe`；卸载前 `taskkill` 掉可能还在跑的 GUI/后台服务（后台服务不持有 GUI 互斥量） |

`build_installer.py --verify` 做的事：把安装包**复制到本地临时目录**、静默装到短路径
（默认 `C:\Users\<user>\AtriXVerify`）、断言两个 exe + `install.json` + `unins000.exe`
都在、跑一次 `atri-x.exe --version`、再静默卸载并等目录被清空。实测 0.9.9（含内置内核的
318.8 MB onedir → 56.5 MB 安装包）：安装 51.8 s / 23865 个文件，卸载 14.4 s。

### 7.1 构建环境的两个坑（实测）

1. **不要在 DSH 工作区（如 `<工作区根>\...` 这类受限目录）里直接双击安装包**。DSH 的
   Windows ACL 沙箱把工作区内的对象标成 **Low integrity**（`icacls "<安装包>"` 里能看到
   `Mandatory Label\Low Mandatory Level:(I)(NW)`），于是工作区里的 exe 一启动就是
   Low IL 进程；它**写不了 Medium IL 的 `%TEMP%`**，Inno 会在第一步报
   「安装程序无法创建目录 `%TEMP%\is-XXXX.tmp`，错误 5: 拒绝访问」并以 exit=1 退出
   （无日志、无残留）。同一份产物**复制到工作区外**（桌面/下载/任意本地目录）后完全正常：
   实测 exit=0、35+ s 装完 24513 个文件、`--version` 与 `config check` 均通过。
   - `icacls "<安装包>" /setintegritylevel Medium` **提不动**（需要 `SeRelabelPrivilege`），
     就地改标签这条路走不通；`Copy-Item` 复制到工作区外即可拿到 Medium 标签。
   - 自查：`icacls "<安装包>" | findstr Mandatory`，出现 `Low Mandatory Level` 就是本问题。
   - 同一个受限令牌还会让 HKCU 写入失败（`RegCreateKeyEx failed; code 5`，卸载项建不出来、
     安装回滚），以及让 `dist\windows\atri-x\atri-x.exe` 写 `%APPDATA%` 失败——本地冒烟
     测试同样要先把产物复制到工作区外再跑。
   - `build_installer.py --verify` 本来就会把安装包复制到 `%TEMP%` 再装，所以自检一直是绿的；
     只有"就地运行"才会踩到。
2. **安装根路径要短**。内置内核里有 190 字符以上的相对路径（`@opentelemetry` 的嵌套
   包），安装根 + 191 必须 ≤ 260（MAX_PATH）。默认的 `%LOCALAPPDATA%\ATRI-X\app`
   在普通用户配置下没问题；如果 profile 路径极深（例如隔离环境把 `LOCALAPPDATA`
   重定向到几十层目录），请用 `/DIR=` 指一个短路径，或给 `--verify-dir` 指短目录。
   `--verify` 默认会优先选短目录。

### 7.2 Inno Setup 6 / 7

`.iss` 同时兼容 6.7+ 与 7.x（`ArchitecturesAllowed=x64compatible` 需要 6.3+）。
简体中文界面依赖编译器自带的 `Languages\ChineseSimplified.isl`：Inno 7 自带，Inno 6.7
便携安装没有（此时安装包只有英文界面，脚本会打印提示）。`find_iscc` 默认优先找
Inno Setup 7。

---

## 8. 便携版 zip（解压即用）

`--with-portable`（或 `--release`）会把**已经核验过的** onedir 产物打成单个 zip。
它和安装版是同一份产物，区别只在"怎么交到用户手里"：

```bash
python packaging/build.py --with-kernel --with-portable --portable-verify   # 构建 + 便携包 + 解压自检
python packaging/build.py --with-kernel --release                          # 构建 + 便携包 + 安装包（都自检）
python packaging/build_portable.py --verify                                 # 只对已有 dist 重打便携包并自检
# 产物：dist/portable/ATRI-X-<版本>-portable-win64.zip（+ 同名 .zip.sha256）
```

zip 内布局（顶层目录名刻意取短，给 Windows 的 `MAX_PATH` 留余量）：

```
ATRI-X-<版本>/
├── atri-x.exe / atri-x-gui.exe / _internal/…   ← onedir 产物原样（内置 DSH 内核在 _internal\runtime）
├── start-portable.bat    便携模式启动：APPDATA 指到 .\data\appdata，数据留在解压目录里
├── start-normal.bat      标准模式启动：数据在 %APPDATA%\ATRI-X（与安装版共用一份）
├── debug-console.bat     调试控制台：打印 doctor 报告，并留一个命令行窗口
├── README.txt            使用/排障说明（UTF-8 BOM + CRLF，记事本可读）
└── build-info.json       构建元数据（版本、Python/PyInstaller、内核版本、文件数）
```

| 项 | 便携版 zip | 安装版 setup.exe |
|---|---|---|
| 交付形态 | 解压即用（绿色版） | 用户级安装（不弹 UAC） |
| 程序位置 | 解压目录 | `%LOCALAPPDATA%\ATRI-X\app` |
| 用户数据 | 便携模式 `<解压目录>\data\appdata\ATRI-X`；标准模式 `%APPDATA%\ATRI-X` | `%APPDATA%\ATRI-X` |
| 注册表 | 不写（除非用户在桌面端自己开自启） | HKCU 卸载项 + 可选开机自启 |
| 卸载 | 直接删文件夹（便携模式下不残留数据） | `unins000.exe`；用户数据默认保留 |
| 适合 | 快速试跑、拷给别人复现问题、绿色部署 | 普通用户长期使用 |

`--portable-verify` 做的事：把 zip **解压到本地短路径**（默认
`C:\Users\<用户>\AtriXPortableVerify`），跑 `atri-x.exe --version` / `config check` /
`-m <模块>` / `-m atri_x.sidecar status`（打了内核时要求 `found: true`），再跑窗口版
`--selftest` 与 `start-portable.bat --selftest`，最后用 `debug-console.bat --version`
断言**配置确实落在解压目录的 `data\appdata` 里**、而不是用户真实的 `%APPDATA%`。
自检目录默认用完即删。

限制：

- **必须解压到短路径**。内置内核里有 191 字符级的相对路径，加上顶层目录后，解压根目录
  超过约 55 字符（总长 260）时 Windows 资源管理器解压会失败。构建时会打印建议上限。
- zip 用 deflate 而不是 lzma：资源管理器双击即可解压，不依赖 7-Zip。
- 发行目录里的顶层 `debug.log`（输入法日志）与 `install.json`（安装版标记）不会进便携包。
- 无代码签名，SmartScreen 仍可能提示；`start-*.bat` 只做「设 `APPDATA` + 调 exe」，
  不包含业务逻辑，便于排查。

---

## 9. 排障

**Q：`config check` 报配置为空 / 首次启动没有生成 `app.toml`。**
A：说明 `atri_x/config/assets_data` 没进包——检查 `atri-x.spec` 的 `datas`，或重跑
`python packaging/build.py --verify-only` 看 `config check` 那一步是否失败。

**Q：窗口版双击没反应 / 一闪而过。**
A：先确认 `atri-x-gui.exe --selftest` 的退出码；再看配置目录下 `gui/service.out.log`
（GUI 拉起的 `service run` 的 stdout/stderr）。窗口版没有控制台，报错走弹窗与日志。

**Q：GUI 一直显示"已断开"。**
A：`atri-x.exe -m atri_x.dist.install status` 看装在哪；`atri-x.exe service run` 手动起一次
看控制台日志；确认 `app.toml` 的 `[platform.cli.server].enabled = true`、`.env` 里
`ATRIX_CLI_LOCAL_TOKEN` 与 `[platform.cli.server.tokens]` 的映射对得上。

**Q：`-m atri_x.sidecar status` 说 `found: false`。**
A：这次构建没打内核（`--no-kernel` 或 `runtime/` 不存在）。要么重跑
`assemble_kernel.py` + `--with-kernel`，要么设 `ATRIX_DSH_KERNEL` 指向现成内核目录。

**Q：`doctor` 里 `model_credentials failed`。**
A：正常的首次状态；在 `services.toml` 的 `[providers.*].api_key_env` 指定环境变量名、
并在系统环境变量或 `%APPDATA%\ATRI-X\.env` 里填值。

**Q：杀软报毒/删除 exe。**
A：PyInstaller onedir + 不开 UPX 误报最少；`--onefile` 更容易被拦。加白名单或做代码签名。

**Q：双击安装包报「安装程序无法创建目录 `%TEMP%\is-XXXX.tmp`，错误 5: 拒绝访问」。**
A：见 §7.1 第 1 条。先看安装包所在位置：`icacls "<安装包>" | findstr Mandatory` 若显示
`Low Mandatory Level`，说明它是在 DSH 工作区（受限目录）里被构建/存放的，Low IL
进程写不了 `%TEMP%`。把安装包复制到工作区外（桌面/下载）再双击即可，不需要重打包。
