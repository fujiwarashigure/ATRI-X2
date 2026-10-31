# SnowLuma 使用与配置教程（ATRI-X 可选的 QQ 协议端）

> 独立文档，可单独转发/存档，不需要同时打开其它 ATRI-X 文档。
> 适用：**ATRI-X 1.1.3**（Windows 桌面端 / 便携版 / 安装版）+ **SnowLuma 2.2.0 win-x64**。
> 本文路径示例统一写成 `D:\SnowLuma-v2.2.0-win-x64`；你放在别处就按实际改。
> 最后更新：2026-10-28。

---

## 0. 先读这一页（TL;DR）

ATRI-X 1.1.3 的**默认协议端是随包内置的 NapCat**（开箱即用）；**SnowLuma 是可选的备用
协议端**，只有你想换一套协议端时才需要下面这些步骤。ATRI-X **不内置 SnowLuma**
（许可原因，见 §11），需要你自行下载安装。
完整接入一共六步：

1. 解压 SnowLuma，**手动启动一次**：`launcher.bat`（或 `node.exe index.mjs`）。
2. 浏览器打开 `http://127.0.0.1:5099/`，用控制台打印的 `admin` 初始密码登录 → 同意 EULA/隐私 → 设置新密码。
3. 在 SnowLuma WebUI 的「进程注入」页注入本机 QQ 并完成 QQ 登录，账号会自动接入 OneBot。
4. 确认 SnowLuma 里有**正向 WebSocket 服务端**（默认 `127.0.0.1:3001`，令牌自动生成）。
5. 让 ATRI-X 找到 SnowLuma：设环境变量 `ATRIX_SNOWLUMA_DIR=<SnowLuma 目录>`（或把它放到自动探测目录），**完全退出并重启 ATRI-X 桌面端**。
6. 桌面端「平台」页把协议端从默认的 **NapCat** 切到 **SnowLuma** 并保存 → **重启桌面端** → 「在线」页「连接（上线）」。

> **最容易卡住的一步是第 5 步**：ATRI-X 不扫全盘，只认 `ATRIX_SNOWLUMA_DIR` 和四个固定目录；
> 目录不对就会显示「SnowLuma：不可用（未找到…）」。见 §6。
>
> **不想折腾？** 默认的 NapCat 已经能直接跑（见 [Windows 桌面端 §5.1](windows-app.md)）。
> 本文只在你想用 SnowLuma 时才需要。

---

## 1. 概念：三样东西各管什么

### 1.1 ATRI-X 是 OneBot v11 的「正向 WS 客户端」

ATRI-X 不直接连 QQ，它连的是**协议端**的 WebSocket 服务端（`ws://127.0.0.1:<端口>/`）。
所以 QQ 侧换什么协议端，ATRI-X 的代码都不用改；`platform.name()` 恒为 `qqonebot`。

```
QQ NT 客户端  ←注入/挂钩←  SnowLuma  ←正向 WebSocket:3001←  ATRI-X 后台服务  ←→  AI / 工具
                            （协议端）                          （OneBot 客户端）
```

### 1.2 SnowLuma 是什么

SnowLuma 是另一套 OneBot v11 协议端，和 NapCat 同类：**自己不带 QQ**，而是往本机已经安装的
QQ NT 客户端里注入 hook，然后把 QQ 的消息/动作翻译成 OneBot v11 接口。它自带：

- 一个 **WebUI**（默认 `http://127.0.0.1:5099/`，账号密码登录，不是 NapCat 那种带令牌的 WebUI）；
- **正向 WS 服务端**（`wsServers`，默认端口 `3001`）、HTTP 服务端（默认端口 `3000`）、
  反向 WS 客户端（`wsClients`，ATRI-X **不用**它）；
- 进程注入、多 QQ 账号、日志、通知等管理能力。

关键点：**ATRI-X 只读 SnowLuma 的配置**（`config/onebot_<QQ号>.json`），
从里面取「端口 + 访问令牌」，**绝不修改 SnowLuma 的任何文件**。

### 1.3 为什么 SnowLuma 是可选项、却不随包分发

SnowLuma 是非商业源码可见许可 + 专有原生组件，其 EULA §5.4 要求
「并入第三方安装包 / 镜像 / 自动化脚本部署须事先书面授权」。
所以 ATRI-X 发行包**不内置**它，只提供本教程这种「用户自备、ATRI-X 去接入」的形态。

同一份 ATRI-X 发行包里**内置了一份 NapCat**（`qqproto\NapCat.52230.Shell` + 便携 QQ），
它就是**默认协议端**：装好就能用，不需要任何额外准备。Catalog 有两条路：

| | 内置 NapCat（**默认**） | SnowLuma（可选备用，本教程） |
|---|---|---|
| 是否随包 | ✅ 包里自带，离线可用 | ❌ 自行下载（本教程） |
| QQ 来源 | 包内**便携 QQ**（也可用本机 QQ） | 注入**本机已安装**的 QQ NT |
| 登录方式 | 「在线」页启动协议端后扫码 | 在 QQ 客户端里登录（扫码/账密） |
| WebUI | `http://127.0.0.1:6099/webui`（带令牌） | `http://127.0.0.1:5099/`（账号密码） |
| OneBot 端口 | 桌面端启动时生成 | 默认 `3001`（读自它自己的配置） |
| 适合 | **大多数用户**：离线、开箱即用、不想装 QQ | 已装 QQ、想和日常 QQ 共存/多账号，或想换一套协议端 |

> **两套协议端互斥**：NapCat 与 SnowLuma 都是往 `QQ.exe` 注入 hook，同一台机器上不能同时接管
> 同一个 QQ。切换前先停旧的、确认旧注入已退出。

---

## 2. 准备工作

### 2.1 环境与软件清单

- Windows 10/11 x64；
- **QQ NT 客户端**（官方安装版，先装好；建议用小号，§11 有风险说明）；
- ATRI-X 1.1.3（便携版解压即用，或安装版）；
- SnowLuma 2.2.0 win-x64 发行包（解压后应包含 §2.3 的文件）。

### 2.2 目录怎么放

