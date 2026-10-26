# ATRI-X 部署套件（Python 版）

本目录是上游 `ATRI-X/deploy/`（**Go 版**部署套件）的 Python 全量重写。

- **声明式文件**（`Dockerfile`、`docker-compose.yml`、systemd unit、nginx conf、
  `.env.example`）保留原格式，内容改成 Python 版；
- **上游所有 shell 脚本（`*.sh`）都重写成了 Python**（纯标准库），
  文件名 `.sh` → `.py`；
- 上游 `windows/atri-x.ps1`（822 行）重写为 `windows/atri_x.py`，
  只留极薄 `.cmd` / `.ps1` 引导壳；
- 上游 `deploy/tests/*.sh`（7 个）重写为 `tests/test_deploy_*.py`（标准库 unittest）。

> 本文件对应上游 `deploy/README.md`（697 行）。上游那份同时讲部署流程**和**
> Go 构建细节；Python 版没有构建/编译环节，因此这里聚焦部署与运维，
> 并把「与上游的差异」集中列在第 13 节。

---

## 1. 快速开始（Docker Compose）

```bash
cd deploy
cp .env.example .env          # 填 API Key / token
chmod 600 .env
docker compose build
docker compose up -d
```

Windows 本地 Docker Desktop 用户可以用引导壳：

```powershell
.\deploy\windows\atri-x.cmd init
# 编辑 deploy\.env 后
.\deploy\windows\atri-x.cmd start
.\deploy\windows\atri-x.cmd status
```

首次启动会在数据卷里生成默认配置：

| 文件 | 作用 |
|---|---|
| `/data/config/atri-x/app.toml` | 主配置（平台、CLI server、Atrinis、生图、security） |
| `/data/config/atri-x/services.toml` | LLM / 生图服务（旧部署叫 `providers.toml`） |
| `/data/config/atri-x/state.toml` | 可写状态（provider / model 选择），与只读配置分离 |
| `/data/config/atri-x/atrinis.toml` | Atrinis 事件入口（默认不启用） |

改 TOML 后 `docker compose restart`；改 `.env` 后**必须**
`docker compose up -d --force-recreate`（环境变量只在创建容器时注入）。

---

## 2. 镜像

`deploy/Dockerfile` 有**两个阶段**：运行阶段的基础镜像是官方 `python:3.14-slim`，
另一个 `node:24-bookworm-slim` 阶段只用来把 DSH（DeepSeek Harness）装进镜像：

- Python 版**没有编译步骤**：纯标准库、零第三方依赖，代码直接 `COPY` 进镜像；
- 只复制 `atri_x/`、`pyproject.toml`、`README.md`（新增的 `atri_x/agent/dsh/` 随包一起进镜像）；
- Python 运行时**不装任何第三方依赖**（Pillow 是可选的图像后端，不装；缺失时走纯 Python 后备）；
- 内置 DSH 需要 `node` 二进制，因此运行阶段固定装 `libstdc++6` 和 `ca-certificates`；
  `EXTRA_TOOLS` 仍然只增不减；
- 非 root 运行（`10001:10001`），配置与数据都在 `/data` 卷上（DSH 状态也在
  `/data/dsh`，见 2.2）；
- `ENTRYPOINT ["python", "-m", "atri_x"]`，`CMD ["service", "run"]`。

构建：

```bash
docker build -f deploy/Dockerfile --build-arg VERSION="$(cat deploy/VERSION)" -t "atri-x:$(cat deploy/VERSION)" .
```

多架构（需要 buildx）：

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -f deploy/Dockerfile --build-arg VERSION=0.9.0 \
  --push -t <registry>/<namespace>/atri-x:0.9.0 .
