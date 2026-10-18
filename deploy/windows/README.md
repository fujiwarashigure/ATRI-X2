# ATRI-X Windows 本地容器部署

本目录是上游 `deploy/windows/` 的 Python 重写。

| 文件 | 角色 |
|---|---|
| `atri_x.py` | **全部实现**（纯标准库）。上游 `atri-x.ps1`（822 行）的逻辑都在这里 |
| `atri-x.cmd` | 极薄引导壳：找到 `python` 并转发参数（给 cmd / 双击 / 计划任务用） |
| `atri-x.ps1` | 极薄引导壳：转发 `$args` 并传递退出码（给 PowerShell 用户用） |

> **不要**往两个引导壳里加业务逻辑——加进去就会重新分裂成两份实现。

> 另见：[`docs/windows-app.md`](../../docs/windows-app.md) —— **不装 Docker、不装 Python** 的原生 exe 形态
> （`packaging/build.py` 构建）。两种形态共用同一份 `app.toml` / `services.toml` / `state.toml` / `.env`，
> 换形态只需搬配置目录。

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| Docker Desktop | 用 **Linux containers**（不要切到 Windows containers），WSL2 后端推荐 |
| Python | 3.11+ 且在 `PATH` 上（`python --version` 能跑） |
| Git for Windows | **不再需要**（上游那 4 个运维动作要 Git Bash，Python 版直接调 `deploy/*.py`） |

首次使用：

```powershell
cd <仓库根>
.\deploy\windows\atri-x.cmd init      # 建 deploy\data 和 deploy\.env
notepad deploy\.env                   # 至少填 LLM API Key、ATRIX_CLI_LOCAL_TOKEN、ATRINIS_HOME_TOKEN（如启用）
.\deploy\windows\atri-x.cmd start     # 构建并后台启动
.\deploy\windows\atri-x.cmd status
```

也可以直接在 PowerShell 里跑实现本体：

```powershell
python .\deploy\windows\atri_x.py status
```

---

## 2. 动作一览

动作集合与上游 `atri-x.ps1` 的 `ValidateSet` **逐个一致**。

### 部署生命周期

| 动作 | 说明 |
|---|---|
| `init` | 建 `deploy\data\{config,run,logs,cache}`、`deploy\backups`，并从 `.env.example` 生成 `.env`（**不覆盖**已有 `.env`） |
| `build` | 构建当前源码版本的镜像 |
| `start` / `up` | 后台启动（默认带 `--build`；加 `--no-build` 跳过构建） |
| `recreate` | `up -d --force-recreate --build`：重新注入 `.env` 并重建容器 |
| `stop` | 停止容器（保留容器和数据） |
| `down` | 停止并移除容器（保留 `deploy\data`） |
| `restart` | 重启容器，**不**重新注入 `.env` |
| `status` / `ps` | 容器状态 + `/live` `/ready` `/healthz`；`--json` 输出 JSON |
| `version` | 源码版本（`deploy\VERSION`）+ 容器内版本 |

### 运行状态 / 诊断

| 动作 | 说明 |
|---|---|
| `health` | 打印 `/live` `/ready` `/healthz`；`--json` 输出 JSON |
| `tasks` | `/tasks`：活跃任务、阶段、排队积压 |
| `metrics` | `/metrics`：任务/资源/模型/限速/生图队列 |
| `diagnostics` | `/diagnostics`：聚合诊断 |
| `doctor [选项]` | 容器内跑 `python -m atri_x doctor`；可追加 `--e2e`、`--no-model`、`--require-platform`、`--json` |
| `logs` | 容器日志；支持 `--follow` / `--tail N` |

### 运维 / 数据安全

| 动作 | 说明 |
|---|---|
| `backup [目录]` | 调 `deploy\backup.py` 做一致性备份 |
| `restore-verify <包>` | 调 `deploy\restore_verify.py` 做隔离恢复验证 |
| `upgrade` | 调 `deploy\upgrade.py` |
| `rollback` | 调 `deploy\rollback.py` |

### 后台与高级用法

| 动作 | 说明 |
|---|---|
| `install-service` | 注册 Windows 计划任务：登录后自动 `start --no-build` |
| `uninstall-service` | 移除计划任务 |
| `shell [shell]` | 进入容器 shell（默认 `bash`） |
| `exec <命令...>` | 容器内执行命令（`docker compose exec -T`） |
| `compose <参数...>` | 参数原样透传给 `docker compose` |
| `help` | 帮助 |

### 开关

| 上游（PowerShell） | Python 版 |
|---|---|
| `-Follow` | `--follow` / `-f` |
| `-Tail 200` | `--tail 200` / `-t 200` |
| `-NoBuild` | `--no-build`（也接受 `--no_build`） |
| `-Json` | `--json` |

---

## 3. 环境变量

| 变量 | 作用 |
|---|---|
| `ATRIX_HEALTH_URL` | 健康接口基址，默认 `http://127.0.0.1:32171` |
| `ATRIX_OPS_TOKEN` | 访问 `/tasks` `/metrics` `/diagnostics` `/healthz` 的 token；未设进程变量时会从 `deploy\.env` 读同一个键 |
| `ATRIX_VERSION` / `ATRIX_IMAGE` | 覆盖 compose 用的版本/镜像标签（未设则按 `deploy\VERSION` 自动填 `atri-x:<version>`） |