SnowLuma 的**配置、日志都是相对「当前工作目录」**生成的（`config\`、`logs\` 建在它自己目录下），
所以请固定一个目录，并注意：

- 路径尽量**短、纯 ASCII**（避免中文、空格、`&`、括号等，省掉一类引号/编码问题）；
- 不要放 `C:\Program Files`、`C:\Windows` 等需要管理员权限/受保护的位置；
- 不要放在只读盘、OneDrive 同步目录或会被安全软件实时锁定的目录；
- ATRI-X 与 SnowLuma 可以是任意相对位置，**不要求**放一起。

### 2.3 检查 SnowLuma 包是否完整

解压后的根目录至少应有：

```
SnowLuma-v2.2.0-win-x64\
├─ launcher.bat            # 双击启动（内部先校验 Node 版本）
├─ index.mjs               # 运行时主程序（约 12 MB）
├─ node.exe                # 自带 Node v24.22.1（满足 ^24.22.1 || >=26.12.1）
├─ package.json
├─ release.json
├─ EULA.md / PRIVACY.md
├─ client\                 # WebUI 静态资源
└─ native\                 # 注入用的原生组件（.dll / .node）
```

自检命令（在 SnowLuma 目录里打开 PowerShell / cmd）：

```bat
cd /d D:\SnowLuma-v2.2.0-win-x64
node.exe -v
```

应输出 `v24.22.1`（或满足 `^24.22.1 || >=26.12.1` 的版本）。版本不符时 `launcher.bat` 会直接退出并暂停窗口。

> **ATRI-X 判定「这是不是一个 SnowLuma 目录」的条件**：目录里同时存在 `index.mjs` 和 `package.json`。
> 少一个就会被当成"没找到"。

### 2.4 端口清单（先确认没被占用）

| 端口 | 用途 | 谁在用 |
|---|---|---|
| `5099` | SnowLuma WebUI（HTTP，账号密码登录） | SnowLuma |
| `3001` | SnowLuma **正向 WebSocket 服务端**（OneBot v11） | SnowLuma ↔ ATRI-X |
| `3000` | SnowLuma HTTP 服务端（OneBot v11） | SnowLuma（ATRI-X 不用） |
| 由 ATRI-X 管理 | 本地 CLI 服务 / 健康接口等 | ATRI-X 自身 |

查占用（PowerShell）：

```powershell
Get-NetTCPConnection -LocalPort 3001,5099,3000 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

有输出就先处理：`Get-Process -Id <OwningProcess>` 看是谁（可能是另一个 OneBot 协议端）。

---

## 3. 第一次启动 SnowLuma（必须手动做一次）

> 首次启动**不能**靠 ATRI-X 的「启动协议端」按钮：那时 SnowLuma 还没写出 OneBot 配置，
> ATRI-X 读不到端口，按钮是灰的/会提示不可用。**必须先手动启动一次完成首启。**

### 3.1 启动方式

在 SnowLuma 目录里双击 `launcher.bat`（推荐），或者命令行：

```bat
cd /d D:\SnowLuma-v2.2.0-win-x64
node.exe index.mjs
```

两种方式的等价性：`launcher.bat` 就是先跑 `check-node-version.cjs`，再跑 `node ./index.mjs`，
最后 `pause`。**不要**在别的目录里用相对路径启动（配置会落到错误的 `config\`）；
如果自己建快捷方式，务必把「起始位置」设成 SnowLuma 目录。

> 这个控制台窗口就是 SnowLuma 本体：**关掉窗口 = SnowLuma 退出 = QQ 注入被卸载**。
> 需要它常驻时最小化，不要关。

### 3.2 记下初始凭据（只打印一次）

启动成功后，控制台里会有一行（不同版本格式略有差异，认 `initial credentials` 关键字）：

```
[INFO] [WebUI] initial credentials: user=admin password=1a2b3c4d5e6f7a8b
```

- 账号固定 `admin`（WebUI 只有一个后台账号）；
- 密码是**随机 16 位十六进制**，只在首次启动打印；之后写入 `config\webui.json` 的是 scrypt 哈希，无法反推；
- 首次登录后系统会强制你改成「强密码」。

**看到这一行先复制保存**（存在本地密码管理器里即可，不要发群里/贴图）。

### 3.3 登录 WebUI

浏览器打开：

```
http://127.0.0.1:5099/
```

- 如果 5099 被占，SnowLuma 会在启动日志里报错；可用环境变量 `SNOWLUMA_WEBUI_PORT=15099`
  换端口（见 §8.5），换完 ATRI-X 也会按 `config\webui.json` 里的 `port` 自动识别；
- 登录框要的是**访问密码**（账号固定 `admin`，登录页通常只让你填密码；部分界面会同时显示
  「输入访问令牌」的自动化入口，普通使用忽略它即可）；
- 页面提示不安全（`http://`）是正常的，本机回环访问即可；**不要**把 5099 暴露到公网。

### 3.4 首次使用引导（三步）

登录后是「首次使用设置」流程：

1. **阅读并同意**《用户协议 / EULA》与《隐私政策 / Privacy》——由你本人在 SnowLuma 里同意；
   ATRI-X **不会**替用户接受第三方协议。同意记录写入 `config\consent.json`。
   （脚本化场景也可用 `SNOWLUMA_ACCEPT_EULA=1` + `SNOWLUMA_ACCEPT_PRIVACY=1` 声明，
   但这属于"无人值守部署"，请自行确认合规。）
2. **设置访问密码**（替换掉临时密码）。规则：
   | 规则 | 要求 |
   |---|---|
   | 长度 | ≥ 10 位 |
   | 小写字母 | 至少 1 个 |
   | 大写字母 | 至少 1 个 |
   | 特殊符号 | 至少 1 个（`!@#$%…`） |
   | 空格 | 不允许 |
   | 建议 | ≥ 16 位、随机、唯一，只用于本机 WebUI |
3. 进入控制台（「完成首次使用设置」）。

> 初始密码一旦丢失又没改过：`config\webui.json` 里只有哈希，找不回。
> 处理办法见 §10.4。

### 3.5 首启后生成的东西

```
SnowLuma-v2.2.0-win-x64\
├─ config\
│  ├─ webui.json           # WebUI 端口/host/密码哈希/TLS 等
│  ├─ consent.json         # { "version": "...", "acceptedAt": "..." }
│  ├─ onebot.json          # OneBot 全局配置（可能没有，按需）
│  ├─ onebot_<QQ号>.json   # 每个账号一份 OneBot 配置（首次登录后生成）
│  └─ key.pem              # 启用 HTTPS 时才有
├─ logs\                   # 运行日志（默认；可用 SNOWLUMA_LOG_DIR 改）
└─ data\ / *.db            # 账号数据库（按版本不同，别手删）
```