```

### 2.1 基础镜像说明（已核对）

`python:3.14-slim` 存在，官方指向 `python:3.14-slim-bookworm`；实测
`python:3.14.7-slim-bookworm == python:3.14-slim-bookworm`（约 122 MB，Debian 12）。
需要国内加速或 pin digest 时用 `PY_BASE_IMAGE` 覆盖：

```bash
--build-arg PY_BASE_IMAGE=registry.cn-hangzhou.aliyuncs.com/library/python:3.14-slim
--build-arg PY_BASE_IMAGE=python:3.14-slim@sha256:<digest>
```

万一该 tag 拉不动，`PY_BASE_IMAGE=python:3.13-slim` 即可（代码只依赖标准库，
`requires-python>=3.11`）。

slim 镜像里**没有 curl / git**，这是官方刻意的精简取舍。本套件因此：

- 健康探测全部走标准库（见 3.1），不依赖 curl；
- **tzdata 由 `deploy/Dockerfile` 显式安装**（1.0.10 起是硬依赖）：ATRI-X 的业务
  时区来自 `[time].timezone`（IANA 名），不再依赖"基础镜像碰巧带了
  `/usr/share/zoneinfo`"。`ENV TZ` 只决定进程本地时区（`timezone = "system"` 时才
  被用到）；镜像构建的最后一步会跑 `python -m atri_x.app.time_check --json`，
  时区数据不可用会让**构建失败**而不是出个坏镜像；
- 需要给 Agent 的 shell 工具补系统命令时，用 `EXTRA_TOOLS` 追加（默认一个都不装）。

=== 验证 Docker 时区数据（构建后执行，留证）===

```bash
# 1) 镜像构建期已经断言过；这里做产物级复核（非 root 用户下加载 IANA 时区）
docker run --rm --entrypoint python atri-x:<version> -m atri_x.app.time_check --json
# 2) 确认发行版包版本（写进部署记录）
docker run --rm --entrypoint dpkg-query atri-x:<version> -W -f='${Version}\n' tzdata
# 3) 显式区分"配置时区"与"进程 TZ"：TZ=UTC 时业务时区仍应是 Asia/Shanghai
docker run --rm -e TZ=UTC --entrypoint python atri-x:<version> -c \
  "from atri_x.config import default; from atri_x.time_service import TimeService as T; \
print(T.from_config(default().time).snapshot().timezone)"
```

### 2.2 镜像内置 DSH（ACP 编排，最小闭环）

镜像里带一份锁版本的 DeepSeek Harness（默认 `@deepseek-ai/dsh@0.2.0-rc.2`），
ATRI-X 通过 `dsh --profile acp` 的 **stdio 子进程**调用它：不开端口、不占宿主机
端口映射，也不需要 pnpm 或运行期联网装包（`dsh` 的 bundle 从镜像安装目录解析，
profile 目录首次启动自动生成）。ACP 客户端已就绪（`atri_x/agent/dsh/`），
**对话链路的按需路由尚未接入**，当前可用的是下面的闭环冒烟。

- 构建参数：`NODE_BASE_IMAGE`（默认 `node:24-bookworm-slim`）、`DSH_VERSION`、
  `DSH_NPM_REGISTRY`；国内构建把 `DSH_NPM_REGISTRY` 指向镜像站即可；
- DSH 的状态与工作区都落 `/data` 卷：`DSH_HOME=/data/dsh`、`DSH_WORKSPACE=/data/dsh-ws`，
  与 ATRI-X 的配置/SQLite/媒体一起备份、一起迁移；
- DSH 自己的模型凭据默认复用 `DEEPSEEK_API_KEY`（`dsh-credentials-local` 继承环境变量），
  需要独立额度时再单独配；
- 最小闭环冒烟（镜像内执行，跑通 initialize → session/new → prompt → close 并打印真实 token）：

```bash
# 镜像 ENTRYPOINT 是 `python -m atri_x`，所以要用 --entrypoint python 覆盖后再传 -m ...
docker compose run --rm --entrypoint python atri-x -m atri_x.agent.dsh.smoke --json
```

- 客户端代码：`atri_x/agent/dsh/`（纯标准库，导入无副作用，不参与启动装配）；
  协议契约、路由阈值与后续工作见 [`devdocs/dsh-acp.md`](../devdocs/dsh-acp.md)。

---

## 3. 端口与健康接口

| 端口 | 用途 |
|---|---|
| 32171 | 独立健康接口（`/live`、`/ready`、`/healthz`、`/tasks`、`/metrics`、`/diagnostics`） |
| 32172 | CLI 远程 WebSocket 服务端（`app.toml` 的 `[platform.cli.server].enabled=true` 时） |
| 32170 | Atrinis 事件 HTTP 入口（`atrinis.toml` 的 `enabled=true` 时；默认关闭） |

Compose 只把它们映射到宿主机 `127.0.0.1`，公网访问统一走宝塔 Nginx 反代
（见 `nginx-atri-x.conf`）。

### 3.1 健康探测三层（与 compose / Dockerfile 完全一致）

1. `python -m atri_x --health-probe`（应用内置开关，**首选**）；
2. 该开关不可用时退回 HTTP `GET /live`（只看进程是否还能响应）；
3. 再退回 TCP 连通 32171。

第 2、3 层保证镜像**不依赖** `--health-probe` 这个开关也能做健康检查。
独立探针脚本：

```bash
python deploy/health_probe.py            # /live
python deploy/health_probe.py --ready    # 调度与数据目录就绪
python deploy/health_probe.py --json     # 结构化输出（键名固定）
```

`/live` 语义边界：**只看进程是否还能响应**，不判断调度心跳、模型 API、
OneBot / Telegram 连通性。要判断“就绪”用 `/ready`。

---

## 4. 环境变量

全部集中在 `deploy/.env`（模板见 `.env.example`），键名逐条沿用上游，
只有 Go 专属键被替换：

| 上游键 | Python 版 | 说明 |
|---|---|---|
| `GO_VERSION` / `GO_BASE_IMAGE` / `GOPROXY` | 删除 | Python 版没有编译阶段 |
| `RUNTIME_BASE_IMAGE` | `PY_BASE_IMAGE` | 基础镜像覆盖（加速 / pin digest） |
| `ATRIX_BUILD_TARGET` | 删除 | 上游用来选 `runtime` / `go-runtime`；Python 版没有 Go 编译阶段（内置 DSH 用独立 node 阶段） |
| `ATRIX_GO_BINARY` / `EXTRA_TOOLS` | `EXTRA_TOOLS` 保留 | 上游默认装一大串 CLI 工具；Python 版默认不装 |
| — | `ATRIX_DATA_VOLUME` | 新增：named volume 名（默认 `atri_x_data`） |
| — | `ATRIX_CONFIG_FILE` | 新增：非标准配置路径时才需要 |

密钥类键名**完全不变**（`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`IMAGE_API_KEY`、
`ATRIX_CLI_LOCAL_TOKEN`、`ATRINIS_HOME_TOKEN`、`QQONEBOT_ACCESS_TOKEN`、
`TELEGRAM_BOT_TOKEN`、`ATRIX_S3_ACCESS_KEY_ID` …），因为它们对应
`services.toml` / `app.toml` 里的 `api_key_env`，改了会打断配置引用。