`/tasks` `/metrics` `/diagnostics` 在服务端设置了 `ATRIX_OPS_TOKEN` 后会自动带
`Authorization: Bearer <token>`。HTTP **401** 会被明确报成认证失败，
不会被当成“接口不可达”。

---

## 4. 容器内命令与路径

- 容器里跑应用用 `python -m atri_x ...`（镜像 `ENTRYPOINT` 已经是这个），
  **不要**再传一次 `atri-x`，否则会变成 `python -m atri_x atri-x doctor`。
- 容器内数据一律在 `/data`：`/data/config/atri-x/*.toml`、`/data/atri-x/`（SQLite、
  日志、媒体）、`/data/cache`、`/data/run`。
- 宿主机数据目录是 `deploy\data`，compose 用 `./data:/data` 绑定（Windows 上
  文件权限由 Docker Desktop 的挂载层决定，属主 10001 一般无需手工处理）。

首次启动后请核对：

1. `services.toml` 的 `api_key_env` 与 `.env` 的变量名逐个对应；
2. `state.toml` 的 provider/model 与 `services.toml` 一致，且模型名真实可用；
3. `app.toml` 的 CLI server / OneBot / Atrinis / 生图 / security 配置符合实际接入方式；
4. `deploy\data` 不需要管理员权限即可写入。

---

## 5. 计划任务（登录后自动启动）

```powershell
.\deploy\windows\atri-x.cmd install-service
schtasks /query /tn ATRI-X-Docker /v /fo LIST
.\deploy\windows\atri-x.cmd uninstall-service
```

注册的动作是：

```
python <仓库>\deploy\windows\atri_x.py start --no-build
```

上游注册的是 `powershell.exe -File atri-x.ps1 start -NoBuild`；Python 版不再经过
PowerShell，因此 `-ExecutionPolicy` 之类的问题也不会出现。

**注意**：计划任务在登录会话里启动 Docker Desktop 之前可能失败——Docker Desktop
需要先起来。本脚本 `start` 会等守护进程（默认 180s），必要时把计划任务触发器
改成“延迟 1 分钟”。

---

## 6. 常见问题

**Q：`-NoBuild` 一直报未知参数？**
A：上游是 PowerShell 的 `-NoBuild`；Python 版用 `--no-build`。

**Q：`status` 里 `/live` 是 `unreachable`。**
A：依次检查：容器在跑（`docker compose ps`）、`32171` 映射到宿主机回环、
`.env` 里的 `ATRIX_HEALTH_ADDR` 是 `0.0.0.0:32171`（不是 `127.0.0.1:32171`，
容器内绑回环会导致宿主机探不到）。

**Q：`tasks` 报 `/tasks 不可达`。**
A：该接口在未设置 `ATRIX_OPS_TOKEN` 时**默认不注册**（只保留 `/live` `/ready`）。
设置 token 后宿主机侧也要能读到同一个值（写进 `deploy\.env` 的
`ATRIX_OPS_TOKEN`，或设进程环境变量）。

**Q：`backup` / `upgrade` 报需要 Docker。**
A：它们会先确认容器在跑——先 `start`。这两个动作**不再**需要 `sqlite3` 命令或
Git Bash（上游需要）。

**Q：改完 `.env` 不生效。**
A：环境变量只在创建容器时注入，必须 `recreate`（`restart` 不够）。

---

## 7. 与上游 `atri-x.ps1` 的差异

| # | 上游 | Python 版 | 原因 |
|---|---|---|---|
| 1 | `backup` / `restore-verify` / `upgrade` / `rollback` 要 `Resolve-Bash` 找 Git for Windows，再做 `Convert-ToBashPath` / `Quote-Bash` 路径转换 | 直接 `sys.executable deploy\*.py`，路径原样传 | 那 4 个脚本已是 Python；整段 Git Bash 依赖删除 |
| 2 | `Assert-BackupVerificationDependencies` 要求宿主 `sqlite3` 命令 + 带 tomllib 的 python | 整条删除 | 两者都是标准库 |
| 3 | 容器内 `atri-x doctor` | `python -m atri_x doctor` | 镜像 ENTRYPOINT 已含 `python -m atri_x` |
| 4 | 计划任务动作是 `powershell.exe -File atri-x.ps1 start -NoBuild` | `python atri_x.py start --no-build` | 不再经过 PowerShell |
| 5 | `Register-ScheduledTask` / `Get-ScheduledTask` | `schtasks /create` / `/query` / `/delete` | 纯标准库，不依赖 `ScheduledTasks` 模块 |
| 6 | `Invoke-RestMethod` + 手工处理 503 响应体 | `urllib`（`_lib.http_get`），503 的 JSON 一样解析出来 | 标准库；保留“503 不等于不可达”的语义 |
| 7 | 822 行单文件 | `atri_x.py` 单文件 + 两个极薄引导壳 | 引导壳保持可双击、可被计划任务调用 |
| 8 | `Show-Status` 里的状态字段 | 字段集合保持一致，`--json` 的键名固定（`version` / `compose_file` / `env_file` / `data_dir` / `docker` / `compose` / `container` / `task_registered` / `live` / `ready` / `healthz` / `ops_token_configured`） | 便于脚本消费 |