**这些都属于 SnowLuma，ATRI-X 只读 `config\onebot*.json` 和 `config\webui.json`。**

---

## 4. 让 SnowLuma 接管 QQ（进程注入 + 登录）

### 4.1 先启动 QQ

用官方 QQ NT 客户端登录（或先停在登录窗口）。SnowLuma 的「进程注入」页会扫描本机可注入的
QQ 主进程；**QQ 没开就扫不到**。

> 建议：QQ 与 SnowLuma 用**同样的权限级别**运行（都以普通用户启动即可）。
> 一个以管理员运行、另一个不是，注入常会失败。

### 4.2 注入

1. SnowLuma WebUI →「进程注入」页；
2. 点刷新，列表里应出现 QQ 主进程（可能显示进程名/PID/版本）；
3. 选中它 → 注入（界面会提示「将向进程注入 SnowLuma，并开始监听登录状态」）；
4. 状态变成「等待登录」。

### 4.3 登录 QQ

在 QQ 客户端里完成登录（扫码或账密）。登录成功后：

- SnowLuma 总览页出现「在线账号」；
- 该账号**自动接入 OneBot 流程**，并生成 `config\onebot_<QQ号>.json`；
- 账号的「协议端点」显示正向 WS / HTTP 的连接状态。

### 4.4 确认账号在线

总览页应能看到：账号昵称、QQ 号、在线状态、协议端点连接情况。
「账号状态」长期为空/「等待登录」= QQ 登录没完成，或注入进程选错（多开 QQ 时尤其注意选哪个）。

### 4.5 退出注入 / 换号

- 想换协议端（比如切回内置 NapCat）或换 QQ：先在该页**卸载注入**，再退出 QQ 或退出 SnowLuma；
- 直接关 SnowLuma 控制台窗口也会卸载注入，但用界面卸载更干净；
- **不要**在两套协议端都注入的情况下反复切换，容易出现 hook 冲突、QQ 崩溃或收不到消息。

---

## 5. 配置 OneBot v11 正向 WebSocket

### 5.1 默认端点（开箱就已经有）

SnowLuma 新建配置时自动生成这些网络端点（每账号一份）：

| 名称 | 类型 | 默认地址 | 说明 |
|---|---|---|---|
| `ws-default` | 正向 WebSocket 服务端 | `ws://127.0.0.1:3001/` | **ATRI-X 连这个** |
| `http-default` | HTTP 服务端 | `http://127.0.0.1:3000/` | ATRI-X 不用 |
| — | 反向 WS 客户端（`wsClients`） | 空 | ATRI-X 不用，别配成反向 |

`ws-default` 的默认值：`host=127.0.0.1`、`port=3001`、`path=/`、`role=Universal`、
`messageFormat=array`、`reportSelfMessage=false`、`accessToken=`（启动时随机生成 32 字节 base64url）。

> **必须保留一个正向 WS 服务端且端口 > 0**。ATRI-X 的读取逻辑是：
> 在最新的 `onebot_<QQ号>.json` 里找 `networks.wsServers`，取**第一个端口 > 0 的项**。
> 如果把它删了/禁用了，ATRI-X 会显示「还没有可用的 OneBot 正向 WS 配置」。

### 5.2 在 WebUI 里核对（一般不用改）

WebUI →「账号配置」/「协议端点」→ 展开对应账号 → 找 `ws-default`：

- 确认「启用 WebSocket」处于开启状态；
- 端口 `3001`（可改，但改完两边都要同步，见 §8.6）；
- 路径 `/`；
- **授权 Token**：保持自动生成（≥16 位；留空 = 关闭鉴权，只建议本机用）；
- 消息格式选 **array（标准 OneBot 段）**，不要用 CQ 码字符串；
- `reportSelfMessage` 保持关闭（否则机器人自己的消息也会被当事件回灌）。

改完保存；界面提示「配置已保存，将在账号下次连接时应用」——重新注入/重新登录，或重启 SnowLuma 生效。

### 5.3 配置文件长什么样（只读参考）

`config\onebot_<QQ号>.json` 大致结构（键名以你的版本为准，**不要手改正在用的文件**）：

```json
{
  "networks": {
    "httpServers": [
      { "name": "http-default", "host": "127.0.0.1", "port": 3000, "path": "/",
        "enableWebSocket": false, "accessToken": "<随机>", "messageFormat": "array",
        "reportSelfMessage": false }
    ],
    "httpClients": [],
    "wsServers": [
      { "name": "ws-default", "host": "127.0.0.1", "port": 3001, "path": "/",
        "role": "Universal", "accessToken": "<随机>", "messageFormat": "array",
        "reportSelfMessage": false }
    ],
    "wsClients": []
  },
  "statusCommand": { "enabled": true, "swallow": false, "cooldownSeconds": 5, "trigger": "#sl" },
  "historySync": { "enabled": false },
  "notifications": { "channelIds": [] }
}
```

- 全局 `config\onebot.json` 是**默认覆盖层**；每账号 `onebot_<QQ号>.json` 覆盖它。
- ATRI-X 只读：`networks.wsServers[].port`、`.accessToken`，以及 `config\webui.json` 的 `port`。

### 5.4 验证 WS 端口活着

```powershell
Test-NetConnection 127.0.0.1 -Port 3001
# TcpTestSucceeded : True
```

或用浏览器访问 `http://127.0.0.1:3001/`：出现「Upgrade Required / 400」之类是**正常**的
（那是 WebSocket 端点，普通 HTTP 请求本就会被拒）。真正验证收发用 §9.2 的冒烟命令。

---

## 6. 让 ATRI-X 找到 SnowLuma（关键一步）

ATRI-X 不会扫全盘。它按下面的**固定顺序**定位 SnowLuma 目录：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | 显式参数 | 只有内部调用/测试用，桌面端不传 |
| 2 | 环境变量 `ATRIX_SNOWLUMA_DIR` | **推荐**，指向 SnowLuma 根目录（含 `index.mjs`） |
| 3 | `%LOCALAPPDATA%\SnowLuma` | 自动探测 |
| 4 | `%LOCALAPPDATA%\Programs\SnowLuma` | 自动探测 |
| 5 | `%USERPROFILE%\SnowLuma` | 自动探测 |
| 6 | `C:\SnowLuma` | 自动探测 |

