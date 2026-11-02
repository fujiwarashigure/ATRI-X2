# ATRI-X 沙箱基础镜像（第一版：Python + 常用开发/构建工具）。
#
# 构建由控制面完成（`atri_x.sandbox.backend_container` 在镜像缺失时用内置
# Dockerfile 自动 build）。镜像里只放「跑用户任务需要的通用工具」，不放入任何
# ATRI-X 运行时、.env、SQLite、QQ 登录态或模型凭据——宿主秘密从未进入盒子。
#
# 安全基线：
# * 非 root 运行（uid 1000，容器启动时还会再显式 `--user 1000:1000`）；
# * 根文件系统只读（启动参数 `--read-only`），可写面只有工作区/私有 home//tmp；
# * 不装 docker CLI / 不挂 docker.sock / 不装包管理守护进程。
#
# 构建参数：
# * ``ATRIX_APT_MIRROR``：可选 Debian 镜像源（网络到官方源不快时用，例如
#   ``https://mirrors.tuna.tsinghua.edu.cn/debian``）；空 = 官方源（https）。
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="ATRI-X sandbox"
LABEL org.opencontainers.image.description="Per-tenant sandbox image for ATRI-X (python+git+curl toolchain)"
LABEL atri-x.sandbox="1.2.0"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/home/atri \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG ATRIX_APT_MIRROR=""
ENV ATRIX_APT_MIRROR=${ATRIX_APT_MIRROR}

COPY apt-setup.sh /opt/atri/apt-setup.sh
RUN sh /opt/atri/apt-setup.sh && rm -f /opt/atri/apt-setup.sh

RUN useradd --create-home --uid 1000 --shell /bin/bash atri \
 && mkdir -p /workspace \
 && chown atri:atri /workspace /home/atri

# 出口代理代码（由控制面在 build context 里提供同名文件）。
COPY egress.py /opt/atri/egress.py

WORKDIR /workspace
USER atri

# 租户容器以 `docker exec` 接收命令；这个默认命令只用来占住容器。
CMD ["sleep", "infinity"]
