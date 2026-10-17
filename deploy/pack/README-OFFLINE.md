# ATRI-X 离线部署（Python 版）

本目录是「无外网 / 弱网目标机」部署用的包。生成方式：

```bash
python deploy/pack/prepare_offline.py
```

产物在 `deploy/dist/`：

| 文件 | 说明 |
|---|---|
| `atri-x-<version>-src.tar.gz` | 源码离线包（`atri_x/` + 部署文件 + `Dockerfile.prebuilt`） |
| `atri-x-<version>-linux-amd64.tar.gz` | 预构建镜像 tar（`docker load` 即可用；生成需要 buildx） |
| `atri-x-<version>-linux-arm64.tar.gz` | 同上，arm64 |
| `atri-x-<version>-offline-amd64.tar.gz` | 完整离线包（amd64） |
| `atri-x-<version>-offline-arm64.tar.gz` | 完整离线包（arm64） |
| `SHA256SUMS` | 全部产物的 sha256 |

只想要源码包、不生成镜像 tar（不需要 Docker / buildx）：

```bash
python deploy/pack/prepare_offline.py --skip-images
```

---

## 1. 为什么 Python 版不需要交叉编译

上游 `prepare-offline.sh` 要用 Go 工具链交叉编译 amd64/arm64 静态二进制，
再用 `build-image-tar.py` 手工拼 `scratch` 镜像层。Python 版：

- **没有编译产物**：`atri_x/` 源码直接进镜像，基础镜像是官方 `python:*-slim`；
- 因此“离线”的难点从“编译”变成“**基础镜像怎么进去**”，两条路：
  1. 目标机已经能拉 `python:*-slim`（或内网有镜像仓库）→ 用源码包 + `docker compose build`；
  2. 完全离线 → 用预构建镜像 tar + `docker load`。

---

## 2. 目标机操作（完整离线包）

```bash
tar -xzf atri-x-0.9.0-offline-amd64.tar.gz
cd offline-amd64
cp .env.example .env && chmod 600 .env
vim .env                    # 填 DEEPSEEK_API_KEY / ATRIX_CLI_LOCAL_TOKEN / ATRINIS_HOME_TOKEN
python3 deploy.py --load    # 载入包内镜像并启动（完全离线）
```

或者用源码 + 本机构建：

```bash
python3 deploy.py           # docker compose build && up -d
```

`deploy.py`（即 `pack/deploy_lighthouse.py`）会：

1. 检测架构（`x86_64`→amd64、`aarch64`→arm64），并核对包内镜像 tar 的架构；
2. 检查 `docker` 命令、`docker info`、`docker compose`；
3. 没有 `.env` 就从 `.env.example` 生成并提示填写（首次运行会退出，填完再跑）；
4. 准备 `data/`、`data/cache/`，并把属主改成容器内 `10001:10001`；
5. 载入镜像或构建；
6. `up -d --remove-orphans`，打印后续命令。

---

## 3. 校验镜像 tar

```bash
python3 verify_image_tar.py --tag atri-x:0.9.0 atri-x-0.9.0-linux-amd64.tar.gz
```

校验内容：tar 结构可读、`manifest.json` 存在且 `RepoTags` 含目标 tag、
`config` 里没有 Go 时代残留、层数与大小合理。校验**不导入 Docker**，
所以在没有 Docker 的机器上也能先验包。

配合 `SHA256SUMS`：

```bash
sha256sum -c SHA256SUMS          # Linux
shasum -a 256 -c SHA256SUMS      # macOS
```

---

## 4. 常见问题

**Q：`docker load` 报 `no space left on device`。**
A：镜像约 150 MB（`python:*-slim` 约 122 MB + 源码），解压后需要约 400 MB 空闲。
用 `docker system prune` 清理后重试。

**Q：目标机是 arm64，我拿的是 amd64 包。**
A：`deploy.py --load` 会检测到包名里的架构与当前机器不一致并告警。
请取对应架构的包，或用源码包在本机构建。

**Q：`python3 deploy.py` 卡在 `docker compose build`。**
A：它在拉 `python:*-slim`。完全离线的机器请改用 `--load`，或先在内网仓库
准备好基础镜像并把 `.env` 里的 `PY_BASE_IMAGE` 指向它。

**Q：为什么离线包里镜像 tar 是 `.tar.gz` 而不是 `.tar`？**
A：`docker load -i` 直接支持 gzip 压缩的 tar；压缩后体积约为原来的 1/3。
