# 用户安全域沙箱

从 1.2.0（P0 批次）起，`shell` 与文件类工具默认在**按用户安全域复用的容器**里执行：
盒内尽量自由（普通命令不再逐条确认），盒外权限严格（宿主秘密不进入盒子）；联网默认
可用但只允许受控公网出站；资源默认有界，任务多了排队；环境坏了直接重建。

> 容器仍有共享内核等残余风险，本功能**不承诺零逃逸**；它是「把执行面与宿主控制面
> 分开 + 默认有界」，不是绝对安全。

## 1. 默认行为

| 场景 | 行为 |
| --- | --- |
| 普通 `shell` 命令 | 在当前用户安全域的容器里执行，工作目录固定 `/workspace`；不再逐条要求确认。 |
| 文件 / 图片 / 发送 | 路径解析到该安全域的工作区；绝对宿主路径默认拒绝（宿主文件访问是独立授权能力）。 |
| 联网 | 默认 `public` 档：公网可用，宿主 / 内网 / 云元数据 / 容器引擎端口被网络层与出口代理拒绝。 |
| 资源 | 内存硬顶、pids 上限、CPU 上限 + 竞争权重、磁盘配额、命令时限、空闲回收、数据保留期。 |
| 并发 | 资源不足时排队（有界、公平、可取消），拿到名额后才启动环境。 |
| 重置 | `/sandbox rebuild` 保留文件重建；`/sandbox wipe confirm` 清空文件；流量与审计账本**不会**清零。 |
| 后端不可用 | **明确报错并拒绝执行**，不会回落到宿主 Shell（`fail-closed`）。 |

`[sandbox].mode = "off"` 是显式的不安全旧模式（宿主 Shell），只建议在运维应急时使用；
桌面端与 `/sandbox` 都会明确提示「未保护」。

## 2. 安全域（租户键）

默认租户键 = **平台 + 机器人账号/实例 + scope + 用户 ID**，不是只按 QQ 号：

- 同一个人在群 A 的任务数据不会带进群 B；
- 同一部署里的不同 bot 不共用环境；
- 不同租户的容器、内部网络、工作区、私有 home、缓存互不可见；
- 同一环境内的命令默认串行，避免两个任务互相改坏文件；
- 不活跃用户只保留限额内的数据（保留期可配），不长期占用运行内存。

## 3. 配置

全部在 `app.toml` 的 `[sandbox]` 段；数值 `0` 表示「用资源档默认值」。

| 键 | 说明 |
| --- | --- |
| `mode` | `container`（默认）/ `off`（显式宿主模式，不安全）。 |
| `backend` | `auto` / `docker` / `podman`；Windows 上通过 Docker Desktop（WSL2 后端）接入同一后端。 |
| `engine_path` | 引擎可执行文件路径；空 = 按 `backend` 在 `PATH` 里找。 |
| `image` | 沙箱镜像名；镜像缺失时按内置 Dockerfile 自动构建。 |
| `apt_mirror` | 构建镜像时可选 Debian 镜像源（网络到官方源慢时用）。 |
| `profile` | `light` / `standard` / `heavy` / `dsh`。 |
| `network` | `offline` / `public`（默认）/ `restricted`（只允许 `restricted_hosts`）。 |
| `require_probe` | 启动时是否必须跑通隔离能力探测（默认 `true`）。 |
| `cpu_cores` / `memory_mb` / `pids_limit` / `disk_mb` | 覆盖资源档的上限。 |
| `command_timeout_seconds` / `long_task_timeout_seconds` | 普通 / 长任务时限。 |
| `idle_ttl_seconds` / `data_retention_hours` | 空闲回收与数据保留期。 |
| `max_environments` / `queue_max_size` / `queue_max_per_tenant` / `queue_wait_seconds` | 准入队列。 |
| `daily_traffic_mb` / `download_kbps` / `upload_kbps` / `max_connections` | 网络额度与限速。 |
| `allow_host_files` | 是否允许文件工具访问租户工作区外的宿主文件（默认 `false`，属于独立授权能力）。 |

`cpu_weight` 是**竞争权重**（忙时按比例分空闲 CPU），不是保底核数；`memory.high` 类阈值
只代表回收/节流，不是内存保底——界面与文档都不会把它说成「固定分配」。

## 4. 后端与镜像

- Linux 宿主：rootless Podman 优先，也可用受约束的 Docker；Windows 主程序通过
  Docker Desktop（WSL2 / Linux VM）接入同一套容器后端；
