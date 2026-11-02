"""容器后端（Docker / Podman）：Linux 容器执行服务。

第一版只做一个后端：在 Linux 宿主上用 rootless Podman / Docker，在 Windows 上通过
Docker Desktop（其本身跑在 WSL2 Linux VM 里）。WSL2 / 虚拟化 / 引擎不可用时，
:meth:`probe` 会给出明确的失败项，上层**拒绝执行**而不是回落宿主 Shell。

安全基线（全部由控制面固定，模型无法指定）：

* 根文件系统只读（``--read-only``）；
* ``--cap-drop ALL`` + ``no-new-privileges`` + 默认 seccomp；
* ``--pids-limit`` / ``--memory`` + ``--memory-swap`` 同值 / ``--cpus`` / ``--cpu-shares``；
* 只挂载租户工作区、私有 home 与临时目录三个可写面；
* 联网档下容器只接一个 ``internal`` 网络，唯一出口是同一网络里的代理容器；
* 不挂 docker.sock / 宿主根 / 用户 home / 应用数据目录。
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any

from atri_x.sandbox.backend import (
    BackendInfo,
    CapabilityCheck,
    CapabilityReport,
    EnvInfo,
    EnvRequest,
    EnvStats,
    ExecResult,
    SandboxBackend,
)
from atri_x.sandbox.container_cli import (
    EngineCLI,
    EngineCommandResult,
    classify_failure,
    decode_output,
    parse_stats,
    read_capped_output,
    resolve_engine,
)
from atri_x.sandbox.errors import (
    SandboxBackendError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from atri_x.sandbox.image import (
    LABEL_KEY,
    apt_setup_source,
    build_digest,
    dockerfile_text,
    egress_source,
)
from atri_x.sandbox.network import DENIED_PORTS, EgressSpec, build_proxy_env  # noqa: F401
from atri_x.sandbox.probe import PROBE_SCRIPT, parse_probe_output, probe_checks
from atri_x.sandbox.settings import SandboxSettings

__all__ = ["ContainerBackend", "new_backend"]

_LABEL = "atrix-sandbox"
#: 用户/进程相关常量（与镜像里的 uid 对齐）。
_RUN_UID = 1000
_RUN_GID = 1000


class ContainerBackend(SandboxBackend):
    """Docker/Podman 命令行后端。"""

    name = "docker"

    def __init__(self, settings: SandboxSettings, cli: EngineCLI | None = None) -> None:
        self.settings = settings
        self.cli = cli or resolve_engine(settings.backend, settings.engine_path)
        if self.cli is not None:
            self.name = self.cli.kind
        self._image_ready = False
        self._probe: CapabilityReport | None = None

    # -- 基础 ---------------------------------------------------------------
    async def backend_info(self) -> BackendInfo:
        """引擎存在且守护进程可达才算可用。"""
        if self.cli is None:
            return BackendInfo(
                backend=self.name,
                available=False,
                error="未找到 docker / podman 命令；Windows 需要 Docker Desktop（WSL2 后端）",
            )
        ok, version = await self.cli.version()
        return BackendInfo(
            backend=self.name,
            engine=self.cli.path,
            version=version if ok else "",
            available=ok,
            error="" if ok else version,
        )

    async def _run(self, args: list[str], *, timeout: float = 120.0) -> EngineCommandResult:
        """跑一条引擎命令；引擎缺失时抛「沙箱不可用」。"""
        if self.cli is None:
            raise SandboxUnavailableError(
                "沙箱后端不可用", detail="未找到容器引擎命令（docker / podman）"
            )
        return await self.cli.run(args, timeout=timeout)

    async def _run_ok(
        self, args: list[str], *, timeout: float = 120.0, action: str = "引擎命令"
    ) -> EngineCommandResult:
        """要求成功的引擎命令；失败抛后端错误（错误里保留引擎输出的最后几行）。"""
        result = await self._run(args, timeout=timeout)
        if not result.ok:
            raise SandboxBackendError(
                f"{action}失败", detail=result.error_text()
            )
        return result

    async def ensure_image(self) -> str:
        """确保镜像存在且指纹匹配；必要时按内置 Dockerfile 构建。"""
        image = self.settings.image
        if self._image_ready:
            return image
        digest = build_digest()
        label = await self._run(
            ["image", "inspect", "--format", "{{ index .Config.Labels \"" + LABEL_KEY + "\" }}", image],
            timeout=60,
        )
        if label.ok and label.stdout.strip() == digest:
            self._image_ready = True
            return image
        if label.ok:
            # 指纹不匹配：删掉旧镜像再重建，避免「改了代理代码还在用旧镜像」。
            await self._run(["image", "rm", "-f", image], timeout=120)
        if not self.settings.image_auto_build:
            raise SandboxUnavailableError(
                "沙箱镜像不存在或已过期",
                detail=f"需要镜像 {image}（构建指纹 {digest}）；"
                "[sandbox].image_auto_build=false 时不会自动构建",
            )
        await self._build_image(image, digest)
        self._image_ready = True
        return image

    async def _build_image(self, image: str, digest: str) -> None:
        """把内置 Dockerfile + 代理源码写成 build context 并构建镜像。"""
        context = self.settings.image_context_dir
        os.makedirs(context, exist_ok=True)
        for name, content in (
            ("Dockerfile", dockerfile_text()),
            ("apt-setup.sh", apt_setup_source()),
            ("egress.py", egress_source()),
        ):
            path = os.path.join(context, name)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        build_args = [
            "build",
            "--label",
            f"{LABEL_KEY}={digest}",
            "--label",
            f"{_LABEL}=1",
        ]
        if self.settings.apt_mirror:
            build_args.extend(["--build-arg", f"ATRIX_APT_MIRROR={self.settings.apt_mirror}"])
        build_args.extend(
            ["-f", os.path.join(context, "Dockerfile"), "-t", image, context]
        )
        result = await self._run(build_args, timeout=1800)
        if not result.ok:
            raise SandboxUnavailableError(
                "沙箱镜像构建失败",
                detail=result.error_text(600),
            )

    async def probe(self) -> CapabilityReport:
        """实测隔离能力；必需项有任一失败即判定不可用。"""
        report = CapabilityReport(
            backend=self.name,
            image=self.settings.image,
            checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        info = await self.backend_info()
        report.engine_version = info.version
        if not info.available:
            report.checks.append(
                CapabilityCheck("engine", False, info.error or "引擎不可用")
            )
            report.ok = False
            self._probe = report
            return report
        try:
            await self.ensure_image()
        except SandboxUnavailableError as exc:
            report.checks.append(CapabilityCheck("image", False, str(exc)))
            report.ok = False
            self._probe = report
            return report
        probe_dir = os.path.join(self.settings.root, "probe")
        os.makedirs(probe_dir, exist_ok=True)
        name = f"atrix-sb-probe-{os.getpid()}-{secrets.token_hex(4)}"
        args = [
            "run", "--rm", "--name", name,
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m",
            "--cpus", "1", "--network", "none",
            "--tmpfs", "/tmp:rw,size=32m,mode=1777",
            "--user", f"{_RUN_UID}:{_RUN_GID}",
            "-v", f"{probe_dir}:/workspace:rw",
            self.settings.image, "/bin/sh", "-lc", PROBE_SCRIPT,
        ]
        result = await self._run(args, timeout=180)
        values = parse_probe_output(result.stdout)
        for check in probe_checks(values, result, probe_dir, uid=_RUN_UID):
            report.checks.append(check)
        report.ok = bool(report.checks) and not report.failed
        if not report.ok:
            report.warnings.append(
                "隔离探测未通过：按 fail-closed 策略拒绝执行沙箱命令"
            )
        self._probe = report
        return report

    # -- 环境生命周期 -------------------------------------------------------
    async def create_env(self, request: EnvRequest) -> EnvInfo:
        """创建（或复用）租户环境；失败抛后端错误，不回落宿主。"""
        image = await self.ensure_image()
        name = request.tenant.container_name
        network = request.egress.network_name if request.egress is not None else ""
        if network:
            await self._ensure_network(request.egress)
        if request.egress is not None:
            await self._ensure_proxy(request.egress, image)
        if await self._container_exists(name):
            info = await self._inspect_env(name, request.tenant.slug)
            if not info.running:
                await self._run_ok(["start", name], timeout=60, action="启动沙箱环境")
            return await self._inspect_env(name, request.tenant.slug)
        args = self._create_args(request, image, network)
        await self._run_ok(args, timeout=120, action="创建沙箱环境")
        await self._run_ok(["start", name], timeout=60, action="启动沙箱环境")
        return await self._inspect_env(name, request.tenant.slug)

    def _create_args(self, request: EnvRequest, image: str, network: str) -> list[str]:
        """拼出 ``docker create`` 参数（全部由控制面决定）。"""
        resources = request.resources
        paths = request.paths
        args = [
            "create", "--name", request.tenant.container_name,
            "--label", f"{_LABEL}=1",
            "--label", f"{_LABEL}.kind=env",
            "--label", f"{_LABEL}.tenant={request.tenant.slug}",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(resources.pids),
            "--memory", f"{resources.memory_mb}m",
            "--memory-swap", f"{resources.memory_mb}m",
            "--cpus", f"{resources.cpu_cores:g}",
            "--cpu-shares", str(max(2, int(resources.cpu_weight))),
            "--ulimit", "nofile=1024:4096",
            "--init",
            "--user", f"{_RUN_UID}:{_RUN_GID}",
            "--workdir", "/workspace",
            "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
            "-v", f"{paths.workspace}:/workspace:rw",
            "-v", f"{paths.home}:/home/atri:rw",
            "-v", f"{paths.tmp}:/tmp:rw",
            "-e", "HOME=/home/atri",
            "-e", "TMPDIR=/tmp",
            "-e", "XDG_CACHE_HOME=/home/atri/.cache",
            "-e", "XDG_CONFIG_HOME=/home/atri/.config",
            "-e", "XDG_DATA_HOME=/home/atri/.local/share",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PIP_NO_CACHE_DIR=1",
            "-e", f"ATRIX_TENANT={request.tenant.slug}",
        ]
        for key, value in request.labels.items():
            args.extend(["--label", f"{key}={value}"])
        if network:
            args.extend(["--network", network])
            for key, value in build_proxy_env(request.egress, host=request.egress.proxy_name).items():
                args.extend(["-e", f"{key}={value}"])
        else:
            args.extend(["--network", "none"])
        args.append(image)
        return args

    async def _ensure_network(self, egress: EgressSpec) -> None:
        """确保租户内部网络存在（``--internal`` = 无默认路由/无外部 DNS）。"""
        name = egress.network_name
        result = await self._run(["network", "inspect", name], timeout=30)
        if result.ok:
            return
        await self._run_ok(
            [
                "network", "create", "--internal",
                "--label", f"{_LABEL}=1",
                "--label", f"{_LABEL}.kind=net",
                "--label", f"{_LABEL}.tenant={egress.tenant_slug}",
                name,
            ],
            timeout=60,
            action="创建沙箱内部网络",
        )

    async def _ensure_proxy(self, egress: EgressSpec, image: str) -> None:
        """确保出口代理容器在租户内部网络上运行，并接一条通往外网的 bridge。"""
        state_dir = egress.proxy_state_dir
        os.makedirs(state_dir, exist_ok=True)
        name = egress.proxy_name
        if await self._container_exists(name):
            info = await self._run(["inspect", "--format", "{{.State.Running}}", name], timeout=30)
            if info.ok and info.stdout.strip().lower() == "true":
                await self._connect_network("bridge", name)
                return
            await self._run(["rm", "-f", name], timeout=60)
        args = [
            "run", "-d", "--name", name,
            "--label", f"{_LABEL}=1",
            "--label", f"{_LABEL}.kind=proxy",
            "--label", f"{_LABEL}.tenant={egress.tenant_slug}",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m",
            "--cpus", "0.5", "--init",
            "--network", egress.network_name,
            "--log-driver", "json-file", "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
            "--tmpfs", "/tmp:rw,size=16m,mode=1777",
            "-v", f"{state_dir}:/state:rw",
        ]
        for key, value in egress.proxy_env().items():
            args.extend(["-e", f"{key}={value}"])
        args.extend([image, "python3", "/opt/atri/egress.py"])
        await self._run_ok(args, timeout=120, action="启动沙箱出口代理")
        await self._connect_network("bridge", name)

    async def _connect_network(self, network: str, container: str) -> None:
        """把容器接到某个网络（已连接时忽略错误）。"""
        await self._run(["network", "connect", network, container], timeout=60)

    async def start_env(self, name: str) -> None:
        """启动环境（已运行则什么都不做）。"""
        if await self._container_exists(name):
            info = await self._run(["inspect", "--format", "{{.State.Running}}", name], timeout=30)
            if info.ok and info.stdout.strip().lower() == "true":
                return
            await self._run_ok(["start", name], timeout=60, action="启动沙箱环境")
            return
        raise SandboxBackendError("沙箱环境不存在", detail=f"{name}（需要先 create）")

    async def stop_env(self, name: str, *, grace_seconds: float = 5.0) -> None:
        """停止环境并确认其中进程已回收（``docker stop`` 会 SIGTERM→SIGKILL 整个容器）。"""
        if not await self._container_exists(name):
            return
        await self._run(["stop", "-t", str(max(1, int(grace_seconds))), name], timeout=90)
        for _ in range(10):
            info = await self._run(["inspect", "--format", "{{.State.Running}}", name], timeout=30)
            if info.ok and info.stdout.strip().lower() != "true":
                return
            await asyncio.sleep(0.2)
        await self._run(["kill", "--signal", "SIGKILL", name], timeout=60)

    async def remove_env(self, name: str, *, proxy_name: str = "") -> None:
        """销毁环境、代理与租户网络（保留宿主上的租户数据目录）。"""
        await self._run(["rm", "-f", name], timeout=90)
        if proxy_name:
            await self._run(["rm", "-f", proxy_name], timeout=90)
        if proxy_name:
            network = proxy_name.replace("atrix-sb-egress-", "atrix-sb-net-")
            await self._run(["network", "rm", network], timeout=60)

    # -- 执行与统计 ---------------------------------------------------------
    async def exec_in_env(
        self,
        name: str,
        command: str,
        *,
        timeout: float,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        max_output_bytes: int = 256 * 1024,
    ) -> ExecResult:
        """在环境内执行命令：输出有界，``proc.wait()`` 与管道读取一起受超时约束。"""
        started = time.monotonic()
        args = ["exec", "--workdir", workdir]
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        args.extend([name, "/bin/sh", "-lc", command])
        process = await self._spawn(args)
        try:
            out_b, err_b, _ = await asyncio.wait_for(
                asyncio.gather(
                    read_capped_output(process.stdout, max_output_bytes),
                    read_capped_output(process.stderr, max_output_bytes),
                    process.wait(),
                ),
                timeout=max(1.0, float(timeout)),
            )
        except asyncio.TimeoutError as exc:
            await self._kill_env(name)
            raise SandboxTimeoutError(
                f"沙箱命令超时（{timeout:g}s），已终止该环境内全部进程",
                detail="超时后环境会被回收，下次执行自动重建",
            ) from exc
        except asyncio.CancelledError:
            await self._kill_env(name)
            raise
        out_text, out_truncated = decode_output(out_b)
        err_text, err_truncated = decode_output(err_b)
        return ExecResult(
            exit_code=int(process.returncode or 0),
            stdout=out_text,
            stderr=err_text,
            truncated=bool(out_truncated or err_truncated),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_kind=classify_failure(process.returncode, err_text),
        )

    async def _spawn(self, args: list[str]) -> asyncio.subprocess.Process:
        """启动一条引擎命令（exec 用；输出走管道）。"""
        if self.cli is None:
            raise SandboxUnavailableError("沙箱后端不可用", detail="未找到容器引擎")
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "stdin": asyncio.subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
        try:
            return await asyncio.create_subprocess_exec(self.cli.path, *args, **kwargs)
        except OSError as exc:
            raise SandboxBackendError("无法启动容器引擎", detail=str(exc)) from exc

    async def _kill_env(self, name: str) -> None:
        """杀掉整个环境（容器 kill = 该租户全部进程一起回收）。"""
        await self._run(["kill", name], timeout=60)
        for _ in range(10):
            info = await self._run(["inspect", "--format", "{{.State.Running}}", name], timeout=30)
            if info.ok and info.stdout.strip().lower() != "true":
                return
            await asyncio.sleep(0.2)

    async def env_stats(self, name: str) -> EnvStats:
        """读取容器资源占用。"""
        result = await self._run(
            ["stats", "--no-stream", "--format", "{{json .}}", name], timeout=60
        )
        data = parse_stats(result.stdout)
        return EnvStats(
            cpu_percent=float(data.get("cpu_percent", 0.0) or 0.0),
            memory_bytes=int(data.get("memory_bytes", 0) or 0),
            memory_limit_bytes=int(data.get("memory_limit_bytes", 0) or 0),
            pids=int(data.get("pids", 0) or 0),
            net_rx_bytes=int(data.get("net_rx_bytes", 0) or 0),
            net_tx_bytes=int(data.get("net_tx_bytes", 0) or 0),
        )

    async def list_envs(self) -> list[EnvInfo]:
        """列出全部沙箱环境。"""
        result = await self._run(
            [
                "ps", "-a",
                "--filter", f"label={_LABEL}.kind=env",
                "--format", "{{json .}}",
            ],
            timeout=60,
        )
        items: list[EnvInfo] = []
        import json

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            labels = _parse_labels(str(data.get("Labels", "") or ""))
            items.append(
                EnvInfo(
                    name=str(data.get("Names", "") or ""),
                    tenant_slug=labels.get(f"{_LABEL}.tenant", ""),
                    running=str(data.get("State", "")).lower() == "running",
                    created_at=str(data.get("CreatedAt", "") or ""),
                    image=str(data.get("Image", "") or ""),
                    network=labels.get(f"{_LABEL}.network", ""),
                    proxy=labels.get(f"{_LABEL}.proxy", ""),
                )
            )
        return items

    async def set_online(self, name: str, online: bool, network: Any) -> None:
        """强制断开/恢复租户网络（流量额度用尽时由控制面调用）。"""
        if network is None or not getattr(network, "online", False):
            return
        net_name = "atrix-sb-net-" + name.replace("atrix-sb-", "")
        if online:
            await self._run(["network", "connect", net_name, name], timeout=60)
        else:
            await self._run(["network", "disconnect", "-f", net_name, name], timeout=60)

    async def cleanup_orphans(self, known_envs: set[str], known_proxies: set[str]) -> int:
        """回收崩溃/重启后遗留的容器与网络。"""
        removed = 0
        result = await self._run(
            ["ps", "-a", "--filter", f"label={_LABEL}=1", "--format", "{{json .}}"],
            timeout=60,
        )
        import json

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            name = str(data.get("Names", "") or "")
            if name and name not in known_envs and name not in known_proxies:
                rc = await self._run(["rm", "-f", name], timeout=90)
                removed += 1 if rc.ok else 0
        networks = await self._run(
            ["network", "ls", "--filter", f"label={_LABEL}=1", "--format", "{{.Name}}"],
            timeout=60,
        )
        keep = {
            "atrix-sb-net-" + name.replace("atrix-sb-", "")
            for name in known_envs
        }
        for net in networks.stdout.splitlines():
            net = net.strip()
            if net and net not in keep:
                rc = await self._run(["network", "rm", net], timeout=60)
                removed += 1 if rc.ok else 0
        return removed

    async def aclose(self) -> None:
        """释放后端资源（命令行后端无需常驻连接）。"""
        return None

    # -- 内部 ---------------------------------------------------------------
    async def _container_exists(self, name: str) -> bool:
        result = await self._run(["inspect", "--type", "container", name], timeout=30)
        return result.ok

    async def _inspect_env(self, name: str, tenant_slug: str) -> EnvInfo:
        result = await self._run(
            ["inspect", "--format", "{{.State.Running}}|{{.Created}}|{{.Config.Image}}", name],
            timeout=30,
        )
        parts = (result.stdout or "").strip().split("|")
        return EnvInfo(
            name=name,
            tenant_slug=tenant_slug,
            running=len(parts) > 0 and parts[0].lower() == "true",
            created_at=parts[1] if len(parts) > 1 else "",
            image=parts[2] if len(parts) > 2 else "",
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _parse_labels(raw: str) -> dict[str, str]:
    """把 docker ``Labels`` 字符串 ``k=v,k=v`` 解析成字典。"""
    labels: dict[str, str] = {}
    for item in (raw or "").split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            labels[key.strip()] = value.strip()
    return labels

