"""``internal/platform/builtin`` 的 Python 移植：把配置变成平台适配器集合。

对应 Go：``internal/platform/builtin/builtin.go``（87 行生产代码）。

职责：按运行模式决定**主平台**（``full``/``cli`` → CLI 适配器；``service`` →
headless），再把配置里启用的 qqofficial / qqonebot / telegram 适配器追加进
``Runtimes``。分支顺序与判定条件**逐字**对齐 Go：

1. ``mode == ""`` → ``ModeFull``；
2. ``service`` → ``Primary = headless``；否则 ``Primary = cli.New()`` 且它同时进
   ``Runtimes``；
3. ``cfg is not None and mode == service`` → 读 ``[platform.cli]``，``enabled`` 且
   ``server.enabled`` 时再追加 ``cli.NewRemoteServer(...)``；
4. ``cfg is None or mode == cli`` → 提前返回（**不**装配任何后台平台）；
5. ``qqofficial`` → ``qqonebot`` → ``telegram``，每个都「先构造、构造失败即抛、
   再按 ``Enabled()`` 过滤」。

并行移植期的两条硬要求（Go 侧不需要，Python 侧必须有）：

* **函数内延迟导入 + 鸭子类型** —— 本模块顶层绝不 import
  ``atri_x.platform.cli`` / ``qq_onebot`` / ``qqofficial``；它们在
  :func:`new` 被调用时才按名字解析（``importlib``），因此某个平台包还没交付时
  ``import atri_x.platform.builtin`` 仍然成功。
* **缺件跳过而不是整体炸** —— 某个平台包缺失、或它没有提供所需的装配入口时，
  该平台被跳过并记录到 :attr:`Bundle.skipped`，其余平台照常装配。真实配置错误
  （模块在、装配函数抛异常）仍然抛出，与 Go 一致。

与 Go 的差异（形式）：

* ``New`` 的 ``(Bundle, error)`` → 抛 :class:`atri_x.errors.InvalidError`。
* ``Bundle`` 字段按 Python 约定 snake_case（``primary`` / ``runtimes``），并多出
  **Python 版新增**的 ``skipped`` 诊断字段。
* Go 的 ``filepath.Dir`` 对空路径返回 ``"."``，Python 的 ``os.path.dirname``
  返回 ``""``；:func:`_config_dir` 复刻 Go 的语义。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..errors import InvalidError

__all__ = [
    "Mode",
    "MODE_FULL",
    "MODE_CLI_ONLY",
    "MODE_SERVICE",
    "ModeFull",
    "ModeCLIOnly",
    "ModeService",
    "Options",
    "Bundle",
    "New",
    "new",
    "PLATFORM_MODULE_NAMES",
    "platform_module_names",
]


class Mode(str):
    """Go ``Mode``：``str`` 子类，便于直接当字符串比较/打印。"""

    __slots__ = ()

    def __new__(cls, value: str = "") -> "Mode":
        return super().__new__(cls, value)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return f"Mode({str(self)!r})"


#: 与 Go 常量逐字对应。
MODE_FULL = Mode("full")
MODE_CLI_ONLY = Mode("cli")
MODE_SERVICE = Mode("service")

#: Go 原名（``ModeFull`` 等），与上面的常量指向同一对象。
ModeFull = MODE_FULL
ModeCLIOnly = MODE_CLI_ONLY
ModeService = MODE_SERVICE

#: 后台平台的装配入口所在的模块名，按 Go 的装配顺序排列。
PLATFORM_MODULE_NAMES: tuple[str, ...] = (
    "atri_x.platform.qqofficial",
    "atri_x.platform.qq_onebot",
    "atri_x.platform.telegram",
)


@dataclass
class Options:
    """对应 Go ``Options``。

    Go 的零值是 ``Mode("")``，由 :func:`new` 归一成 ``ModeFull``；这里默认就给
    ``ModeFull``（经 :func:`new` 的效果完全一致）。
    """

    mode: Mode = MODE_FULL


@dataclass
class Bundle:
    """由应用配置造出的平台适配器集合。对应 Go ``Bundle``。

    ``skipped`` 是 **Python 版新增**的诊断字段：记录因「模块缺失 / 未提供装配
    入口」而被跳过的平台，便于集成期发现"静默少了一个平台"。Go 侧没有这个字段
    （它在编译期就不会缺件）。
    """

    primary: Any = None
    runtimes: list[Any] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def new(
    opts: Options,
    cfg: Any,
    store: Any,
    chat_history: Any,
    logger: Any,
) -> Bundle:
    """对应 Go ``New``。

    参数（与 Go 逐字对应）：

    ``opts``
        :class:`Options`，只用到 ``mode``。
    ``cfg``
        :class:`atri_x.config.AppConfig`（Go ``*config.Config``）；``None`` 表示
        只造主平台（Go 里 nil 配置同样走这条分支）。
    ``store``
        :class:`atri_x.storage.Store`，透传给各平台。
    ``chat_history``
        Chat history 仓储，透传给各平台。
    ``logger``
        ``atri_x.logging`` 的 Logger（Go ``*slog.Logger``），透传给各平台；
        允许 ``None``（Go 测试就是这么传的）。
    """
    mode = opts.mode if opts is not None else MODE_FULL
    if mode == "":
        mode = MODE_FULL

    bundle = Bundle()
    headless_new, headless_reason = _resolve("atri_x.platform.headless", "New", "new")
    # Go ``cli.New()``：``atri_x.platform.cli`` 目前只导出 snake_case 的
    # ``new_adapter``（见该包 ``__init__`` 的说明），三种拼写都认。
    cli_new, cli_reason = _resolve(
        "atri_x.platform.cli", "New", "new", "new_adapter"
    )

    if mode == MODE_SERVICE:
        bundle.primary = _require(headless_new, "atri_x.platform.headless", headless_reason)()
    elif cli_new is not None:
        adapter = cli_new()
        bundle.primary = adapter
        bundle.runtimes.append(adapter)
    else:
        # 并行移植期 atri_x.platform.cli 可能尚未交付：跳过它，改用 headless 当
        # 主平台（Go 里不可能发生）。集成期请看 bundle.skipped。
        bundle.primary = _require(headless_new, "atri_x.platform.headless", headless_reason)()
        _skip(bundle, logger, "cli", cli_reason)

    if cfg is not None and mode == MODE_SERVICE:
        adapter = _new_cli_remote_server(cfg, bundle, logger)
        if adapter is not None:
            bundle.runtimes.append(adapter)

    if cfg is None or mode == MODE_CLI_ONLY:
        return bundle

    _append_qqofficial(cfg, store, chat_history, logger, bundle)
    _append_qqonebot(cfg, store, chat_history, logger, bundle)
    _append_telegram(cfg, store, chat_history, logger, bundle)
    return bundle


# ---------------------------------------------------------------------------
# 各平台分支（逐条对应 Go 里的一段）
# ---------------------------------------------------------------------------


def _new_cli_remote_server(cfg: Any, bundle: Bundle, logger: Any) -> Any:
    """``mode == service`` 且 ``[platform.cli]`` 的 server 启用时的远程服务端。

    对应 Go ``builtin`` 里 ``cli.NewConfigFromPlatformConfig`` /
    ``cli.NewRemoteServer`` 那一段。
    """
    factory, reason = _resolve(
        "atri_x.platform.cli",
        "NewConfigFromPlatformConfig",
        "new_config_from_platform_config",
    )
    if factory is None:
        _skip(bundle, logger, "cli.server", reason)
        return None
    cli_raw = _platform_section(cfg, "cli")
    # Go 对缺键的 map 取到的是 nil，装配入口按"全默认"处理。
    cli_cfg = factory(None if cli_raw is _ABSENT else cli_raw)
    if not (_attr(cli_cfg, "enabled") and _attr(_attr(cli_cfg, "server"), "enabled")):
        return None
    remote_new, remote_reason = _resolve(
        "atri_x.platform.cli", "NewRemoteServer", "new_remote_server"
    )
    options_cls, options_reason = _resolve("atri_x.platform.cli", "RemoteServerOptions")
    if remote_new is None or options_cls is None:
        _skip(bundle, logger, "cli.server", remote_reason or options_reason)
        return None
    return remote_new(
        options_cls(
            config=cli_cfg,
            config_dir=_config_dir(cfg),
            superadmins=_superadmins(cfg, "cli"),
        )
    )


def _append_qqofficial(
    cfg: Any, store: Any, chat_history: Any, logger: Any, bundle: Bundle
) -> None:
    """QQ 官方机器人。对应 Go ``qqofficial.NewFromPlatformConfig`` 那一段。"""
    raw = _platform_section(cfg, "qqofficial")
    if raw is _ABSENT:
        return
    factory, reason = _resolve(
        "atri_x.platform.qqofficial",
        "NewFromPlatformConfig",
        "new_from_platform_config",
    )
    if factory is None:
        _skip(bundle, logger, "qqofficial", reason)
        return
    adapter = factory(
        raw,
        store,
        chat_history,
        logger,
        _superadmins(cfg, "qqofficial"),
        _prefixes(cfg),
        _config_dir(cfg),
        _attachment_dir(cfg, "qqofficial"),
        _platform_files(cfg, "max_receive_file_bytes"),
        _platform_files(cfg, "download_timeout_secs"),
    )
    _append_if_enabled(bundle, adapter)


def _append_qqonebot(
    cfg: Any, store: Any, chat_history: Any, logger: Any, bundle: Bundle
) -> None:
    """QQ OneBot。对应 Go ``qqonebot.NewFromPlatformConfig`` 那一段。"""
    raw = _platform_section(cfg, "qqonebot")
    if raw is _ABSENT:
        return
    factory, reason = _resolve(
        "atri_x.platform.qq_onebot",
        "NewFromPlatformConfig",
        "new_from_platform_config",
    )
    if factory is None:
        _skip(bundle, logger, "qqonebot", reason)
        return
    adapter = factory(
        raw,
        store,
        chat_history,
        logger,
        _superadmins(cfg, "qqonebot"),
        _prefixes(cfg),
        _config_dir(cfg),
        _attachment_dir(cfg, "qqonebot"),
        _platform_files(cfg, "max_receive_file_bytes"),
        _platform_files(cfg, "download_timeout_secs"),
    )
    _append_if_enabled(bundle, adapter)


def _append_telegram(
    cfg: Any, store: Any, chat_history: Any, logger: Any, bundle: Bundle
) -> None:
    """Telegram。对应 Go ``telegram.NewFromPlatformConfig`` 那一段。

    注意 Go 的 telegram 装配入口**没有** attachmentDir / 平台文件限额两个参数。
    """
    raw = _platform_section(cfg, "telegram")
    if raw is _ABSENT:
        return
    factory, reason = _resolve(
        "atri_x.platform.telegram",
        "NewFromPlatformConfig",
        "new_from_platform_config",
    )
    if factory is None:
        _skip(bundle, logger, "telegram", reason)
        return
    adapter = factory(
        raw,
        store,
        chat_history,
        logger,
        _superadmins(cfg, "telegram"),
        _prefixes(cfg),
        _config_dir(cfg),
    )
    _append_if_enabled(bundle, adapter)


def _append_if_enabled(bundle: Bundle, adapter: Any) -> None:
    """``if adapter.Enabled() { runtimes = append(...) }``。"""
    if _enabled(adapter):
        bundle.runtimes.append(adapter)


# ---------------------------------------------------------------------------
# 延迟导入与鸭子类型助手
# ---------------------------------------------------------------------------


def _resolve(module_name: str, *names: str) -> tuple[Any, str]:
    """延迟导入 ``module_name`` 并取第一个存在的名字。

    返回 ``(入口, 跳过原因)``：成功时原因是空串，失败时入口是 ``None``、
    原因是一句可直接进 ``bundle.skipped`` 的说明。

    并行移植期某个平台包可能整体不存在（``ImportError``）、只是命名空间包
    （目录在、``__init__.py`` 还没写），或入口函数还没导出——三种都当作
    "未交付"，不抛。
    """
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 并行移植期允许缺件
        return None, f"platform module {module_name} is unavailable ({type(exc).__name__})"
    for name in names:
        value = getattr(module, name, None)
        if value is not None:
            return value, ""
    return None, f"platform module {module_name} does not export {'/'.join(names)}"


def _require(value: Any, module_name: str, reason: str) -> Any:
    if value is None:
        raise InvalidError(f"{reason} (module={module_name})")
    return value


def _skip(bundle: Bundle, logger: Any, name: str, reason: str) -> None:
    """记录被跳过的平台，并按 Go 的 slog 习惯留一条 warning。"""
    bundle.skipped.append(f"{name}: {reason}")
    emit = getattr(logger, "warn", None)
    if not callable(emit):
        emit = getattr(logger, "warning", None)
    if callable(emit):
        try:
            emit("platform skipped", platform=name, reason=reason)
        except Exception:  # noqa: BLE001 - 诊断不应影响装配
            pass


def _enabled(adapter: Any) -> bool:
    """``adapter.Enabled()`` 的鸭子类型版本（Go 导出名 / Python snake_case 都认）。"""
    for name in ("enabled", "Enabled", "is_enabled"):
        method = getattr(adapter, name, None)
        if callable(method):
            return bool(method())
    return False


def _attr(obj: Any, name: str) -> Any:
    """取字段；``obj`` 为 ``None`` 时返回 ``None``。"""
    return getattr(obj, name, None) if obj is not None else None


#: ``_platform_section`` 的"键不存在"哨兵，对应 Go 的 ``ok == false``。
#: 注意它与"键存在但值是 ``None``"不是一回事（后者 Go 会照常调用装配入口）。
_ABSENT = object()


def _platform_section(cfg: Any, name: str) -> Any:
    """``raw, ok := cfg.Platform[name]``：键不存在时返回 :data:`_ABSENT`。

    调用方一律写成 ``raw = _platform_section(...)`` / ``if raw is _ABSENT: return``，
    以逐字复刻 Go 判定的是**键存在性**而不是值是否为空。
    """
    platform = getattr(cfg, "platform", None) or {}
    if not hasattr(platform, "get"):
        return _ABSENT
    if name not in platform:
        return _ABSENT
    return platform.get(name)


def _superadmins(cfg: Any, platform_name: str) -> list[str]:
    """``cfg.Security.Superadmins[name]``（缺键时 Go 给 nil 切片）。"""
    superadmins = getattr(getattr(cfg, "security", None), "superadmins", None) or {}
    if not hasattr(superadmins, "get"):
        return []
    return list(superadmins.get(platform_name) or [])


def _prefixes(cfg: Any) -> list[str]:
    """``cfg.Commands.Prefixes``。"""
    return list(getattr(getattr(cfg, "commands", None), "prefixes", None) or [])


def _platform_files(cfg: Any, name: str) -> Any:
    """``cfg.PlatformFiles.<name>``。"""
    return getattr(getattr(cfg, "platform_files", None), name, None)


def _attachment_dir(cfg: Any, platform_name: str) -> str:
    """``filepath.Join(cfg.Sandbox.Root, "platform", <name>)``。"""
    root = str(getattr(getattr(cfg, "sandbox", None), "root", "") or "")
    return os.path.join(root, "platform", platform_name)


def _config_dir(cfg: Any) -> str:
    """``filepath.Dir(cfg.ConfigPath)`` —— 空路径时 Go 给 ``"."``。"""
    return os.path.dirname(str(getattr(cfg, "config_path", "") or "")) or "."


# Go 命名别名（PascalCase 导出名），与 snake_case 主名指向同一对象。
New = new


def platform_module_names() -> Sequence[str]:
    """本模块会尝试延迟导入的平台模块名（供集成期自查）。"""
    return PLATFORM_MODULE_NAMES
