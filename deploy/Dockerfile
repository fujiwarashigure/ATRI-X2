# syntax=docker/dockerfile:1.7
#
# ATRI-X Python 版容器镜像
#
# 上游 Go 版用「golang 构建阶段 + debian 运行阶段」两段式编译静态二进制；
# Python 版**没有编译步骤**：纯标准库、零第三方依赖，代码直接复制进镜像即可，
# 因此这里只有一个阶段，基础镜像就是官方 python:3.14-slim。
#
# 本机构建（版本号取自 deploy/VERSION，也可显式传入）：
#   docker build -f deploy/Dockerfile \
#     --build-arg VERSION="$(cat deploy/VERSION)" -t "atri-x:$(cat deploy/VERSION)" .
#
# 多架构（需 buildx；一次产出 amd64 + arm64 的 manifest list）：
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -f deploy/Dockerfile --build-arg VERSION=0.9.0 \
#     --push -t <registry>/<namespace>/atri-x:0.9.0 .
#
# 基础镜像可以覆盖（国内加速 / pin digest）：
#   --build-arg PY_BASE_IMAGE=registry.cn-hangzhou.aliyuncs.com/library/python:3.14-slim
#   --build-arg PY_BASE_IMAGE=python:3.14-slim@sha256:<digest>
#
# ⚠️ 基础镜像可用性（已核对）：
#   python:3.14-slim 存在，官方指向 python:3.14-slim-bookworm 的别名，
#   实测 python:3.14.7-slim-bookworm == python:3.14-slim-bookworm（约 122 MB，Debian 12）。
#   若某天该 tag 被撤下而拉取失败，把下面默认值改成 python:3.13-slim（同样存在）即可；
#   本仓库代码只依赖标准库且 requires-python>=3.11，3.11~3.14 均可运行。
#
# ⚠️ slim 基础镜像里**没有** curl / git / tzdata，这是官方刻意的精简取舍：
#   - 健康探测改用标准库（python -m atri_x --health-probe + /live 兜底），不依赖 curl；
#   - 时区靠 ENV TZ + 标准库 zoneinfo（Debian 自带 /usr/share/zoneinfo）；
#   - Agent 的 shell 工具想用更多系统命令时，用 --build-arg "EXTRA_TOOLS=xxx"
#     追加（跟上游一样“只增不减”：默认列表是空的，不装任何多余东西）。

ARG PY_BASE_IMAGE=python:3.14-slim

FROM ${PY_BASE_IMAGE}

ARG VERSION=dev
# 只用于“追加”更多系统命令；为空时一个 apt 包都不装（Python 版零依赖）。
ARG EXTRA_TOOLS=

# PYTHONUNBUFFERED=1  让 stdout/stderr 直接进 docker logs，不做块缓冲；
# PYTHONDONTWRITEBYTECODE=1  只读根文件系统 / 卷上不写 __pycache__；
# PYTHONPATH=/app  同时服务两件事：`python -m atri_x` 能找到包，
#                  HEALTHCHECK 里 `python -c "import atri_x.health"` 也能找到。
# XDG_* 与上游一致：配置 / 数据 / 缓存 / 运行时标记全部落在 /data 卷上，
# 重建容器后配置、SQLite、日志、临时媒体都还在。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PYTHONFAULTHANDLER=1 \
    TZ=Asia/Shanghai \
    XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    XDG_RUNTIME_DIR=/data/run \
    HOME=/home/atri-x \
    LANG=C.UTF-8 \
    ATRIX_HEALTH_ADDR=0.0.0.0:32171

WORKDIR /app

# 只复制运行必需的三样：包目录、项目元数据、README。
# deploy/、tests/、docs/、scripts/、.git/ 全部由 deploy/.dockerignore 排除。
COPY atri_x/ /app/atri_x/
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md