判据：目录里同时有 `index.mjs` 和 `package.json`。注意变量名是 **`ATRIX_SNOWLUMA_DIR`**
（`ATRIX`，不是 `ATRI-X`），拼错等于没设。

### 6.1 方案 A：设环境变量（推荐）

当 SnowLuma 放在 `D:\SnowLuma-v2.2.0-win-x64` 这类非默认位置时，用这个。

**永久（推荐）**：在 cmd 里执行一次（只影响之后新启动的进程）：

```bat
setx ATRIX_SNOWLUMA_DIR "D:\SnowLuma-v2.2.0-win-x64"
```

然后**完全退出 ATRI-X**（托盘图标右键退出，别只关窗口），再重新启动。
如果重启后仍不生效，注销/重新登录一次 Windows（或重启资源管理器）让新环境变量传到桌面进程。

**临时（当前窗口）**：不写系统变量，适合测试：

```bat
set "ATRIX_SNOWLUMA_DIR=D:\SnowLuma-v2.2.0-win-x64"
cd /d "<ATRI-X 便携版目录>"
start-portable.bat
```

**包装启动脚本（便携版推荐，不污染系统）**：在 ATRI-X 便携版目录里新建
`start-with-snowluma.bat`，内容：

```bat
@echo off
set "ATRIX_SNOWLUMA_DIR=D:\SnowLuma-v2.2.0-win-x64"
call "%~dp0start-portable.bat" %*
```

之后双击它启动即可；换 SnowLuma 位置只改这一行。

> 环境变量必须在 **ATRI-X 桌面端进程启动之前**存在。桌面端只在启动时准备协议端环境、
> 读取 SnowLuma 的端口与令牌，运行中不会再读。

### 6.2 方案 B：放到自动探测目录

把（或拷贝/移动）整个 SnowLuma 目录放到 `%LOCALAPPDATA%\SnowLuma` 等位置之一即可，
不用设环境变量。例如：

```powershell
$dst = Join-Path $env:LOCALAPPDATA 'SnowLuma'
Move-Item 'D:\SnowLuma-v2.2.0-win-x64' $dst
```

- 优点：零配置；缺点：路径不由你控制、升级/备份时要记得这个位置；
- 移动后 `config\` 会跟着走；如果你之前是在原目录首启的，**配置也一起搬过去**即可。

### 6.3 怎么确认 ATRI-X 找到了

重启桌面端后看两处：

- **「在线」页**协议端卡片：`协议端环境：外部自备 D:\...\SnowLuma-v2.2.0-win-x64`
  以及 `SnowLuma WebUI：http://127.0.0.1:5099/`；
- **「平台」页** → `QQ（OneBot v11…）`：「协议端」下拉显示 **SnowLuma**。

如果显示「SnowLuma：不可用（未找到 SnowLuma…）」，按顺序自查：

1. `ATRIX_SNOWLUMA_DIR` 是否拼对、是否带引号问题（路径里别留引号）；
2. 该目录里是否真有 `index.mjs` + `package.json`；
3. 变量是否真的进了**桌面端进程**：用同一个启动方式打开 `cmd`，执行 `echo %ATRIX_SNOWLUMA_DIR%`；
   注意 `setx` 对"已经开着的资源管理器/启动器"不一定立刻生效；
4. 是否**完全重启**了 ATRI-X（托盘退出后重新打开）；
5. 便携版是否用 `start-portable.bat` / 包装脚本启动（直接双击 exe 也行，但环境变量来源要一致）。

---

## 7. ATRI-X 桌面端接入步骤（一次走通）

前提：§3~§5 已完成（SnowLuma 能启动、QQ 在线、3001 端口在听），并且你**已经决定从默认的
内置 NapCat 换成 SnowLuma**（默认路径不需要本文）。

### 7.1 重启 ATRI-X 桌面端

协议端环境在**桌面端启动时**准备：读 SnowLuma 的 `config\onebot_<QQ号>.json` 取端口/令牌，
并在 ATRI-X 自己的配置里**补空值**。所以：改完 SnowLuma 配置 / 刚完成首启 / 刚设好环境变量后，
都要**完全退出再启动**桌面端。

### 7.2 「平台」页核对并保存

打开「平台」→ `QQ（OneBot v11，NapCat / SnowLuma 等协议端）` 分组：

| 字段 | 期望值 | 说明 |
|---|---|---|
| 启用 | 勾选 | 不勾=平台不装配 |
| **协议端** | **SnowLuma** | 默认是内置 NapCat；这里要**显式切**过来并保存（见 §7.6） |
| 协议端 WS 地址 | `ws://127.0.0.1:3001/` | 桌面端会**自动补全**（只补空值） |
| access_token 环境变量 | `QQONEBOT_ACCESS_TOKEN` | 同上，仅在协议端有令牌时补 |
| token 值 | 留空 | 真实令牌写进 `.env`，不落 `app.toml` 明文 |
| 唤醒词 | `atri`（默认） | 群里 @机器人、回复或含唤醒词才响应 |

点「保存平台配置」。如果提示"将把协议端从 X 切换到 Y"的确认框，按提示操作
（切换只改 `protocol_backend`，**不会**动 WS 地址与令牌）。

> **"只补空值"规则**：ATRI-X 绝不覆盖你已写好的 `ws_url` / 令牌。
> 所以 SnowLuma 之后**改了端口或令牌**，ATRI-X 不会自动跟改，需要你手动同步（§8.6）。

### 7.3 「在线」页看四维状态并上线

「在线」页的协议端卡片把"在线"拆成四个状态：

| 维度 | 含义 | SnowLuma 场景下怎么看 |
|---|---|---|
| 进程 | 是否运行 | SnowLuma 由你自己/ATRI-X 托管启动；外部自备时 ATRI-X 只标注目录 |
| OneBot 连通 | WS 是否连上 | ATRI-X ↔ `ws://127.0.0.1:3001/` |
| QQ 登录 | `get_login_info` 是否成功 | 在 SnowLuma 里登录的号；未登录时显示「协议端已连上，但 QQ 未登录」 |
| ATRI 接入 | `[platform.qqonebot].enabled` | 点「连接（上线）」后才为真 |

操作顺序：