---

## 5. 数据、备份与恢复

数据全在 `/data`（Compose 默认挂 named volume `atri_x_data`；
需要直接 tar 宿主机目录时把 `volumes` 改成 `./data:/data`，文件里保留了注释行）。

**关键点：不要给 `/data` 的子路径再挂一个卷。** 子路径挂载会覆盖父挂载，
把 `/data/config` 从数据卷里遮掉，`docker compose down -v` 之后配置就“消失”了。
配置与数据同卷是刻意选择，与上游 `./data:/data` 的布局一致。

### 5.1 备份

```bash
python deploy/backup.py                 # 默认 BACKUP_MODE=auto
BACKUP_MODE=stop python deploy/backup.py
BACKUP_MODE=hot  python deploy/backup.py
```

三种模式：

| 模式 | 行为 |
|---|---|
| `sqlite` | **不中断服务**：对每个 SQLite 库做在线一致性快照，再复制其余文件，最后按数据库里的 `media.local_path` 补齐媒体 |
| `stop` | 短暂停止容器后打包，再自动启动并等 healthcheck 就绪 |
| `hot` | 热打包，明确警告 SQLite WAL 下可能不一致 |

`auto` 在 Python 版**始终**能走 `sqlite`（标准库 `sqlite3` 的在线备份 API 永远可用），
所以默认路径是不中断服务的。上游要靠宿主机有 `sqlite3` 命令才能走这条路径。

