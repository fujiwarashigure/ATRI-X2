"""重新生成 ATRI-X Python 版的离线部署产物（无需任何第三方依赖）。

对应上游：``deploy/pack/prepare-offline.sh``（153 行 bash）。

与上游最大的差异：**Python 版没有编译步骤**。上游要交叉编译 amd64/arm64 静态
二进制、再用 ``build-image-tar.py`` 把二进制塞进 ``scratch`` 镜像；Python 版
直接打包**源码树 + 预构建镜像 tar**，因此：

- 不需要 Go 工具链，不需要交叉编译；
- hadolint / buildx 只在**真要产出镜像 tar** 时才需要（``--skip-images`` 可跳过）；
- 离线包内是 ``atri_x/`` 源码 + ``Dockerfile.prebuilt``（``python:*-slim``
  基础镜像 + 源码），目标机 ``docker compose build`` 即可，或用预构建 tar
  ``docker load`` 完全离线。

用法::

    python deploy/pack/prepare_offline.py
    python deploy/pack/prepare_offline.py --skip-images     # 只出源码离线包
    ATRIX_VERSION=0.9.0 python deploy/pack/prepare_offline.py

产物（``deploy/dist/``）::

    atri-x-<version>-src.tar.gz                 源码离线包（含 Dockerfile.prebuilt）
    atri-x-<version>-linux-<arch>.tar.gz        预构建镜像 tar（需 docker buildx）
    atri-x-<version>-offline-<arch>.tar.gz      完整离线包
    SHA256SUMS                                  全部产物的校验和
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = Path(__file__).resolve().parents[1]
_PACK = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

ARCHES = ("amd64", "arm64")
#: 离线包里需要 LF 结尾的文本文件后缀（Windows 检出常带 CRLF）。
LF_SUFFIXES = {".py", ".sh", ".service", ".timer", ".conf", ".yml", ".yaml", ".example", ".toml", ".md"}
LF_NAMES = {"Dockerfile", "VERSION", "docker-compose.yml"}
#: 打进源码离线包的顶层条目（相对仓库根）。
SOURCE_ENTRIES = ("atri_x", "pyproject.toml", "README.md")
#: 复制进离线包的 deploy 文件：(源, 包内名字)
PACK_FILES = (
    ("Dockerfile", "Dockerfile"),
    ("docker-compose.yml", "docker-compose.yml"),
    (".env.example", ".env.example"),
    ("nginx-atri-x.conf", "nginx-atri-x.conf"),
    ("atri-x-compose.service", "atri-x-compose.service"),
    ("atri-x.service", "atri-x.service"),
    ("atri-x-watchdog.service", "atri-x-watchdog.service"),
    ("atri-x-watchdog.timer", "atri-x-watchdog.timer"),
    ("watchdog.env.example", "watchdog.env.example"),
    ("atri_x_watchdog.py", "atri_x_watchdog.py"),
    ("backup.py", "backup.py"),
    ("restore_verify.py", "restore_verify.py"),
    ("rollback.py", "rollback.py"),
    ("upgrade.py", "upgrade.py"),
    ("health_probe.py", "health_probe.py"),
    ("init_host.py", "init.sh.py"),
    ("_lib.py", "_lib.py"),
    ("_libfs.py", "_libfs.py"),
    ("_envutil.py", "_envutil.py"),
    ("_proc.py", "_proc.py"),
    ("_netutil.py", "_netutil.py"),
    ("_console.py", "_console.py"),
)


def normalize_line_endings(root: Path) -> int:
    """把包内文本文件的 CRLF 统一成 LF，返回修改的文件数。

    Windows 检出的 ``*.sh`` / ``*.service`` 带 CRLF 时，Linux 上 ``./deploy.sh``
    会报 ``bad interpreter: /usr/bin/env bash^M``，systemd unit 也可能解析失败。
    Python 版全部是 ``*.py`` 与声明式文件，同样需要这一步。
    """
    changed = 0
    for path in _lib.iter_files(root):
        if path.suffix not in LF_SUFFIXES and path.name not in LF_NAMES:
            continue
        data = path.read_bytes()
        if b"\r\n" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n"))
            changed += 1
    return changed


class OfflinePack:
    """离线包生成流程。"""

    def __init__(self, skip_images: bool = False) -> None:
        self.repo_root = _REPO
        self.deploy_dir = _DEPLOY
        self.pack_dir = _PACK
        self.dist_dir = self.deploy_dir / "dist"
        self.skip_images = skip_images
        self.version = _lib.env_value("ATRIX_VERSION") or (
            _lib.read_text(self.deploy_dir / "VERSION").strip() or "dev"
        )
        self.produced: list[Path] = []

    # ---------- 产物 ----------
    def source_archive(self) -> Path:
        """打源码离线包：``atri_x/`` + ``pyproject.toml`` + ``README.md`` + 部署文件。"""
        output = self.dist_dir / f"atri-x-{self.version}-src.tar.gz"
        _lib.log(f"组装源码离线包 {output.name}")
        staging = self.dist_dir / f".src-{self.version}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        for entry in SOURCE_ENTRIES:
            source = self.repo_root / entry
            if not source.exists():
                _lib.die(f"缺少源码条目：{source}")
            target = staging / entry
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, target)
        for source_name, pack_name in PACK_FILES:
            source = self.deploy_dir / source_name
            if not source.is_file():
                _lib.die(f"缺少部署文件：{source}")
            shutil.copy2(source, staging / pack_name)
        _lib.write_text(staging / "VERSION", self.version + "\n")
        normalize_line_endings(staging)
        with tarfile.open(output, "w:gz") as tar:
            for entry in sorted(staging.iterdir()):
                tar.add(entry, arcname=entry.name)
        shutil.rmtree(staging, ignore_errors=True)
        self.produced.append(output)
        return output

    def image_archives(self) -> list[Path]:
        """用 buildx 构建并导出各架构的镜像 tar。

        完全离线交付靠这一步；没有 buildx 或没有 Docker 时**不静默跳过**，
        而是明确报错——上游同理（``prepare-offline.sh`` 直接依赖 go 工具链）。
        """
        if self.skip_images:
            _lib.warn("--skip-images：跳过镜像 tar 生成（离线包将需要在目标机 build）")
            return []
        if not _lib.has_command("docker"):
            _lib.die("未找到 docker；无法生成镜像 tar。确认不需要时用 --skip-images")
        if _lib.run_capture(["docker", "buildx", "version"], timeout=60.0).returncode != 0:
            _lib.die("未找到 docker buildx；无法生成镜像 tar。确认不需要时用 --skip-images")

        outputs: list[Path] = []
        for arch in ARCHES:
            output = self.dist_dir / f"atri-x-{self.version}-linux-{arch}.tar.gz"
            _lib.log(f"生成镜像 tar linux/{arch} -> {output.name}")
            raw = self.dist_dir / f".image-{arch}.tar"
            raw.unlink(missing_ok=True)
            argv = [
                "docker",
                "buildx",
                "build",
                "--platform",
                f"linux/{arch}",
                "--build-arg",
                f"VERSION={self.version}",
                "-f",
                "deploy/Dockerfile",
                "-t",
                f"atri-x:{self.version}",
                "--output",
                f"type=docker,dest={raw}",
                ".",
            ]
            code = _lib.run_inherit(argv, cwd=self.repo_root)
            if code != 0:
                _lib.die(f"buildx 构建 linux/{arch} 失败（退出码 {code}）")
            with raw.open("rb") as src, output.open("wb") as dst:
                import gzip

                with gzip.GzipFile(fileobj=dst, mode="wb", mtime=0) as gz:
                    shutil.copyfileobj(src, gz)
            raw.unlink(missing_ok=True)
            self.produced.append(output)
            outputs.append(output)
        return outputs

    def offline_bundle(self, image_archives: list[Path]) -> list[Path]:
        """把源码 + 部署文件 + （可选）对应架构镜像 tar 组成完整离线包。"""
        bundles: list[Path] = []
        by_arch = {path.name.split("-linux-")[-1].removesuffix(".tar.gz"): path for path in image_archives}
        for arch in ARCHES:
            output = self.dist_dir / f"atri-x-{self.version}-offline-{arch}.tar.gz"
            staging = self.dist_dir / f"offline-{arch}"
            _lib.log(f"组装 {staging.name}")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            for entry in SOURCE_ENTRIES:
                source = self.repo_root / entry
                target = staging / entry
                if source.is_dir():
                    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                elif source.is_file():
                    shutil.copy2(source, target)
            for source_name, pack_name in PACK_FILES:
                shutil.copy2(self.deploy_dir / source_name, staging / pack_name)
            _lib.write_text(staging / "VERSION", self.version + "\n")
            (staging / "data").mkdir(exist_ok=True)
            image = by_arch.get(arch)
            if image is not None:
                shutil.copy2(image, staging / image.name)
            _lib.write_text(
                staging / "README-OFFLINE.md",
                offline_readme(self.version, arch, image is not None),
            )
            normalize_line_endings(staging)
            with tarfile.open(output, "w:gz") as tar:
                tar.add(staging, arcname=staging.name)
            shutil.rmtree(staging, ignore_errors=True)
            self.produced.append(output)
            bundles.append(output)
        return bundles

    def checksums(self) -> Path:
        """为全部产物生成 ``SHA256SUMS``。"""
        sums = self.dist_dir / "SHA256SUMS"
        lines = []
        for path in sorted({*self.produced, *self.dist_dir.glob("atri-x-*.tar.gz")}):
            if not path.is_file():
                continue
            lines.append(f"{_lib.file_sha256(path)}  {path.name}")
        _lib.write_text(sums, "\n".join(lines) + ("\n" if lines else ""))
        _lib.log(f"校验和：{sums}")
        return sums

    def run(self) -> int:
        """执行全部步骤；返回进程退出码。"""
        self.dist_dir.mkdir(parents=True, exist_ok=True)
        self.source_archive()
        images = self.image_archives()
        self.offline_bundle(images)
        self.checksums()
        _lib.log(f"完成，产物在 {self.dist_dir}")
        for path in sorted(self.dist_dir.glob("atri-x-*")):
            size = path.stat().st_size / 1048576
            sys.stdout.write(f"  {path.name}  {size:.1f} MiB\n")
        return 0


def offline_readme(version: str, arch: str, with_image: bool) -> str:
    """生成离线包内 README（内容随是否含镜像 tar 变化）。"""
    lines = [
        f"# ATRI-X {version} 离线部署包（linux/{arch}，Python 版）",
        "",
        "本包纯标准库运行，**没有安装步骤、不需要 venv、不需要编译**。",
        "",
        "## 目录内容",
        "",
        "| 文件 | 说明 |",
        "|---|---|",
        "| `atri_x/` | 应用源码（Python 包） |",
        "| `pyproject.toml` / `README.md` | 项目元数据与说明 |",
        "| `Dockerfile` | 镜像构建文件（`python:*-slim` 基础镜像） |",
        "| `docker-compose.yml` | 单机常驻服务编排 |",
        "| `.env.example` | 环境变量模板 |",
        "| `backup.py` / `restore_verify.py` | 备份与恢复验证 |",
        "| `rollback.py` / `upgrade.py` | 回滚与升级 |",
        "| `health_probe.py` | 独立健康探针 |",
        "| `atri_x_watchdog.py` | 外部看门狗 |",
    ]
    if with_image:
        lines += [
            f"| `atri-x-{version}-linux-{arch}.tar.gz` | 预构建镜像（`docker load` 即可，完全离线） |",
        ]
    lines += [
        "",
        "## 部署",
        "",
        "```bash",
        "cp .env.example .env && chmod 600 .env",
        "vim .env                       # 填 API Key / token",
    ]
    if with_image:
        lines += [
            f"docker load -i atri-x-{version}-linux-{arch}.tar.gz",
            "docker compose up -d           # 直接用已载入的镜像，无需联网构建",
        ]
    else:
        lines += [
            "docker compose build           # 需要能拉取 python:*-slim 基础镜像",
            "docker compose up -d",
        ]
    lines += [
        "```",
        "",
        "## 验收",
        "",
        "```bash",
        "python3 health_probe.py            # /live",
        "python3 health_probe.py --ready    # 调度与数据目录就绪",
        "docker compose exec atri-x python -m atri_x doctor   # 部署验收",
        "```",
        "",
        "## 运维",
        "",
        "```bash",
        "python3 backup.py                             # 一致性备份（SQLite 在线快照）",
        "python3 restore_verify.py backups/atri-x-data-*.tar.gz   # 隔离恢复演练",
        "python3 upgrade.py                            # 升级（含数据/镜像快照）",
        "python3 rollback.py                           # 回滚",
        "```",
        "",
        "看门狗由 systemd timer 每分钟执行 `atri_x_watchdog.py`；",
        "配置见 `watchdog.env.example`。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="prepare_offline.py",
        description="生成 ATRI-X Python 版离线部署产物",
    )
    parser.add_argument("--skip-images", action="store_true", help="只出源码离线包，不生成镜像 tar")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    return OfflinePack(skip_images=args.skip_images).run()


if __name__ == "__main__":
    raise SystemExit(main())
