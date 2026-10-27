"""qqonebot 协议端（后端）差异描述。

任务背景：ATRI-X 1.0.x 只接过 NapCat，1.1 起要同时支持 **SnowLuma**。两者都是
标准 OneBot v11 正向 WebSocket 服务端，所以 **平台层不动**：

* ``qqonebot`` 是**平台**（:data:`atri_x.platform.qq_onebot.config.PLATFORM_NAME`），
  权限作用域键是 ``qqonebot:...``；协议端（NapCat / SnowLuma / 将来的其它实现）
  只是同一平台内的一条**实现策略**。因此后端名**不参与**平台名、作用域键、唤醒词、
  令牌与 ``ws_url`` 的解析——换后端不改变平台身份，也不隐式换端口。
* 本模块只放"两端确实有差异"的纯数据（:class:`BackendProfile`）和取值校验，
  **零重依赖**（只 import :mod:`atri_x.errors`），GUI、装配层、冒烟工具都可以安全 import；
  不要在这里 import 网络/GUI 模块。

``protocol_backend`` 三态：

* 键缺失（``""``）：没有人显式选过后端，由 :func:`resolve_protocol_backend`
  解析成 :data:`LEGACY_DEFAULT_BACKEND`——**这是"未配置"兜底的唯一位置**；
* 显式 ``"napcat"`` / ``"snowluma"``：用户/发行包写入的显式选择，非法值抛
  :class:`atri_x.errors.InvalidError`，绝不静默回退。

**默认协议端是 NapCat**（内置随包分发，开箱即用）。SnowLuma 是**可选的备用协议端**
（外置接入，发行包不内置，选择它需要用户在「平台」页显式切过去）。

**新装默认**（:data:`NEW_INSTALL_BACKEND`）不由配置解码层猜——解码层看不到"这份配置
是几时生成的"。它只由"写配置的一方"表达，且判据是**整个 ``[platform.qqonebot]``
小节在不在**：小节不存在 = 新装（默认 NapCat）；小节存在但键缺失 = 1.0.x 老配置
（同样是 NapCat）。GUI 平台页与 ``proto_manager`` 用同一条规则
（:func:`atri_x.gui.tabs.platform._display_backend` /
:func:`atri_x.gui.proto_manager.configured_backend`）。发行包**内置 NapCat** 与
:data:`NEW_INSTALL_BACKEND` 一致：默认值就是"包里自带、能直接用"的那一套，SnowLuma
只有用户显式选择才会生效。详见 ``devdocs/qq-backends-matrix.md``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.errors import InvalidError

#: NapCat（内置/外置都算）。
PROTOCOL_BACKEND_NAPCAT = "napcat"
#: SnowLuma。
PROTOCOL_BACKEND_SNOWLUMA = "snowluma"

#: 合法取值集合（顺序即 GUI 里的展示顺序：默认推荐在前）。
PROTOCOL_BACKENDS: tuple[str, ...] = (PROTOCOL_BACKEND_NAPCAT, PROTOCOL_BACKEND_SNOWLUMA)

#: 键缺失时的解析结果：**未配置 = NapCat**（默认协议端；升级不自动切换）。
LEGACY_DEFAULT_BACKEND = PROTOCOL_BACKEND_NAPCAT

#: 报错文案里的排列（默认在前：napcat 是默认项）。
LEGACY_FIRST_BACKENDS: tuple[str, ...] = (
    LEGACY_DEFAULT_BACKEND,
    *[name for name in PROTOCOL_BACKENDS if name != LEGACY_DEFAULT_BACKEND],
)

#: 全新安装（没有历史配置可继承）时的默认协议端：**随包内置的 NapCat**。
#: SnowLuma 是可选的备用方案，只有用户显式选择才会写入配置。
NEW_INSTALL_BACKEND = PROTOCOL_BACKEND_NAPCAT

#: 后端 → 显示名。GUI 文案一律走这里，不要在各处写死 "NapCat"。
BACKEND_LABELS: dict[str, str] = {
    PROTOCOL_BACKEND_SNOWLUMA: "SnowLuma",
    PROTOCOL_BACKEND_NAPCAT: "NapCat",
}


@dataclass(frozen=True)
class BackendProfile:
    """一个协议端的差异描述。只描述**两端确实不同**的点。

    字段语义（供批次②的协议/媒体适配消费）：

    * ``forward_id_param``：``get_forward_msg`` 的主参数键。SnowLuma 的合并转发 id
      是 res_id 字符串，优先 ``id``；NapCat 传 ``message_id``。
    * ``group_file_url_action`` / ``private_file_url_action``：文件下载的专有动作；
      空串表示该后端没有，只能回落到 ``get_file``。
    * ``get_file_supports_file``：``get_file`` 对**群/私聊文件**是否可用。SnowLuma
      只覆盖图片/语音缓存，群/私聊文件必须走专有动作，否则稳定失败。
    * ``private_file_url_requires_hash``：私聊文件下载是否依赖入站 ``file`` 段的
      ``file_hash``（SnowLuma 的 ``file_id`` 是裸 fileUUID）。
    * ``upload_group_action`` / ``upload_private_action``：上传动作（返回 ``file_id``，
      **不返回 message_id**，与"发消息"是两回事）。

    端口**不在这里**：两边默认端口会随部署形态变（内置包写的是 3001），而且
    ``ws_url`` 永远来自用户配置，后端 profile 不参与地址推导。
    """

    name: str
    label: str
    forward_id_param: str
    group_file_url_action: str
    private_file_url_action: str
    get_file_supports_file: bool
    private_file_url_requires_hash: bool
    upload_group_action: str
    upload_private_action: str


_PROFILES: dict[str, BackendProfile] = {
    PROTOCOL_BACKEND_SNOWLUMA: BackendProfile(
        name=PROTOCOL_BACKEND_SNOWLUMA,
        label=BACKEND_LABELS[PROTOCOL_BACKEND_SNOWLUMA],
        forward_id_param="id",
        group_file_url_action="get_group_file_url",
        private_file_url_action="get_private_file_url",
        get_file_supports_file=False,
        private_file_url_requires_hash=True,
        upload_group_action="upload_group_file",
        upload_private_action="upload_private_file",
    ),
    PROTOCOL_BACKEND_NAPCAT: BackendProfile(
        name=PROTOCOL_BACKEND_NAPCAT,
        label=BACKEND_LABELS[PROTOCOL_BACKEND_NAPCAT],
        forward_id_param="message_id",
        group_file_url_action="get_group_file_url",
        private_file_url_action="get_private_file_url",
        get_file_supports_file=True,
        private_file_url_requires_hash=False,
        upload_group_action="upload_group_file",
        upload_private_action="upload_private_file",
    ),
}


def normalize_protocol_backend(raw: Any = "") -> str:
    """归一化取值：``None`` → ``""``，字符串 strip + lower；非字符串直接报错。

    类型错误必须显式报错：``decode_config`` 不做类型转换，
    ``protocol_backend = 123`` 会原样落到配置对象上，参与比较后行为未定义。
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise InvalidError(
            "qqonebot protocol_backend must be a string, got "
            f"{type(raw).__name__}: {raw!r}"
        )
    return raw.strip().lower()