产物：`backups/atri-x-data-<YYYYmmdd-HHMMSS>.tar.gz` +
同名 `.manifest`（sha256sum 格式，从**归档内容本身**生成，因此清单与归档必然一致）。
默认保留最近 14 份（`KEEP` 覆盖）。

### 5.2 恢复验证（演练）

```bash
python deploy/restore_verify.py backups/atri-x-data-20260101-000000.tar.gz
```

在系统临时目录里隔离解包，逐项检查：清单 sha256、TOML 可解析且引用关系正确、
`app.toml` / `services.toml`（或 `providers.toml`）非空、SQLite
`PRAGMA integrity_check = ok` 且有表、数据库引用的本地媒体文件都在。
**不接触生产数据目录**，可安全地由 `backup.py` 自动调用。

收尾状态行（调用方按它判断，是契约）：

```
restore_verify: passed                    # 静态检查 + 真实隔离启动都通过
restore_verify: static_passed             # 静态通过，但启动验收被关闭/跳过
restore_verify: static_passed_with_skips  # 非严格模式下有跳过项
```

严格模式（`RESTORE_VERIFY_STRICT=1`，默认）**拒绝**跳过清单校验、TOML 解析、
SQLite 完整性检查；需要降级时显式设 `0`。

可选的真实验收：`RESTORE_VERIFY_START=required` 会用真实镜像 + 恢复出的数据
起一个 `--network none` 的一次性容器并等它的 `/ready`。

---

## 6. 升级与回滚

```bash
python deploy/upgrade.py            # 用 deploy/VERSION 作为新版本
ATRIX_VERSION=0.9.0 python deploy/upgrade.py
ATRIX_GIT_REF=v0.9.0 python deploy/upgrade.py   # 先切源码标签再升级
python deploy/rollback.py
```

升级顺序（每一步失败都会中止，不会留下“半升级”状态）：

1. 需要时 `git fetch --tags && git checkout --detach <ref>`（在**仓库根**执行）；
2. 校验目标版本 == `deploy/VERSION`（不一致必须显式
   `ATRIX_ALLOW_VERSION_MISMATCH=1`）；
3. 当前容器内 `python -m atri_x config check`；
4. `backup.py` 数据快照（默认 `stop` 模式），并**用当前实际镜像**验证可恢复；
5. 按**实际 image ID** 打 `atri-x:rollback` 标签并 `docker save`，写 `rollback/rollback.env`；
6. 构建新镜像；
7. 用新镜像在 data 的**隔离副本**上 `config check`（`UPGRADE_PRECHECK_ISOLATED=1`，默认）；
8. `up -d --force-recreate`，等 healthcheck `healthy`，再跑 `doctor`。

失败时脚本会打印回滚命令。`rollback.py` 会先用旧镜像验证数据快照，
**验证不通过就绝不停服务、绝不覆盖 data**；万一回滚过程中失败，会把
「回滚前的 data + 回滚前的镜像」装回去。

---

## 7. 外部 watchdog

```bash
cp watchdog.env.example watchdog.env   # 按需修改
python deploy/atri_x_watchdog.py       # 单次执行；推荐由 systemd timer 每分钟跑
```

判定链：

1. `/live` 失败计数达到 `WATCHDOG_FAILURE_THRESHOLD`（默认 3）；
2. 冷却时间已过（`WATCHDOG_COOLDOWN_SECONDS`，默认 300s）；
3. 窗口内重启次数未超上限（`WATCHDOG_MAX_RESTARTS` / `WATCHDOG_WINDOW_SECONDS`）。

达上限后写 `paused` 标记并停止自动重启，等人介入（手动恢复：
删除 `<state-dir>/paused`）。

`/live` 正常但 `/ready` 持续失败时**只告警、不重启**——这种“进程活着但初始化
或调度有问题”的情况直接重启容易演变成重启风暴。