# 非 root 运行：10001:10001 与上游 Dockerfile / compose 的 user 完全一致，
# 换镜像不会因为 UID 漂移导致已有的 ./data 卷不可写。
# 没有安装步骤、不建 venv：代码就在 /app，靠 WORKDIR + PYTHONPATH 直接 import。
RUN set -eux; \
    if [ -n "${EXTRA_TOOLS}" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends ${EXTRA_TOOLS}; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    groupadd -g 10001 atri-x; \
    useradd -u 10001 -g 10001 -d /home/atri-x -m -s /usr/sbin/nologin atri-x; \
    mkdir -p /data/config /data/cache /data/run /home/atri-x /app; \
    chown -R atri-x:atri-x /data /home/atri-x /app; \
    chmod 750 /data/run; \
    python -c "import atri_x, sys; print('atri_x', atri_x.__version__, 'on', sys.version.split()[0])"

LABEL org.opencontainers.image.title="atri-x" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/fujiwarashigure/ATRI-X" \
      org.opencontainers.image.licenses="Apache-2.0"

USER atri-x:atri-x
WORKDIR /app

VOLUME ["/data"]

# 32170 = Atrinis HTTP 入口；32171 = 独立健康接口；32172 = CLI 远程 WebSocket 服务端
# docker-compose 中只映射到宿主机 127.0.0.1，公网访问请走宝塔 Nginx 反向代理。
EXPOSE 32170 32171 32172

# 健康探测三层，全部走标准库（slim 镜像没有 curl）：
#   1. 首选 `python -m atri_x --health-probe`；
#   2. 该入口不可用时退回 /live（只看进程是否还能响应，不判断心跳/平台/模型）；
#   3. 再退回 TCP 连通 32171（进程已在监听）。
# HEALTHCHECK CMD 用 shell 形式是因为 `--health-probe` 失败必须静默重试而不是
# 让容器在启动期直接判死；`exec` 替换掉 sh 进程本身，退出码语义不被包装。
#
# ⚠️ 待对接：`python -m atri_x --health-probe` 由 atri_x/launcher 的同僚实现，
#    本文件按「有此开关」书写并把它作为首选；入口不存在时第 2/3 层会自动兜底，
#    因此镜像功能不依赖该开关。契约见 deploy/README.md「待对接」一节。
# 三层探测与 /live 语义一致：不判断调度心跳、模型 API、OneBot / Telegram 连通性；
# 调度 / 数据目录就绪请看 /ready，部署验收仍需按 deploy/README.md 第 12 节实发消息。
HEALTHCHECK --interval=30s --timeout=8s --start-period=45s --retries=3 \
    CMD python -m atri_x --health-probe >/dev/null 2>&1 \
        || python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:32171/live',timeout=3).status==200 else 1)" >/dev/null 2>&1 \
        || python -c "import socket,sys;s=socket.socket();s.settimeout(3);sys.exit(0 if s.connect_ex(('127.0.0.1',32171))==0 else 1)" >/dev/null 2>&1

# 不用 tini：Python 入口就是 `python -m atri_x`，它作为 PID 1 自行处理 SIGTERM；
# 僵尸进程回收交给 compose 的 init: true（上游是在镜像里装 tini，Python 版不必
# 为此多装一个包）。
# 上游 ENTRYPOINT 是 `tini -- /usr/local/bin/atri-x` + CMD `service run`；这里**逐字对齐**为
# `["service", "run"]`。
#
# ⚠️ 不要改成 `["run"]`：`run` 是「完整前台」模式，按 Go `runPlatforms` 的语义，
#    所有平台运行时退出后进程就跟着退出（`mode != RunModeService` → `return nil`）；
#    冒烟用的最小配置里一个平台都没启用，容器会在启动完成 0.5 秒后**以退出码 0 正常退出**，
#    Docker / compose 下表现为"容器起来又没了"、health 端点消失、watchdog 反复重启。
#    容器里要的是长驻服务语义，这与上游镜像的 CMD 一致。
# 注意：镜像 ENTRYPOINT 已含 `python -m atri_x`，容器内命令**只能**传子命令，
# 例如 `service run`、`config check`、`doctor --no-model`，不要再传一次 `atri-x`（上游同样的坑）。
ENTRYPOINT ["python", "-m", "atri_x"]
CMD ["service", "run"]