1. SnowLuma 已在运行（控制台窗口开着）→ **不要**再点「启动协议端」（会再起一个实例，端口冲突）；
   想改由 ATRI-X 托管，先完全退出你手动启动的那个 SnowLuma，再点「启动协议端」
   （它会新开一个控制台窗口 `node.exe index.mjs`，关窗即下线）。外部自备启动的 SnowLuma，
   ATRI-X 的「停止协议端」不会去动它（只停本程序启动的进程树）；
2. 点「打开 WebUI」可直达 `http://127.0.0.1:5099/`（账号密码登录）；
3. 点「检测端口」/「检测」刷新一次 TCP 探测；
4. 点「**连接（上线）**」：ATRI-X 写 `enabled = true` → 重启后台服务 → 轮询到「已连接」。

> 「启动协议端」按钮只有 `available=True` 时可用，即**已经能从 SnowLuma 配置里读到 WS 端口**。
> 首次使用前必须先手动启动过一次 SnowLuma（§3）。

### 7.4 完整顺序（照做即可）

```
① 手动启动 SnowLuma → 完成首启（同意协议 + 改密）
② SnowLuma「进程注入」注入 QQ → QQ 登录 → 账号在线（3001 在听）
③ 设 ATRIX_SNOWLUMA_DIR（或放自动目录）
④ 平台页把协议端从默认 NapCat 切成 SnowLuma → 保存
⑤ 完全退出并重启 ATRI-X 桌面端（这时才会去读 SnowLuma 的配置并补 ws_url/令牌）
⑥ 在线页 → 连接（上线）→ 群里 @ 一下验证
```

### 7.5 关于「SnowLuma 还没完成首启」提示（已知误报，可忽略）

ATRI-X 判断"是否已完成首启"用的是 `config\consent.json` 的启发式读法：找
`accepted` / `eulaAccepted` / `privacyAccepted` / `agreed` 为 true 的字段。
而 SnowLuma 2.2.0 实际写入的是 `{ "version": "...", "acceptedAt": "..." }`，
两者对不上 → **你已经同意了，ATRI-X 仍可能显示一行"还没完成首启"**。
这只是提示，不影响探测、启动与连接；按 §7.2/§7.3 正常操作即可（该文案的识别逻辑计划在下个版本修正）。

### 7.6 切回内置 NapCat（或切换协议端）

正确顺序（切换只改一个键，但环境在启动时准备）：

1. 「在线」页先**断开（下线）**；
2. SnowLuma 侧卸载进程注入，并退出 SnowLuma（控制台窗口关掉）；
3. 「平台」页「协议端」改选 **NapCat** → 保存（会二次确认）；
4. **重启桌面端**（首次切到 NapCat 会生成内置 NapCat 的 OneBot 端口/令牌/配置）；
5. 「在线」页「启动协议端」→「打开 WebUI」扫码（建议小号）→「连接（上线）」。

反向切回 SnowLuma 同理：先停 NapCat、再改下拉、再重启桌面端。

---

## 8. 配置参考（速查）

### 8.1 ATRI-X 侧：`[platform.qqonebot]`

`app.toml` 里（便携版：`<ATRI-X 目录>\data\appdata\ATRI-X\app.toml`；
标准/安装版：`%APPDATA%\ATRI-X\app.toml`）：

```toml
[platform.qqonebot]
enabled = true
# 协议端：napcat（默认，随包内置）/ snowluma（可选的备用方案，外置接入，就是本文）。
# 整个 [platform.qqonebot] 段不存在 = 新装 = napcat；
# 段存在但本键缺失 = 1.0.x 老配置 = napcat，升级不会自动切换。
# 想用 SnowLuma 就必须把它显式写成 "snowluma"。
protocol_backend = "snowluma"

# SnowLuma 的正向 WS 地址（默认端口 3001；以它 config 里的实际值为准）
ws_url = "ws://127.0.0.1:3001/"

# 令牌不写明文：这里只写环境变量名，值放 .env（或系统环境变量）
access_token_env = "QQONEBOT_ACCESS_TOKEN"

# 群里被 @/回复之外，消息里带这些词也唤醒（留空 = 只认 @ 与回复）
trigger_keywords = ["atri"]

send_file_mode = "base64"          # 跨机器安全；同机同文件系统可用 file_uri
inbound_dedup_enabled = true       # 断线重连不重放旧消息
```

`.env`（与 `app.toml` 同目录）：

```text
QQONEBOT_ACCESS_TOKEN=<SnowLuma 里那把 WS 令牌>
```

要点：

- `ws_url` 留空时适配器会回落到 `ws://127.0.0.1:6700/`（旧 NapCat 默认），**不是 3001**——
  所以一定要确认它被填成 3001（或你实际改的端口）；
- `access_token`（明文）优先级高于 `access_token_env`；一般只用后者；
- 「平台」页的 token 值输入框 = 直接写 `.env`，不会写进 `app.toml`。

### 8.2 文件位置对照

| 内容 | 便携版 | 标准/安装版 |
|---|---|---|
| `app.toml` / `.env` | `<ATRI-X 目录>\data\appdata\ATRI-X\` | `%APPDATA%\ATRI-X\` |
| 日志 | `<ATRI-X 目录>\data\appdata\ATRI-X\logs\`、`gui\service.out.log` | `%APPDATA%\ATRI-X\logs\` |
| SnowLuma 配置 | `<SnowLuma 目录>\config\` | 同左 |
| SnowLuma 日志 | `<SnowLuma 目录>\logs\`（`SNOWLUMA_LOG_DIR` 可改） | 同左 |

### 8.3 令牌是怎么同步的

```
SnowLuma config\onebot_<QQ号>.json
        └─ networks.wsServers[].accessToken   ──(ATRI-X 只读)──►  ATRI-X 生成/补全 access_token_env
                                                                    └─ 值写入 <配置目录>\.env 的 QQONEBOT_ACCESS_TOKEN
