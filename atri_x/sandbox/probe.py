"""隔离能力探测脚本与判定。

探测容器用与租户容器相同的安全基线启动（只读根、drop 全部 capability、
no-new-privileges、默认 seccomp、内存/pids 硬顶、``--network none``、非 root），
然后从**容器内部**读取内核视角的事实：

* ``id -u`` 不是 0；
* ``CapEff`` 全 0、``NoNewPrivs=1``、``Seccomp=2``；
* 根文件系统写不进去，``/workspace``（bind mount）可写；
* ``memory.max`` / ``pids.max`` 与请求值一致；
* 直接连 ``1.1.1.1:443`` 失败（``--network none``）；
* 没有 docker socket。

「配置已开启」不算数：只有这些实测项全过，:class:`CapabilityReport.ok` 才为真，
上层才允许执行沙箱命令。
"""

from __future__ import annotations

import os

from atri_x.sandbox.backend import CapabilityCheck
from atri_x.sandbox.container_cli import EngineCommandResult

__all__ = ["PROBE_SCRIPT", "parse_probe_output", "probe_checks", "within_memory_limit"]

#: 探测容器里执行的脚本（``/bin/sh``，只依赖基础镜像自带的 coreutils + python3）。
PROBE_SCRIPT = r"""
set -u
echo "UID=$(id -u)"
echo "CAPEFF=$(grep -E '^CapEff:' /proc/self/status | awk '{print $2}')"
echo "NONEW=$(grep -E '^NoNewPrivs:' /proc/self/status | awk '{print $2}')"
echo "SECCOMP=$(grep -E '^Seccomp:' /proc/self/status | awk '{print $2}')"
echo "MEM=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo na)"
echo "PIDS=$(cat /sys/fs/cgroup/pids.max 2>/dev/null || echo na)"
if touch /opt/.atri_probe 2>/dev/null; then echo "ROOTWRITE=yes"; else echo "ROOTWRITE=no"; fi
if test -S /var/run/docker.sock; then echo "DOCKERSOCK=yes"; else echo "DOCKERSOCK=no"; fi
if touch /workspace/.atri_probe 2>/dev/null; then echo "WORKSPACE=ok"; rm -f /workspace/.atri_probe; else echo "WORKSPACE=fail"; fi
python3 - <<'PY'
import socket
s = socket.socket(); s.settimeout(2)
try:
    s.connect(("1.1.1.1", 443)); print("NET=open")
except OSError:
    print("NET=blocked")
PY
"""


def parse_probe_output(text: str) -> dict[str, str]:
    """把 ``KEY=value`` 行解析成字典。"""
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key:
            values[key] = value.strip()
    return values


def within_memory_limit(raw: str, mib: int) -> bool:
    """cgroup 内存限额是否不超过 ``mib``（``na`` / cgroup v1 视为失败）。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return False
    return 0 < value <= mib * 1024 * 1024 + 2 * 1024 * 1024


def probe_checks(
    values: dict[str, str],
    result: EngineCommandResult,
    probe_dir: str,
    *,
    uid: int = 1000,
    memory_mib: int = 256,
    pids: int = 64,
) -> list[CapabilityCheck]:
    """把探测输出翻译成能力检查项（必需项失败 = 沙箱不可用）。"""
    engine_ok = result.exit_code == 0 and bool(values)
    workspace_ok = values.get("WORKSPACE", "") == "ok" and os.path.isdir(probe_dir)
    return [
        CapabilityCheck("probe_container", engine_ok, result.error_text() if not engine_ok else "ok"),
        CapabilityCheck("non_root", values.get("UID", "") == str(uid), f"uid={values.get('UID', '?')}"),
        CapabilityCheck(
            "capabilities_dropped",
            values.get("CAPEFF", "") == "0000000000000000",
            values.get("CAPEFF", "?"),
        ),
        CapabilityCheck("no_new_privs", values.get("NONEW", "") == "1", values.get("NONEW", "?")),
        CapabilityCheck("seccomp", values.get("SECCOMP", "") == "2", values.get("SECCOMP", "?")),
        CapabilityCheck("rootfs_read_only", values.get("ROOTWRITE", "") == "no", values.get("ROOTWRITE", "?")),
        CapabilityCheck("network_isolated", values.get("NET", "") == "blocked", values.get("NET", "?")),
        CapabilityCheck("memory_limited", within_memory_limit(values.get("MEM", ""), memory_mib), values.get("MEM", "?")),
        CapabilityCheck("pids_limited", values.get("PIDS", "") == str(pids), values.get("PIDS", "?")),
        CapabilityCheck("no_docker_socket", values.get("DOCKERSOCK", "") == "no", values.get("DOCKERSOCK", "?")),
        CapabilityCheck("workspace_writable", workspace_ok, values.get("WORKSPACE", "?")),
    ]