- WSL2 / 虚拟化 / 引擎不可用时明确提示安装或连接，不静默降级；
- 沙箱镜像只含 Python / git / curl / ripgrep / jq 等通用工具，**不含** ATRI-X 运行时、
  `.env`、SQLite、QQ 登录态或模型凭据；
- 容器以非 root 运行、根文件系统只读、`--cap-drop ALL`、no-new-privileges、默认 seccomp；
- 只挂载工作区、私有 home、临时目录三个可写面，不挂 docker socket / 宿主根 / 用户 home；
- 容器引擎的命令行只由控制面拼装，模型无法指定挂载、特权模式或启动参数。

## 5. 网络

三档：

| 档位 | 用途 |
| --- | --- |
| `offline` | 纯计算、文件处理、测试；容器 `--network none`。 |
| `public`（默认） | 日常联网任务；租户容器只接内部网络，唯一出口是同一网络的出口代理。 |
| `restricted` | 只允许 `restricted_hosts` 里的目标，其余拒绝。 |

强制点在**网络层与出口服务**，不是环境变量：

- 内部网络没有默认路由与外部 DNS，直连公网 / 内网 / 宿主在路由层就失败；
- 出口代理按「解析后的 IP」做 deny/allow，禁止回环、私网、链路本地（含云元数据）、
  CGNAT、组播、保留段、IPv6 ULA/链路本地，以及容器引擎/编排/运维端口；
- 每租户每日流量额度、上下行速率、并发连接数都有硬上限；到限后停止联网并返回明确原因；
- 额度跨容器重建累计，重置沙箱**不会**清零；代理状态写进 `<沙箱根>/tenants/<租户>/proxy/`。

公网出站仍可能被用于外传任务可见数据或滥用公网服务，所以还有审计、配额与停用能力；
它不是零风险档。

## 6. 输入输出

- 文件、媒体通过受控导入进入工作区（有单文件大小上限）；产物默认回到发起任务的会话；
- 导入导出检查路径穿越、符号链接 / Windows reparse point、FIFO / socket / 设备等特殊文件；
- 不把 ATRI-X 的 `.env`、SQLite、QQ 登录态、插件配置和模型凭据复制进盒子；
- 用户输入本身含秘密时，它仍会进入执行环境——不要承诺「联网也不会泄露」。

## 7. 命令与桌面端

聊天命令（`docs/commands.md` 也有速查）：

```text
/sandbox                 状态：安全域、后端、隔离探测、队列、今日流量
/sandbox reset           重启环境（文件保留）
/sandbox rebuild         按干净镜像重建（工作区/私有 home 保留）
/sandbox wipe confirm    清空全部文件并重建（必须 confirm）
/sandbox probe           重新跑隔离能力探测
```

`/panic` 现在会额外**实际停止**相关沙箱环境。桌面端「进阶」页有「沙箱」面板：刷新状态、
运行隔离自检、环境列表、停止全部环境、保留文件/清空文件重置。

安装验收与运维自检（源码与安装版一致）：

```text
python -m atri_x.sandbox.doctor           # 后端/隔离/资源/网络/清理能力实测
atri-x.exe -m atri_x.sandbox.doctor --json
python -m atri_x.sandbox status|list|probe|gc
```

## 8. 验收状态与残余风险

P0 批次已实现：统一沙箱服务、容器后端（Docker/Podman）、租户隔离、资源硬顶、公平队列、
联网三档 + 出口代理、shell / 文件 / 发送 / 看图接线、`/panic` 停环境、重置三档、
审计与用量账本、桌面端面板、安装版自检。

仍属后续批次（P1/P2）：

- **DSH 执行链**尚未按租户隔离：沙箱模式下 DSH 路由被**限制**（回落沙箱内原生工具），
  这不是「已完成密钥隔离」；
- Hook / Skill / 研究 / 渲染等执行链的信任分级已登记（见 `atri_x/sandbox/trust.py`），
  任务新生成的 Skill/插件进入候选区、不热加载到宿主仍待第二批；
- 快照 / 检查点 / 自动恢复是 P1；现有 `/rollback` 仍只服务 `edit_file` 的撤销；
- Windows 原生 AppContainer + Job Object 后端是 P2，不承诺与 Linux 工具链等价；
- 网络域名的细粒度治理、带宽精细计费、共享协作环境属于增强项。

已知边界：容器与宿主共享内核；只读挂载仍受权限控制；出口代理是网络层强制但代理本身是
控制面组件；「拿到名额后才启动环境」意味着高并发时会有排队等待。
