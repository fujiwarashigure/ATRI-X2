#!/bin/sh
# 镜像构建期的 apt 源设置与依赖安装（由 Dockerfile 调用）。
#
# 为什么单独成脚本：不同网络到 deb.debian.org 的连通性差异很大（有的网络只放
# 443，有的到官方源会长时间卡住）。控制面可以用 ``[sandbox].apt_mirror`` 指定一个
# 可达镜像（如 https://mirrors.tuna.tsinghua.edu.cn/debian），构建时通过
# ``--build-arg ATRIX_APT_MIRROR=...`` 传进来；不指定时保持官方 https 源。
set -eu

MIRROR="${ATRIX_APT_MIRROR:-}"

sources() {
    find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -print
}

if [ -n "$MIRROR" ]; then
    echo "[apt-setup] using mirror: $MIRROR"
    # shellcheck disable=SC2046
    sed -i \
        -e "s|http://deb.debian.org/debian|$MIRROR|g" \
        -e "s|https://deb.debian.org/debian|$MIRROR|g" \
        -e "s|http://security.debian.org|$MIRROR-security|g" \
        -e "s|https://security.debian.org|$MIRROR-security|g" \
        $(sources)
else
    echo "[apt-setup] using official deb.debian.org over https"
    # shellcheck disable=SC2046
    sed -i \
        -e 's|http://deb.debian.org|https://deb.debian.org|g' \
        -e 's|http://security.debian.org|https://security.debian.org|g' \
        $(sources)
fi

# 部分网络对 IPv6 到 mirrors 的路由不稳定；强制 IPv4 + 有限重试。
printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "5";\n' \
    > /etc/apt/apt.conf.d/99atrix

apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    less \
    pkg-config \
    procps \
    ripgrep \
    unzip \
    vim-tiny \
    xz-utils
rm -rf /var/lib/apt/lists/*
