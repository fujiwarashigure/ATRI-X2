"""Docker CLI 的薄封装：冒烟 / 稳定性测试里**唯一**直接调 ``docker`` 的地方。

对应上游：无。上游 Go 版部署套件只有 ``deploy/tests/*.sh`` 那 7 个脚本，
没有容器冒烟测试，也没有长跑稳定性测试；本模块是 Python 版新增的。

为什么单独一个模块
------------------
``smoke.py`` / ``stability.py`` 需要 ``docker build`` / ``run`` / ``exec`` /
``inspect`` / ``stats`` / ``logs`` 六七个子命令，且每一处都要处理「退出码非 0」。
把「怎么调 docker」收敛到这一个类，带来两个直接好处：

1. **可在没有 Docker 的机器上被验证**：测试把 :meth:`DockerCLI._run` 换成假实现
   （见 ``tests/test_deploy_harness.py``），用预置 stdout 驱动全部分支；
2. ``_lib.run_capture`` 本身也是注入点（``ATRIX_DEPLOY_FAKE_CAPTURE``），
   所以连真实 :class:`DockerCLI` 都能被端到端地喂假输出。

约定
----
* 所有方法都**不抛异常**：退出码非 0 由调用方判断（与 :mod:`_proc` 一致）。
  命令不存在 → 127，超时 → 124，无法执行 → 126。
* ``docker`` 可执行文件名与单次调用超时都可覆盖，方便测试与非默认安装路径。
* 只做「拼参数 + 拆输出」，不做任何业务判断——业务判断在
  :mod:`_smokelib` 与两个脚本里，便于单测。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _candidate in (str(_REPO), str(_HERE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

__all__ = ["DockerCLI", "DEFAULT_DOCKER", "DEFAULT_TIMEOUT"]

#: 默认的 docker 可执行文件名（``PATH`` 上找不到时退出码是 127）。
DEFAULT_DOCKER = "docker"
#: 单次 docker 调用的默认超时（秒）。``docker build`` 会显式传更长的值。
DEFAULT_TIMEOUT = 600.0


class DockerCLI:
    """``docker`` 命令行封装。子命令一一对应，参数原样拼接。

    ``capture`` 默认是 :func:`_lib.run_capture`；测试可传入自己的实现，
    或直接继承本类覆写 :meth:`_run`（推荐，见测试文件里的 ``FakeDocker``）。
    """

    def __init__(
        self,
        executable: str = DEFAULT_DOCKER,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        capture: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.executable = executable
        self.timeout = timeout
        self._capture = capture if capture is not None else _lib.run_capture

    # ------------------------------------------------------------------
    # 注入点
    # ------------------------------------------------------------------
    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess:
        """跑 ``docker <args...>`` 并捕获输出；**永不抛异常**。"""
        return self._capture(
            [self.executable, *args],
            cwd=cwd,
            timeout=self.timeout if timeout is None else timeout,
        )

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def version(self) -> str:
        """Docker **服务端**版本；守护进程不可用时返回空串。

        这里刻意问 ``.Server.Version`` 而不是 ``docker --version``：客户端版本
        在守护进程没起来时照样能打印，用它判断「Docker 是否可用」会得到假阳性
        （本机 ``docker version`` 报 ``npipe:////./pipe/dockerDesktopLinuxEngine``
        找不到，而 ``docker --version`` 仍然成功）。
        """
        done = self._run(["version", "--format", "{{.Server.Version}}"], timeout=60.0)
        if done.returncode != 0:
            return ""
        return (done.stdout or "").strip()

    def probe_error(self) -> str:
        """可用性探测失败时的原始输出（取 stderr 优先，便于打印根因）。"""
        done = self._run(["version", "--format", "{{.Server.Version}}"], timeout=60.0)
        text = (done.stderr or "").strip() or (done.stdout or "").strip()
        return text or f"docker version 退出码 {done.returncode}"

    # ------------------------------------------------------------------
    # 镜像
    # ------------------------------------------------------------------
    def image_present(self, tag: str) -> bool:
        """本地是否已有该镜像（``docker image inspect``）。"""
        return self._run(["image", "inspect", tag], timeout=120.0).returncode == 0

    def build(
        self,
        tag: str,
        *,
        dockerfile: str = "deploy/Dockerfile",
        context: str = ".",
        build_args: Sequence[str] = (),
        timeout: float = 3600.0,
        cwd: str | None = None,
    ) -> subprocess.CompletedProcess:
        """``docker build -f <dockerfile> -t <tag> [--build-arg ...] <context>``。

        与 :file:`deploy/README.md` 第 2 节给的构建命令一致：``-f deploy/Dockerfile``
        + 仓库根作为上下文。
        """
        args = ["build", *build_args, "-f", dockerfile, "-t", tag, context]
        return self._run(args, timeout=timeout, cwd=cwd)

    def remove_image(self, tag: str) -> subprocess.CompletedProcess:
        """``docker rmi``（清理用；被占用时非 0，调用方忽略）。"""
        return self._run(["rmi", "-f", tag], timeout=300.0)

    # ------------------------------------------------------------------
    # 卷
    # ------------------------------------------------------------------
    def create_volume(self, name: str) -> subprocess.CompletedProcess:
        """``docker volume create``（幂等：已存在也返回 0）。"""
        return self._run(["volume", "create", name], timeout=120.0)

    def remove_volume(self, name: str) -> subprocess.CompletedProcess:
        """``docker volume rm -f``（清理用）。"""
        return self._run(["volume", "rm", "-f", name], timeout=120.0)

    # ------------------------------------------------------------------
    # 容器
    # ------------------------------------------------------------------
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        """``docker run <args...>``；``args`` 不含开头的 ``run``。"""
        return self._run(["run", *args])

    def exec(
        self,
        container: str,
        command: Sequence[str],
        *,
        timeout: float = 60.0,
        user: str = "",
    ) -> subprocess.CompletedProcess:
        """``docker exec <container> <command...>``（不带 ``-i``/``-t``）。

        ``command`` 是 argv 列表，**不经过 shell**：因此探针命令里的引号、
        括号都不需要转义，容器内的 Python 拿到的就是原样参数。
        """
        args = ["exec"]
        if user:
            args += ["--user", user]
        args += [container, *command]
        return self._run(args, timeout=timeout)

    def inspect(self, container: str, fmt: str) -> subprocess.CompletedProcess:
        """``docker inspect --format <fmt> <container>``。"""
        return self._run(["inspect", "--format", fmt, container], timeout=120.0)

    def inspect_text(self, container: str, fmt: str) -> str:
        """按格式取一段文本；失败返回空串。"""
        done = self.inspect(container, fmt)
        if done.returncode != 0:
            return ""
        return (done.stdout or "").strip()

    def inspect_json(self, container: str, fmt: str) -> object | None:
        """按格式取一段 JSON 并解析；失败或无法解析返回 ``None``。"""
        text = self.inspect_text(container, fmt)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def logs(self, container: str, *, tail: int = 40) -> str:
        """``docker logs --tail <n>``；容器不存在时返回空串。"""
        done = self._run(["logs", "--tail", str(tail), container], timeout=120.0)
        if done.returncode != 0:
            return ""
        return done.stdout or ""

    def stats(self, container: str, fmt: str) -> str:
        """``docker stats --no-stream --format <fmt>``；失败返回空串。"""
        done = self._run(["stats", "--no-stream", "--format", fmt, container], timeout=120.0)
        if done.returncode != 0:
            return ""
        return (done.stdout or "").strip()

    def published_port(self, container: str, port: int) -> int | None:
        """``docker port <container> <port>`` → 宿主机端口号；没发布返回 ``None``。

        输出形如 ``127.0.0.1:49153``（IPv6 是 ``[::1]:49153``），取最后一段。
        """
        done = self._run(["port", container, f"{port}/tcp"], timeout=60.0)
        if done.returncode != 0:
            return None
        text = (done.stdout or "").strip().splitlines()
        if not text:
            return None
        candidate = text[0].strip().rsplit(":", 1)[-1].strip()
        return int(candidate) if candidate.isdigit() else None

    def remove_container(self, container: str) -> subprocess.CompletedProcess:
        """``docker rm -f``（清理用；容器不存在时非 0，调用方忽略）。"""
        return self._run(["rm", "-f", container], timeout=120.0)