def validate_protocol_backend(raw: Any = "") -> None:
    """校验取值；空串表示"未配置"（按旧配置处理），不算非法。"""
    name = normalize_protocol_backend(raw)
    if name == "" or name in PROTOCOL_BACKENDS:
        return
    allowed = " or ".join(repr(item) for item in LEGACY_FIRST_BACKENDS)
    raise InvalidError(
        f"qqonebot protocol_backend must be {allowed} (or empty = legacy default), "
        f"got {name!r}"
    )


def resolve_protocol_backend(raw: Any = "") -> str:
    """把配置值解析成生效后端：``""`` → :data:`LEGACY_DEFAULT_BACKEND`。

    非法值抛 :class:`atri_x.errors.InvalidError`（不静默回退）。
    """
    validate_protocol_backend(raw)
    return normalize_protocol_backend(raw) or LEGACY_DEFAULT_BACKEND


def backend_label(name: Any = "") -> str:
    """后端显示名；空值按生效后端显示，未知值原样回显（只做展示，不在这里报错）。"""
    text = normalize_protocol_backend(name)
    if text == "":
        text = LEGACY_DEFAULT_BACKEND
    return BACKEND_LABELS.get(text, text)


def backend_choices() -> tuple[tuple[str, str], ...]:
    """GUI 下拉选项：``(取值, 显示名)``，顺序与 :data:`PROTOCOL_BACKENDS` 一致。"""
    return tuple((name, BACKEND_LABELS[name]) for name in PROTOCOL_BACKENDS)


def backend_profile(name: Any = "") -> BackendProfile:
    """取差异表；非法取值抛 :class:`atri_x.errors.InvalidError`。"""
    return _PROFILES[resolve_protocol_backend(name)]


def detect_backend(app_name: str = "") -> str:
    """按 ``get_version_info`` 的 ``app_name`` 认出实际后端；认不出返回 ``""``。

    用于"配置的后端 vs 连上的后端"不一致告警（配置写 ``napcat``、实际连上
    SnowLuma 时应当能发现）。只做子串匹配，不抛异常。
    """
    text = str(app_name or "").strip().lower()
    if text == "":
        return ""
    for name in PROTOCOL_BACKENDS:
        if name in text:
            return name
    return ""


__all__ = [
    "BACKEND_LABELS",
    "BackendProfile",
    "LEGACY_DEFAULT_BACKEND",
    "LEGACY_FIRST_BACKENDS",
    "NEW_INSTALL_BACKEND",
    "PROTOCOL_BACKENDS",
    "PROTOCOL_BACKEND_NAPCAT",
    "PROTOCOL_BACKEND_SNOWLUMA",
    "backend_choices",
    "backend_label",
    "backend_profile",
    "detect_backend",
    "normalize_protocol_backend",
    "resolve_protocol_backend",
    "validate_protocol_backend",
]