诊断包收集 `/live` `/ready` `/healthz` `/tasks` `/metrics` + `docker ps/state/ports/logs/stats`
+ `df` + data 目录大小，保留最新 30 份。**只抓 `State` 与 `NetworkSettings.Ports`**，
不抓 `Config.Env`，避免把 API Key / token 写进诊断包。

诊断包会就地脱敏并复检；复检不过就写 `REDACTION-FAILED` 标记、`chmod 700`
并以非零退出，**绝不把可能带凭据的包当成成功产物**。手动对已有目录脱敏：

```bash
python deploy/atri_x_watchdog.py --redact-dir deploy/diagnostics/20260101-000000
```

---

## 8. 离线 / 预构建部署

```bash
python deploy/pack/prepare_offline.py              # 生成 deploy/dist/ 下的全部产物
python deploy/pack/prepare_offline.py --skip-images # 只出源码离线包
```

产物：

| 文件 | 说明 |
|---|---|
| `atri-x-<version>-src.tar.gz` | 源码离线包（`atri_x/` + 部署文件 + `Dockerfile.prebuilt`） |
| `atri-x-<version>-linux-<arch>.tar.gz` | 预构建镜像 tar，`docker load` 即可（需要 buildx） |
| `atri-x-<version>-offline-<arch>.tar.gz` | 完整离线包 |
| `SHA256SUMS` | 全部产物的校验和 |

目标机上：

```bash
tar -xzf atri-x-0.9.0-offline-amd64.tar.gz && cd offline-amd64
cp .env.example .env && chmod 600 .env && vim .env
python3 deploy.py                 # 或 python3 deploy.py --load（完全离线）
```

`pack/deploy_lighthouse.py` 会检测架构、检查 Docker、生成 `.env`、准备数据目录属主、
载入或构建镜像、启动容器并打印后续命令。

---

## 9. systemd（原生部署，不用容器）

```bash
useradd -r -s /usr/sbin/nologin atri-x
mkdir -p /opt/atri-x/data/config /opt/atri-x/data/run
cp -a <仓库>/. /opt/atri-x/
chown -R atri-x:atri-x /opt/atri-x
cp deploy/atri-x.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now atri-x
```

| 单元 | 用途 |
|---|---|
| `atri-x.service` | 原生运行 `python3 -m atri_x run`（**没有**编译产物，代码就在 `/opt/atri-x/atri_x/`） |
| `atri-x-compose.service` | 用 systemd 托管整套 Compose（与宝塔 Docker 管理器**二选一**） |
| `atri-x-watchdog.service` + `.timer` | 每分钟跑一次 `atri_x_watchdog.py` |

> 单元里的解释器路径写的是 `/usr/bin/python3`。若你的解释器在别处，
> 按 `command -v python3` 的结果改 `ExecStart`。

---

## 10. 反向代理（宝塔 Nginx）

把 `nginx-atri-x.conf` 的内容贴到站点 `server {}` 内，或按注释用宝塔的
「反向代理」界面加三条规则。**独立健康接口 32171 不要放进反代文件**——
它只供宿主机 watchdog / 宝塔监控 / `health_probe.py` 使用。

`/atri-x/healthz` 那一段只代理 Atrinis 的 `/healthz`。警告：ATRI-X 默认关闭
Atrinis，未启用时该请求会失败，但进程、模型 API、OneBot / Telegram、CLI
仍可能完全正常；**不要用 `/healthz` 判断服务异常**。

---

## 11. 安全须知

- **`.env` 会被容器内所有进程继承**，包括 Agent 派生的 shell / hook 子进程。
  面向群聊用户开放工具时，不要只依赖「容器不是 root」来保护 API Key。
- 建议用**与 LLM 分开的**生图 Key，并在中转站设额度/限速。
- 只把端口映射到 `127.0.0.1`；公网入口走 Nginx + TLS。
- 诊断包默认脱敏 + 复检，但**外发前仍应人工确认**（尤其带 `REDACTION-FAILED` 的）。
- Portainer（`portainer/portainer-compose.yml`）挂载了 `docker.sock`，
  等同 Docker 管理员权限，只监听回环，首次启动务必设强密码。

---

## 12. 部署验收清单