```

- ATRI-X **不改** SnowLuma 的任何文件；
- 只在这三个条件同时成立时才补：`setup.available`（读到端口）、ATRI-X 里 `ws_url` 为空、
  ATRI-X 里既没有 `access_token_env` 也没有明文 `access_token`；
- 想换令牌：在 SnowLuma 里重新生成 → 重启 SnowLuma/账号重连 → 把新令牌手动写进 ATRI-X 的 `.env`
  → 重启后台服务（或点「连接（上线）」）。

### 8.4 多 QQ 账号时用哪一个

SnowLuma 支持多账号，每账号一份 `config\onebot_<QQ号>.json`。
ATRI-X 的读取规则：**按修改时间取最新的那一份** `onebot_*.json`，再取其中第一个端口 > 0 的 WS 服务端。

- 一个 ATRI-X 只连**一个** WS 端点；
- 多账号且端口/令牌不同时，可能读到"另一个号"的配置；需要确定性时：
  - 只给要用的那个号启用正向 WS 服务端（其他号的 WS 关掉），或
  - 在「平台」页**手动填** `ws_url` + 令牌（填了就不会被自动覆盖）。

### 8.5 SnowLuma 常用环境变量（可选）

| 变量 | 作用 |
|---|---|
| `ATRIX_SNOWLUMA_DIR` | **ATRI-X 用**：SnowLuma 目录（见 §6） |
| `SNOWLUMA_WEBUI_PORT` | WebUI 端口（默认 5099） |
| `SNOWLUMA_WEBUI_HOST` | WebUI 绑定地址（默认 127.0.0.1，**不要**随便改成 0.0.0.0） |
| `SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD` | 无人值守时预设初始密码（≥8 位；登录后仍需改成强密码） |
| `SNOWLUMA_WEBUI_TLS` | WebUI 启用 HTTPS（需证书） |
| `SNOWLUMA_ONEBOT_WS_PORT` | 覆盖 `ws-default` 监听端口（只覆盖工厂默认项） |
| `SNOWLUMA_ONEBOT_HTTP_PORT` | 覆盖 `http-default` 监听端口 |
| `SNOWLUMA_ACCEPT_EULA` / `SNOWLUMA_ACCEPT_PRIVACY` | 声明已同意协议（无人值守部署用，自行确认合规） |
| `SNOWLUMA_LOG_DIR` / `SNOWLUMA_LOG_LEVEL` / `SNOWLUMA_LOG_FILE` | 日志目录/级别/是否写文件（`0` 关闭文件日志） |

### 8.6 改了端口/令牌/目录之后

| 改动 | 必须同步的动作 |
|---|---|
| SnowLuma WS 端口 | SnowLuma 生效 → 重启 ATRI-X 桌面端 → 检查「平台」页 `ws_url`（ATRI-X 只补空值，旧值要手动改） |
| SnowLuma WS 令牌 | SnowLuma 重新生成 → 把新值写进 ATRI-X `.env`（或「平台」页 token 值）→ 重启后台服务 |
| SnowLuma 目录移动 | 改 `ATRIX_SNOWLUMA_DIR`（或放到自动目录）→ 完全重启 ATRI-X |
| SnowLuma WebUI 端口 | 不用改 ATRI-X，它会按 `config\webui.json` 重新读（重启桌面端后生效） |
| 换 QQ 号 | SnowLuma 重新注入登录 → 生成新 `onebot_<uin>.json` → 重启 ATRI-X（必要时手动改 `ws_url`/令牌） |

---

## 9. 验证与冒烟

### 9.1 图形界面判读

| 「在线」页显示 | 说明 | 下一步 |
|---|---|---|
| 协议端进程在/OneBot 已连通/QQ 已登录/ATRI 已接入 | 全通 | 群里 @ 机器人试 |
| OneBot 已连通 / QQ 未登录 | SnowLuma 在但账号没登 | 去 SnowLuma 看注入与登录 |
| 协议端未监听 | 3001 没起 | 启动 SnowLuma；查端口占用 |
| SnowLuma：不可用（未找到…） | 目录/环境变量问题 | §6.3 |
| SnowLuma 不可用（还没有可用的 OneBot 正向 WS 配置） | 配置里没有端口>0 的 wsServers | §5.2 启用 WS |

> 端口明明没被占用、SnowLuma 也登录了，但 3001 就是起不来？先查 Windows 的**排除端口段**：
> `netsh interface ipv4 show excludedportrange protocol=tcp`。Hyper-V / WSL / Docker 会整段
> 保留动态端口（实测 `2942–3041`），落在里面的端口绑定会报 `WinError 10013`。换一个不在
> 保留段里的端口（如 `30001`），并在 ATRI-X 的 `ws_url` 里同步改掉即可。内置 NapCat 走的是
> 同一条链路，但桌面端会自动顺延端口，不需要手工改。

### 9.2 命令行冒烟（推荐先跑这个）

便携版用 `debug-console.bat`（它会设好便携数据目录并打开命令行），然后：

```bat
rem 只连、握手、get_login_info
atri-x.exe -m atri_x.platform.qq_onebot.smoke --url ws://127.0.0.1:3001/ ^
    --access-token "<你的令牌>" --backend snowluma --json

rem 发一条测试消息到群（把 123456 换成你的测试群）
atri-x.exe -m atri_x.platform.qq_onebot.smoke --url ws://127.0.0.1:3001/ ^
    --access-token "<你的令牌>" --backend snowluma --group 123456 --text "连通性测试"

rem 监听事件 20 秒，期间在群里 @ 一下机器人
atri-x.exe -m atri_x.platform.qq_onebot.smoke --url ws://127.0.0.1:3001/ ^
    --access-token "<你的令牌>" --backend snowluma --listen 20
