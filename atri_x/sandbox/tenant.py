"""租户（用户安全域）标识与目录布局。

修改清单 §3 的要求：默认租户键是 **平台 + 机器人账号/实例 + scope + 用户 ID**。
只用 QQ 号做键会把「同一个人在群 A 的任务数据」带进群 B；把机器人账号算进去，
同一部署里的不同 bot 也不会共用环境。

本模块只做纯计算（不碰网络、不碰容器），因此工具层、GUI、测试都可以直接复用。
``tenant_from_context`` 需要的平台路由信息来自 ``ctx`` 里的
:class:`atri_x.platform.MessageContext`（``platform`` / ``scope_id`` / ``bot``），
身份来自 :func:`atri_x.security.actor_from_context`；两者都取不到时退化成
``local`` 域（CLI / 本地会话），保证功能可用且不会与他人混用。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from atri_x.sandbox.errors import SandboxDeniedError

__all__ = [
    "LOCAL_TENANT",
    "TenantEnvPaths",
    "TenantKey",
    "sanitize_component",
    "tenant_from_context",
    "tenant_from_ids",
]

#: 安全域各段的长度上限（防止超长 id 把目录名/容器名撑爆）。
_MAX_COMPONENT = 48
_SAFE_PATTERN = re.compile(r"[^0-9A-Za-z._-]+")
_DIGEST_LEN = 10


@dataclass(frozen=True)
class TenantKey:
    """一个用户安全域。``key`` 是稳定身份，``slug`` 是文件系统/容器名用的短名。"""

    platform: str = ""
    bot: str = ""
    scope: str = ""
    user: str = ""

    @property
    def key(self) -> str:
        """稳定且可读的身份串（审计日志用）。"""
        return "\x1f".join(
            part for part in (self.platform, self.bot, self.scope, self.user)
        )

    @property
    def display(self) -> str:
        """人类可读的短身份（GUI/错误文本用，不含任何主机字段）。"""
        parts = [part for part in (self.platform, self.scope, self.user) if part]
        return " / ".join(parts) if parts else "local"

    @property
    def slug(self) -> str:
        """目录名/容器名后缀：``platform-bot-scope-user-<digest>``（各段消毒后截断）。"""
        parts = [
            sanitize_component(self.platform, 12),
            sanitize_component(self.bot, 12),
            sanitize_component(self.scope, 16),
            sanitize_component(self.user, 16),
        ]
        readable = "-".join(part for part in parts if part) or "local"
        digest = hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
        return f"{readable}-{digest}"[:96]

    @property
    def container_name(self) -> str:
        """沙箱容器名（带 ``atrix-sb-`` 前缀，方便按标签回收）。"""
        return "atrix-sb-" + hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:24]

    @property
    def proxy_name(self) -> str:
        """出口代理容器名。"""
        return "atrix-sb-egress-" + hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:20]

    @property
    def network_name(self) -> str:
        """该租户的 Docker 内部网络名。"""
        return "atrix-sb-net-" + hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:20]


#: 没有平台上下文时的本地租户（CLI / 嵌入式 / 单元测试）。
LOCAL_TENANT = TenantKey(platform="local", bot="local", scope="local", user="local")


@dataclass(frozen=True)
class TenantEnvPaths:
    """一个租户在宿主上的全部可写路径。全部位于 ``<sandbox root>/tenants/<slug>/``。"""

    root: str = ""
    workspace: str = ""
    home: str = ""
    tmp: str = ""
    proxy_state: str = ""
    logs: str = ""

    def dump(self) -> dict:
        """审计/GUI 用字典（不暴露宿主绝对路径给模型）。"""
        return {
            "workspace": self.workspace,
            "home": self.home,
            "proxy_state": self.proxy_state,
        }

    @staticmethod
    def for_root(root: str, tenant: TenantKey) -> "TenantEnvPaths":
        """按沙箱根与租户键推导目录。"""
        base = root
        tenant_root = _join(base, "tenants", tenant.slug)
        return TenantEnvPaths(
            root=tenant_root,
            workspace=_join(tenant_root, "workspace"),
            home=_join(tenant_root, "home"),
            tmp=_join(tenant_root, "tmp"),
            proxy_state=_join(tenant_root, "proxy"),
            logs=_join(tenant_root, "logs"),
        )


def sanitize_component(value: str, limit: int = _MAX_COMPONENT) -> str:
    """把任意 id 变成目录/容器名安全的短段（保留可读性，非法字符折叠成 ``-``）。"""
    text = (value or "").strip()
    if not text:
        return ""
    text = _SAFE_PATTERN.sub("-", text).strip("-.")
    if not text:
        return ""
    return text[:limit]


def tenant_from_ids(
    platform: str = "",
    bot: str = "",
    scope: str = "",
    user: str = "",
) -> TenantKey:
    """用显式四元组构造租户；缺省段落到 ``local``，空串一律归一成 ``local``。"""
    return TenantKey(
        platform=(platform or "").strip() or "local",
        bot=(bot or "").strip() or "local",
        scope=(scope or "").strip() or "local",
        user=(user or "").strip() or "local",
    )


def tenant_from_context(ctx: Any) -> TenantKey:
    """从工具 ctx 推导租户（纯计算，异常一律退化为本地域）。

    取不到的字段不抛错：CLI、cron、测试等没有平台路由的路径也要能执行任务，
    退化为 ``local`` 域意味着「不会和其他人共享」，只是这个域本身是单一的。
    """
    try:
        from atri_x.platform import message_context_from

        msg, ok = message_context_from(ctx)
    except Exception:  # noqa: BLE001 - 平台层未装配时退化为本地域
        msg, ok = None, False
    platform = ""
    scope = ""
    bot = ""
    user = ""
    if ok and msg is not None:
        platform = str(getattr(msg, "platform", "") or "")
        scope = str(getattr(msg, "scope_id", "") or "")
        identity = getattr(msg, "bot", None)
        bot = str(getattr(identity, "user_id", "") or "")
        user = str(getattr(msg, "platform_user_id", "") or "")
    try:
        from atri_x.security import actor_from_context

        actor, found = actor_from_context(ctx)
        if found:
            user = user or str(getattr(actor, "platform_user_id", "") or "")
            bot = bot or str(getattr(actor, "platform", "") or "")
    except Exception:  # noqa: BLE001 - 没有 actor 就用消息里的
        pass
    if not user:
        user = scope or "anonymous"
    if not scope:
        scope = platform or "local"
    return tenant_from_ids(platform, bot, scope, user)


def _join(root: str, *parts: str) -> str:
    """用 ``os.path.join`` 拼路径但避免引入模块级依赖（租户模块保持轻量）。"""
    import os

    return os.path.join(root, *parts)


def require_bot_name(name: str) -> str:
    """校验容器/网络名（容器引擎对名字有字符集限制）。"""
    text = (name or "").strip()
    if not text or len(text) > 120 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text):
        raise SandboxDeniedError(f"非法的沙箱资源名：{name!r}")
    return text