1. `docker compose ps` 显示容器 `healthy`；
2. `python deploy/health_probe.py` 返回 0；`--ready` 返回 0；
3. `docker compose exec atri-x python -m atri_x doctor` 全绿
   （做端到端验收时加 `--e2e`）；
4. 在真实平台**发一条消息**，确认收到回复；
5. 重启容器（`docker compose restart`），确认会话与配置仍在；
6. `python deploy/backup.py` 成功，且最后一行是 `restore_verify: static_passed`
   或 `restore_verify: passed`；
7. `python deploy/restore_verify.py backups/<刚生成的包>` 通过；
8. 改一次 `.env` 并 `up -d --force-recreate`，确认新变量生效。

---

## 13. 与上游 Go 版部署套件的差异（有意偏差）

逐条列出，便于复核：

| # | 上游（Go / bash） | Python 版 | 原因 |
|---|---|---|---|
| 1 | Dockerfile 两阶段（golang 构建 + debian 运行），产物是静态二进制 | 运行阶段 `python:3.14-slim` 只 COPY 源码；另有 node 阶段装内置 DSH | Python 没有编译步骤 |
| 2 | 运行镜像装一大串 CLI 工具（curl/wget/git/jq/…），另装 tini | 只装 node 需要的 libstdc++6 / ca-certificates；用 compose `init: true` 代替 tini | 零第三方依赖是硬约束；内置 DSH 需要 node |
| 3 | `HEALTHCHECK` 用 `curl` 打 `/live` | 三层标准库探测：`--health-probe` → `/live` → TCP 32171 | slim 镜像没有 curl |
| 4 | `ENTRYPOINT tini -- atri-x`，`CMD ["service","run"]` | `ENTRYPOINT ["python","-m","atri_x"]`，`CMD ["service","run"]` | 对应 `python -m atri_x service run` |
| 5 | 有 `go-runtime` 构建目标（容器内编译 Go Skill） | 删除该目标 | Python 版没有需要编译的 Skill |
| 6 | 数据用 `./data` bind mount | named volume `atri_x_data`（bind mount 以注释保留） | 要求 named volume；注释保留便于直接备份目录 |
| 7 | `.env.example` 有 `GO_VERSION` / `GOPROXY` / `ATRIX_BUILD_TARGET` 等 | 换成 `PY_BASE_IMAGE`，并新增 `ATRIX_DATA_VOLUME` / `ATRIX_CONFIG_FILE` | 见第 4 节映射表 |
| 8 | 备份依赖宿主 `sqlite3` 命令 + `sha256sum`；没有就走停机/热打包 | 标准库 `sqlite3` 在线备份 + `hashlib`；`auto` 总能走一致性路径 | 标准库永远可用，判据更硬 |
| 9 | 恢复验证缺少 `sha256sum` / `sqlite3` / 带 tomllib 的 python 时，严格模式直接失败 | 这三个能力都是标准库，**不存在**“工具缺失”这类跳过项 | 同上 |
| 10 | 升级时容器内命令是 `atri-x config check` / `atri-x doctor` | `python -m atri_x config check` / `doctor` | 镜像 ENTRYPOINT 已含 `python -m atri_x`；绝不能多传一次应用名 |
| 11 | upgrader 用 `ls -1t backups/*.tar.gz \| head -1` 找最新备份 | 比对调用前后集合，只认**本次**产生的那一份 | 避免并发备份认错快照 |
| 12 | watchdog 用 6 条 `sed -E` 就地脱敏 | 复用 `atri_x.redact.secrets`（PORTING.md §3 的唯一出处）+ 引号修复 + 收敛到不动点 | 与“应用自己写日志时的脱敏”规则一致；并修复项目规则贪吃收尾引号导致的**非幂等**（重复脱敏必须幂等） |
| 13 | watchdog 检测到“已知凭据值”只**报警**并返回非零 | 除了报警，还**真的把它从文件里清掉** | “报警了但没清”仍会让诊断包带着凭据外发 |
| 14 | watchdog 无互斥，timer 每分钟触发可能并发 | `state_dir` 里 O_EXCL 单实例锁（超时可抢占） | 并发会互相覆盖状态并重复重启 |
| 15 | watchdog 用 `find \| sort \| tail \| xargs rm -rf` 清理旧诊断 | 标准库遍历 + `shutil.rmtree` | 不依赖 awk/sort/xargs |
| 16 | `windows/atri-x.ps1` 822 行，其中 4 个动作要再找 Git Bash 调 `*.sh` | `windows/atri_x.py`（纯标准库），4 个动作直接调 `deploy/*.py`；`.cmd` / `.ps1` 只留引导壳 | 不再需要 Git for Windows；路径转换逻辑整段删除 |
| 17 | `Assert-BackupVerificationDependencies` 要求宿主 `sqlite3` + 带 tomllib 的 python | 整条删除 | 两者都是标准库，断言没有意义 |
| 18 | Windows 计划任务动作是 `powershell.exe -File atri-x.ps1 start -NoBuild` | `python atri_x.py start --no-build`（`schtasks`） | 不再经过 PowerShell |
| 19 | 离线包是“静态二进制 + scratch 镜像”，`build-image-tar.py` 手工拼镜像层 | 离线包是源码 + `Dockerfile.prebuilt`（`python:*-slim`）；镜像 tar 由 buildx/`docker save` 导出 | 没有二进制可拼；导出的是 Dockerfile 真正构建出来的东西，不会漂移 |
| 20 | `prepare-offline.sh` 需要 Go 工具链交叉编译 | 只在需要镜像 tar 时需要 buildx；`--skip-images` 可纯源码打包 | 没有编译步骤 |
| 21 | `init-host.sh` 用 `${COMPOSE} build` 由 shell 展开 | 走 `compose_argv()`，始终显式 `--env-file` / `-f` | 从任意目录调用都不丢 `.env` |
| 22 | 归档解包直接用 `tar -xzf` | `_libfs.safe_extract_tar` 先校验每个成员（绝对路径 / `..` / 越界链接） | 备份包来自生产环境，不能无条件信任 |

