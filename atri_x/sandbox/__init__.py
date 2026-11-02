"""沙箱执行环境（用户安全域）：默认联网、资源有界、可重置的独立执行面。

对外入口按用途分三层：

* **工具层**通过 :mod:`atri_x.sandbox.runtime` 取当前服务，不直接构造后端；
* **装配层**用 :func:`atri_x.sandbox.runtime.build_service` 按 ``[sandbox]`` 建服务；
* **运维**用 ``python -m atri_x.sandbox``（status/list/reset/probe）与
  ``python -m atri_x.sandbox.doctor``（安装/验收自检）。

设计边界见《ATRI-X2 沙箱功能修改清单》：盒内允许折腾，盒外控制权限；联网默认
能用，资源默认有界；环境坏了重建，任务多了排队。容器仍有共享内核等残余风险，
不承诺零逃逸。
"""

from __future__ import annotations

from atri_x.sandbox.audit import SandboxAudit, redact_text
from atri_x.sandbox.backend import (
    BackendInfo,
    CapabilityCheck,
    CapabilityReport,
    EnvInfo,
    ExecResult,
    SandboxBackend,
)
from atri_x.sandbox.errors import (
    SandboxBackendError,
    SandboxDeniedError,
    SandboxError,
    SandboxQuotaError,
    SandboxQueueTimeoutError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from atri_x.sandbox.files import (
    export_file,
    import_file,
    prune_tree,
    resolve_workspace_path,
)
from atri_x.sandbox.ledger import UsageLedger
from atri_x.sandbox.manager import SandboxService, ShellOutcome
from atri_x.sandbox.network import EgressSpec
from atri_x.sandbox.profiles import (
    NETWORK_PROFILES,
    PROFILES,
    NetworkProfile,
    ResourceProfile,
    network_profile,
    resource_profile,
)
from atri_x.sandbox.queue import AdmissionController, Lease
from atri_x.sandbox.runtime import (
    build_service,
    clear_service,
    get_service,
    install,
    set_service,
)
from atri_x.sandbox.settings import SandboxSettings, from_config
from atri_x.sandbox.tenant import TenantEnvPaths, TenantKey, tenant_from_context

__all__ = [name for name in dir() if not name.startswith("_")]