```

期望：`connected`、`self_id`/昵称正确、发送返回 `message_id`、监听能打印 `post_type=message`。
`--access-token` 可省略（前提是 SnowLuma 关了 WS 鉴权；不建议）。

### 9.3 日志在哪

- **SnowLuma**：它的控制台窗口（stdout）+ `<SnowLuma 目录>\logs\`；
- **ATRI-X 后台服务**：便携版 `data\appdata\ATRI-X\gui\service.out.log`，或「日志」选项卡；
- 排查顺序：先 SnowLuma 日志（有没有注入/登录/WS 报错），再看 ATRI-X 的 `platforms[]` 状态与重连日志。

### 9.4 一条消息的全链路自检

1. 群里 @ 机器人发一句话；
2. SnowLuma 日志应出现该消息事件；
3. ATRI-X「在线」页应短暂显示处理中/心跳更新；「日志」页能看到入站与会话；
4. 机器人回复 → SnowLuma 日志出现发送动作 → 群里收到消息。

哪一步断了就查哪一段，不要一上来重装。

---

## 10. 故障排查

### 10.1 ATRI-X 找不到 SnowLuma

见 §6.3。补充：目录名里带空格没问题（环境变量加引号即可），但**目录本身**要在；
`ATRIX_SNOWLUMA_DIR` 指到 `SnowLuma-v2.2.0-win-x64` 这一层，不要指到 `native\` 或 `client\`。

### 10.2 找到了但显示「还没有可用的 OneBot 正向 WS 配置」

- SnowLuma 还没完成首启/还没登录账号 → 先手动启动并登录；
- 正向 WS 服务端被禁用/删除 → §5.2 重新启用（端口 > 0）；
- 只配了反向 WS 客户端（`wsClients`）→ ATRI-X 不支持反向，改成正向；
- 刚改完配置 → 账号重新连接/重启 SnowLuma 后配置才落盘，再重启 ATRI-X。

### 10.3 WebUI 打不开 / 5099 被占

- 看 SnowLuma 控制台是否有 `EADDRINUSE`；
- `Get-NetTCPConnection -LocalPort 5099 -State Listen` 找占用进程；
- 换端口：`set SNOWLUMA_WEBUI_PORT=15099` 后再启动 SnowLuma，访问 `http://127.0.0.1:15099/`
  （ATRI-X 重启桌面端后会按新端口显示 WebUI 地址）。

### 10.4 忘了 WebUI 密码