**新增**（上游没有对应物）：

| 文件 | 用途 |
|---|---|
| `deploy/health_probe.py` | 独立健康探针（curl 的标准库等价物），给 HEALTHCHECK / 编排 / 运维用 |
| `deploy/_lib.py` + `_console/_envutil/_proc/_netutil/_libfs.py` | 上游 7 个 shell 脚本各自复制一份的公共函数（log/warn/die、Compose 探测、路径转换、HTTP 探针、sha256、tar 解包、脱敏）抽成共享库 |
| `deploy/.dockerignore` | 构建上下文裁剪（与 `Dockerfile.dockerignore` 内容相同、必须同步） |

---

## 14. 本轮改动未包含 / 及待对接事项

### 14.1 对接确认（本轮已实测）

| # | 事项 | 结论 |
|---|---|---|
| 1 | **`python -m atri_x --health-probe`** | `atri_x/__main__.py` 已存在，开关已实现并被 `tests/test_main.py::TestHealthProbeCommand` 覆盖；容器内 `--health-probe` 退出码 0（冒烟步骤 `health_probe_exit_zero`） |
| 2 | **`python -m atri_x run` / `service run`** | 已确认：Dockerfile 的 `CMD ["service","run"]` 与 `atri-x.service` 用的都是真实子命令；容器冒烟的 `healthy` / `container_running` 步骤证明长驻形态正确 |
| 3 | **`config check` / `doctor --no-model` / `completion <shell>`** | 已确认：CLI 侧实测过 `--help`、`config check`、`doctor --no-model`、`completion bash`（另见 `tests/test_main.py` 的 `TestConfigCheck` / `TestDoctor` / `TestCompletionCommand`） |
| 4 | **镜像内应用监听 `32171` 并与 `ATRIX_HEALTH_ADDR` 对齐** | 已确认：compose / Dockerfile / 探针 / watchdog 四处一致，冒烟 `healthy` 步骤（第 2 层 HTTP 探测）通过 |
| 5 | **配置文件落位** | 已确认：部署侧把配置 seed 到 `/data/config/atri-x/`（冒烟 `seed_config`），应用数据写在 `/data/data/`（冒烟 `data_volume_layout` 实测到 `sessions.db` / `chat_history.db` / `logs`） |
| 6 | **`atri_x/launcher` 的 CLI 是否接受 `--config`** | 仍然不用 `--config`；只认 `ATRIX_CONFIG_FILE` 环境变量（`atri_x/config/loader.py`） |
| 7 | **Docker 冒烟与稳定性测试** | 已在 Docker Desktop 上执行：`deploy/smoke.py` **13/13**；`deploy/stability.py --duration 300 --interval 5` 见 §12（冒烟/稳定性报告默认写到 `deploy/reports/`，属于运行产物、不入库；宿主同时跑全量测试套件时出现过一次资源争用导致的失败，安静环境复跑通过） |