- 初始密码只在首次启动打印；改过密码后只存 scrypt 哈希，**无法反推**；
- 处理：先备份 `config\`（至少 `onebot_*.json`、`consent.json`），退出 SnowLuma，
  临时改名/删除 `config\webui.json` 后重新启动——它会重新进入"设置访问密码"流程并再打印一次
  初始密码（协议确认记录、账号配置与 OneBot 令牌都在别的文件里，不受影响），
  完成后再按 §7.2 核对令牌（如果重新登录导致令牌变化）；
- 不要手改 `webui.json` 里的哈希字段。

### 10.5 注入失败 / QQ 登录不上 / QQ 闪退

- QQ 没启动：先开 QQ 再刷新进程列表；
- 多开 QQ：选错主进程；只保留一个要用的 QQ 再注入；
- 权限不一致：QQ 与 SnowLuma 都用普通权限，或以相同权限启动；
- QQ 版本过旧/非官方客户端：换官方 NT 版本（以 SnowLuma 发布说明支持范围为准）；
- 安全软件拦截注入：临时加白名单（自己评估风险）；
- 已有一套 NapCat 注入：先卸载旧的再注入 SnowLuma（两套互斥）。

### 10.6 ATRI-X「协议端未监听」

- SnowLuma 控制台还开着吗？关窗即退出；
- `Test-NetConnection 127.0.0.1 -Port 3001` 不通 → 账号没登录/WS 没启用/端口被占；
- 端口没错但 ATRI-X 显示未监听 → 看 `ws_url` 是否被写成了旧端口（§8.6 手动改）。

### 10.7 OneBot 连上但「QQ 未登录」

SnowLuma 里账号掉线/被踢/注入被卸载。重新注入并登录；确认总览页账号在线。

### 10.8 连不上且报 401/403（令牌错）

ATRI-X 会把它识别为「令牌错误」并固定 30s 慢重试（不会疯狂重连）。
把 SnowLuma 的 `ws-default` 授权 Token 复制到 ATRI-X 的 `.env`（`QQONEBOT_ACCESS_TOKEN`），
重启后台服务或点「连接（上线）」。

> 令牌别经过聊天工具转发；复制时注意别带首尾空格/不可见字符。

### 10.9 连上但机器人不回话

- 群里是否 **@机器人** 或消息含唤醒词（默认 `atri`）？群策略是否 `off`？
- 「工具权限」里对应群的工具是否被禁？
- 「平台」页 `enabled` 是否勾选、后台服务是否重启过；
- 看 ATRI-X 日志里的入站事件与模型调用错误（API Key 未配也会"收得到但不回"）。

### 10.10 两套协议端冲突

现象：QQ 掉线、消息重复或混乱、注入后 QQ 崩溃。
处理：退出 ATRI-X 在线 → 停旧协议端并卸载注入 → 只保留一套 → 重启桌面端。

### 10.11 改完端口/令牌不生效

ATRI-X **只补空值**：已有的 `ws_url` / `.env` 令牌不会被覆盖。手动改（§8.6），
再重启后台服务（或点「连接（上线）」）。

### 10.12 换电脑 / 迁移

要搬：ATRI-X 便携目录（含 `data\`）+ SnowLuma 目录（含 `config\`）。
新机器上：装 QQ → 启动 SnowLuma（账号登录态与设备绑定，需**重新扫码**）→ 设 `ATRIX_SNOWLUMA_DIR`
→ 启动 ATRI-X → 核对 `ws_url`/令牌（令牌可能随新登录重新生成，需要更新 `.env`）。

### 10.13 升级 SnowLuma

1. 退出 ATRI-X 在线；2. SnowLuma 卸载注入并退出；3. **保留 `config\` 目录**，替换程序文件；
4. 启动新版本，检查首启/协议是否兼容；5. 核对 WS 端口/令牌；6. 重启 ATRI-X。

---

## 11. 安全、许可与风险（务必读）

- **许可**：SnowLuma 的 EULA §5.4 要求"并入第三方安装包 / Docker 镜像 / 自动化脚本部署须事先书面授权"。
  所以：不要把它打进你自己的安装包/镜像/一键脚本分发；本教程只覆盖"用户自己安装、ATRI-X 接入"。
  ATRI-X 自己的 Apache-2.0 也不覆盖 SnowLuma。
- **账号风险**：NapCat / SnowLuma 都基于对 QQ NT 的非官方 hook，存在**风控/封号风险**。
  建议用小号，不要拿主号做实验；合规路线是项目内置的 `[platform.qqofficial]`（QQ 官方机器人）。
- **只绑本机**：WebUI 默认 `127.0.0.1:5099`，OneBot WS 默认 `127.0.0.1:3001`。
  不要把这两个端口映射到公网；远程访问必须走 HTTPS + 强密码/令牌，并评估风险。
- **凭据**：WebUI 密码、WS 令牌都当密码保管；不要贴群、贴图、提交到 git；
  ATRI-X 的 `.env` 与 SnowLuma 的 `config\` 都属于敏感文件。
- **遥测/联网**：本教程基于 SnowLuma 2.2.0 发行包；其 EULA/隐私条款是最终依据。
  公开版本目前未发现遥测代码（不同版本可能变化），升级后请按条款自行复核。
- **数据备份**：SnowLuma 的 `config\`（含令牌）与 ATRI-X 的 `data\` / `%APPDATA%\ATRI-X\` 要一起备份；
  备份件同样含敏感信息。

---

## 12. 卸载与清理

1. ATRI-X：「在线」页断开（下线）→ 停止协议端（如果是 ATRI-X 托管的）；
2. SnowLuma：卸载进程注入 → 退出 SnowLuma；
3. 删除 SnowLuma 目录（想保留账号配置就只删程序文件、留 `config\`）；
4. 删除 `ATRIX_SNOWLUMA_DIR` 环境变量：`setx ATRIX_SNOWLUMA_DIR ""`（或系统属性里删）；
5. ATRI-X 里可保留 `ws_url`/令牌（不影响启动），也可清空；删 ATRI-X 数据目录等于清空其配置与会话。

---

## 13. FAQ

**Q：ATRI-X 能不能自带 SnowLuma，做到开箱即用？**
A：不能随包分发（§11 许可）。要"开箱即用"请用包里内置的 NapCat（「平台」页切过去 + 重启桌面端）。

**Q：SnowLuma 必须一直开着吗？**
A：是。它退出 = 注入卸载 = 机器人下线。可以最小化控制台窗口。

**Q：能用 NapCat 的 WebUI 地址/令牌习惯来配 SnowLuma 吗？**
A：不行。SnowLuma WebUI 是 `5099` + 账号密码；OneBot 令牌在账号配置的「授权 Token」里。

**Q：`ws_url` 到底用 3001 还是 3000？**
A：ATRI-X 连**正向 WS**（3001）。3000 是 HTTP 服务端，不是给 ATRI-X 用的。

**Q：为什么文档里说 SnowLuma 是"可选"，ATRI-X 默认用什么？**
A：默认是随包内置的 **NapCat**（开箱即用、离线可用）。SnowLuma 只是**可选的备用协议端**，
需要你自行安装并在「平台」页显式切过去；不装 SnowLuma 也完全不影响 ATRI-X 使用。

**Q：我想从默认 NapCat 换成 SnowLuma，需要先做什么？**
A：按 §3~§6 装好 SnowLuma 并让它被 ATRI-X 找到，然后 §7.2 在「平台」页把协议端切成
SnowLuma、保存、**完全重启桌面端**。切换前先在「在线」页断开并停掉旧协议端（两套互斥）。

**Q：能不能同时用 NapCat 和 SnowLuma？**
A：不能同时接管同一个 QQ。可以装两套，但同一时间只能起一套。

**Q：ATRI-X 会和 SnowLuma 抢配置吗？**
A：不会。ATRI-X 只读 SnowLuma 的 `config\onebot*.json` / `webui.json`，一个字节都不写。

**Q：便携版和安装版接法一样吗？**
A：一样。区别只是数据目录（§8.2）和启动方式（`start-portable.bat` vs 快捷方式）。

---

## 附录 A：ATRI-X 的读取逻辑（给维护者）

代码位置：

| 行为 | 代码 |
|---|---|
| 目录探测（`ATRIX_SNOWLUMA_DIR` → 4 个默认路径，判据 `index.mjs` + `package.json`） | `atri_x/gui/snowluma_proto.py`（`find_dir` / `is_snowluma_dir`） |
| 读端口/令牌：`config/onebot_*.json` 取 mtime 最新、`networks.wsServers` 第一个 `port>0` | `snowluma_proto.read_endpoint` |
| 读 WebUI 端口：`config/webui.json` 的 `port`，缺省 5099 | `snowluma_proto.read_webui_port` |
| 首启同意启发式（`accepted` 等键；对 2.2.0 的 `{version, acceptedAt}` 会误报） | `snowluma_proto._consent_state` |
| 托管启动：`<dir>\node.exe index.mjs`，`cwd` 固定为 SnowLuma 目录 | `snowluma_proto.launch` |
| 停止：只停本程序启动的进程树（`atri-x-proto-process.json` 记 PID/镜像/启动时间） | `atri_x/gui/proto_process.py` + `snowluma_proto.stop` |
| 新装判定与分派 | `atri_x/gui/proto_manager.py`（`configured_backend` / `prepare`） |
| 只补空值（`ws_url` / `access_token_env` / `.env`） | `proto_manager.apply_connection` |
| 桌面端启动时准备协议端 | `atri_x/gui/service.py`（`_prepare_qqproto`） |

三态默认规则（与「平台」页共用）：

- 整个 `[platform.qqonebot]` 段不存在 = **新装 = `napcat`**（默认协议端，随包内置）；
- 段存在但 `protocol_backend` 键缺失/为空 = 1.0.x 老配置 = `napcat`（升级不自动切换）；
- 显式 `snowluma` / `napcat` = 用户选择；非法值装配期报错。**SnowLuma 只有显式选择才生效。**

## 附录 B：最小检查清单（可打印）

- [ ] 已确认要换成 SnowLuma（默认的 NapCat 不需要这份清单）
- [ ] QQ NT 已安装并能登录（建议小号）
- [ ] SnowLuma 解压完整：`index.mjs` + `package.json` + `node.exe`（`node.exe -v` 符合要求）
- [ ] 第一次启动是**手动**的，已完成：同意 EULA/隐私 → 改强密码
- [ ] 「进程注入」已注入 QQ，总览页账号在线
- [ ] `ws-default` 正向 WS 已启用：`127.0.0.1:3001`、`path=/`、`messageFormat=array`、令牌已生成
- [ ] `Test-NetConnection 127.0.0.1 -Port 3001` = True
- [ ] `ATRIX_SNOWLUMA_DIR` 已设（或 SnowLuma 在自动探测目录）
- [ ] ATRI-X 已**完全重启**；「在线」页显示「外部自备 <SnowLuma 目录>」
- [ ] 「平台」页：协议端已从 NapCat **切到 SnowLuma** 并保存、`ws_url=ws://127.0.0.1:3001/`、`access_token_env=QQONEBOT_ACCESS_TOKEN`
- [ ] `.env` 里 `QQONEBOT_ACCESS_TOKEN` = SnowLuma 的令牌
- [ ] 「在线」页四维全绿 → 群里 @ 机器人有回复