### 14.2 需要确认的取舍

| # | 点 | 当前选择 |
|---|---|---|
| 1 | 配置与数据**同一个卷** | 是。分成两个卷会让 `/data/config` 被 `/data` 覆盖（第 5 节）；备份脚本也按单一 `/data` 布局工作 |
| 2 | 运行镜像是否装 CLI 工具 | 默认**不装**。上游镜像里那一大串工具是给 Agent 的 shell 工具用的，需要时用 `EXTRA_TOOLS` 追加 |
| 3 | 不做 `go-runtime` 等价物 | Python 版没有需要编译的 Skill；若将来需要“容器内可写 Python 依赖”，应新开机制而不是复活该目标 |
| 4 | `-h` / `--help` 不经过 `atri_x` 的参数解析器 | `atri_x.py` 用 `add_help=False` 自己处理，保证动作集合与上游 `ValidateSet` 一致 |

---

## 15. 本目录文件清单

| 文件 | 对应上游 |
|---|---|
| `Dockerfile` | `Dockerfile` |
| `.dockerignore` / `Dockerfile.dockerignore` | `Dockerfile.dockerignore`（拆成两份，内容相同） |
| `docker-compose.yml` | `docker-compose.yml` |
| `.env.example` | `.env.example` |
| `VERSION` | `VERSION` |
| `atri-x.service` | `atri-x.service` |
| `atri-x-compose.service` | `atri-x-compose.service` |
| `atri-x-watchdog.service` / `.timer` | 同名 |
| `watchdog.env.example` | 同名 |
| `nginx-atri-x.conf` | 同名 |
| `backup.py` | `backup.sh` |
| `restore_verify.py` | `restore-verify.sh` |
| `rollback.py` | `rollback.sh` |
| `upgrade.py` | `upgrade.sh` |
| `build_push.py` | `build-push.sh` |
| `init_host.py` | `init-host.sh` |
| `atri_x_watchdog.py` | `atri-x-watchdog.sh` |
| `health_probe.py` | （新增，替代 HEALTHCHECK 里的 curl） |
| `_lib.py` / `_console.py` / `_envutil.py` / `_proc.py` / `_netutil.py` / `_libfs.py` | （新增，7 份 shell 公共函数的共享实现） |
| `pack/prepare_offline.py` | `pack/prepare-offline.sh` |
| `pack/deploy_lighthouse.py` | `pack/deploy-lighthouse.sh` |
| `pack/build_image_tar.py` | `pack/build-image-tar.py`（改写：改为导出真实构建产物） |
| `pack/verify_image_tar.py` | `pack/verify-image-tar.py`（改写：适配 Python 镜像） |
| `pack/Dockerfile.prebuilt` / `pack/docker-compose.prebuilt.yml` | 同名 |
| `pack/README-*.md` | `pack/README-*.md` |
| `portainer/portainer-compose.yml` | 同名 |
| `windows/atri_x.py` | `windows/atri-x.ps1` |
| `windows/atri-x.cmd` / `atri-x.ps1` | 同名（改成极薄引导壳） |
| `windows/README.md` | `windows/README.md` |
| `tests/test_deploy_*.py` | `tests/*.sh` |

> 冒烟 / 稳定性测试（`smoke.py`、`stability.py`、`fixtures/smoke-config/`）与第 12 节验收清单的逐条对应关系见 [`TESTING.md`](TESTING.md)。
