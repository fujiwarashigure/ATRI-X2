"""桌面端「模型」页的数据模型：LLM Provider、上下文窗口与模型分配。

三件事：

1. **Provider**（``services.toml`` 的 ``[providers.<name>]``，旧部署退回
   ``providers.toml``）：``base_url`` / ``api_key_env`` / ``models`` / ``api_mode`` /
   ``extra_headers``（网关要求的自定义请求头，如 OpenCode Go 的
   ``x-opencode-session``）/ ``reasoning_effort``（推理思考强度：none / minimal /
   low / medium / high，空串 = 不发送）。密钥值不写 TOML，只写配置根 ``.env``；
   这里只负责"读状态 + 掩码展示"。
2. **上下文窗口**（``services.toml`` 的 ``[model_metadata].default_context_window``
   与 ``[providers.<name>.model_configs.<model>].context_window``）：chat / work
   按 provider+model 解析窗口时的第 3、4 级来源（provider 上报的元数据优先），
   文件里都不写时后台服务退回内置 256000。写盘入口见
   :func:`save_default_context_window` / :func:`save_model_context_window`。
3. **模型分配**（``state.toml`` 的 ``[mode_models.*]`` / ``[naming_model]`` /
   ``[compact_model]`` / ``[session].default_mode``）：运行期回写的文件，后台服务
   会热加载，保存后几秒生效。

**写盘保证**（1.0.2 起）：两个保存入口都是「先全量校验 → 再落盘 → 最后回读逐字段比对」，
不一致时抛 :class:`ConfigEditError`。这样"界面改了、文件没变"会立刻变成可见错误，而不是
静默留下一份和界面不一致的配置。

读写原语在 :mod:`atri_x.gui.configedit`（行级补丁 + 备份 + 校验）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.config.services import normalize_reasoning_effort
from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    patch_section,
    read_env,
    read_toml,
    remove_section,
)

__all__ = [
    "BUILTIN_CONTEXT_WINDOW",
    "ContextWindows",
    "KeyState",
    "MODEL_SLOTS",
    "ModelSlot",
    "ProviderView",
    "REQUIRED_MODEL_SLOTS",
    "delete_provider",
    "load_context_windows",
    "load_model_selection",
    "load_providers",
    "mask_secret",
    "parse_context_window",
    "provider_key_state",
    "remove_model_context_window",
    "resolve_fetch_key",
    "save_default_context_window",
    "save_model_context_window",
    "save_model_selection",
    "save_provider",
    "secret_state",
    "secret_value",
    "validate_provider_name",
]

#: Provider 名字允许的字符（同时要能直接写进 ``[providers.<name>]``）。
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: provider 所在小节前缀。
_PROVIDERS_SECTION = "providers"

#: 上下文窗口（tokens）允许的范围：太小没有意义，太大一定是手滑。
MIN_CONTEXT_WINDOW = 1_000
MAX_CONTEXT_WINDOW = 32_000_000

#: 文件里没配时后台服务用的内置兜底窗口。
#: 与 :data:`atri_x.contextmgr.budget.FALLBACK_CONTEXT_WINDOW` 保持一致（界面只用于提示）。
BUILTIN_CONTEXT_WINDOW = 256_000


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
@dataclass
class ProviderView:
    """``[providers.<name>]`` 在界面上需要编辑的字段。"""

    name: str
    base_url: str = ""
    api_key_env: str = ""
    models: list[str] = field(default_factory=list)
    api_mode: str = ""
    #: 推理思考强度（写 ``[providers.<name>].reasoning_effort``；空串 = 不发送）。
    #: 取值集合见 :data:`atri_x.config.services.REASONING_EFFORT_VALUES`。
    reasoning_effort: str = ""
    #: 自定义请求头（写 ``[providers.<name>.extra_headers]`` 子表）。
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyState:
    """某个 provider 密钥的配置状态（不回显明文）。"""

    name: str = ""
    in_process_env: bool = False
    in_dotenv: bool = False
    masked: str = ""

    @property
    def configured(self) -> bool:
        return self.in_process_env or self.in_dotenv

    def describe(self) -> str:
        if not self.name:
            return "未配置 api_key_env"
        suffix = f"（{self.masked}）" if self.masked else ""
        if self.in_process_env:
            return f"已设置：来自当前进程环境变量{suffix}"
        if self.in_dotenv:
            return f"已设置：来自 .env{suffix}"
        return "未设置（在下方填密钥，保存时写入 .env）"


def validate_provider_name(name: str) -> str:
    """校验 provider 名字；返回去空白后的名字，非法时抛 :class:`ConfigEditError`。"""
    value = (name or "").strip()
    if not _PROVIDER_NAME.match(value):
        raise ConfigEditError(
            "provider 名字只能用字母/数字/下划线/短横线/点，且以字母或数字开头（最长 64）"
        )
    return value


def load_providers(paths: ConfigPaths) -> dict[str, ProviderView]:
    """读 provider 列表（``services.toml`` 优先，退回旧的 ``providers.toml``）。"""
    for candidate in (paths.services_path, os.path.join(paths.config_dir, "providers.toml")):
        if not candidate or not os.path.exists(candidate):
            continue
        data = read_toml(candidate)
        providers = data.get(_PROVIDERS_SECTION)
        if isinstance(providers, Mapping) and providers:
            return _providers_from(providers)
    return {}


def _providers_from(raw: Mapping[str, Any]) -> dict[str, ProviderView]:
    result: dict[str, ProviderView] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping):
            continue
        models = entry.get("models")
        model_list = [str(item) for item in models] if isinstance(models, (list, tuple)) else []
        headers = entry.get("extra_headers")
        header_map: dict[str, str] = {}
        if isinstance(headers, Mapping):
            for key, value in headers.items():
                header_map[str(key)] = "" if value is None else str(value)
        result[str(name)] = ProviderView(
            name=str(name),
            base_url=_text(entry.get("base_url")),
            api_key_env=_text(entry.get("api_key_env")),
            models=model_list,
            api_mode=_text(entry.get("api_mode")),
            reasoning_effort=_text(entry.get("reasoning_effort")),
            extra_headers=header_map,
        )
    return result


def provider_key_state(paths: ConfigPaths, view: ProviderView) -> KeyState:
    """算某个 provider 的密钥状态（进程环境变量优先，其次 ``.env``）。"""
    return secret_state(paths, view.api_key_env)


def secret_state(paths: ConfigPaths, env_name: str) -> KeyState:
    """算任意一个"环境变量名 → 密钥"的状态（平台 token、client secret 也用）。"""
    name = (env_name or "").strip()
    if not name:
        return KeyState()
    in_process = bool((os.environ.get(name) or "").strip())
    try:
        dotenv = read_env(paths.env_path)
    except ConfigEditError:
        dotenv = {}
    plain = (os.environ.get(name) or "").strip() or (dotenv.get(name) or "").strip()
    return KeyState(
        name=name,
        in_process_env=in_process,
        in_dotenv=bool((dotenv.get(name) or "").strip()),
        masked=mask_secret(plain),
    )


def secret_value(paths: ConfigPaths, env_name: str) -> str:
    """取一个环境变量名的**明文**值（进程环境优先，其次 ``.env``）。

    只给「获取可用模型目录 / 下载头像」这类本机即时调用用：它们要真的带上密钥去请求
    上游，所以不能只拿掩码。界面上仍然不回显明文。
    """
    name = (env_name or "").strip()
    if not name:
        return ""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        dotenv = read_env(paths.env_path)
    except ConfigEditError:
        return ""
    return (dotenv.get(name) or "").strip()


def resolve_fetch_key(typed: str, paths: ConfigPaths, env_name: str) -> tuple[str, str]:
    """「获取可用模型」这类即时请求该用哪个密钥。

    界面输入框里刚填、**还没保存**的密钥优先：用户填完直接点「获取可用模型」
    是最常见的路径，可这条路径以前只读 ``.env`` / 进程环境，于是刚填的密钥
    根本没被用上，上游只看到一次匿名请求（401）——提示又把原因说成"密钥不对"。
    其次才回落到 :func:`secret_value`（进程环境 → ``.env``）。

    返回 ``(明文密钥, 来源说明)``；来源是 ``"输入框（尚未保存）"`` /
    ``"环境变量或 .env"`` / ``""``（三处都没有）。
    """
    text = (typed or "").strip()
    if text:
        return text, "输入框（尚未保存）"
    value = secret_value(paths, env_name)
    if value:
        return value, "环境变量或 .env"
    return "", ""


def save_provider(paths: ConfigPaths, view: ProviderView, *, original: str = "") -> str:
    """保存一个 provider，返回实际写入的文件路径。

    ``original`` 非空且与新名字不同表示**改名**：先删旧小节再写新的。
    ``extra_headers`` 写在 ``[providers.<name>.extra_headers]`` 子表里；空表表示删掉
    整个子表（不留下一个空小节）。

    写盘后会**从文件回读并逐字段比对**：界面上的值没有真正落进 ``services.toml``
    时抛 :class:`ConfigEditError`，而不是留一个"界面看着改了、实际没生效"的状态。
    """
    name = validate_provider_name(view.name)
    target = _providers_path(paths)
    effort, ok = normalize_reasoning_effort(view.reasoning_effort)
    if not ok:
        raise ConfigEditError(
            "推理思考强度只能是 none / minimal / low / medium / high 或留空（不设置），"
            f"当前是 {view.reasoning_effort!r}"
        )
    values: dict[str, Any] = {
        "base_url": view.base_url.strip() or None,
        "api_key_env": view.api_key_env.strip() or None,
        "models": list(view.models) or None,
        "api_mode": view.api_mode.strip() or None,
        "reasoning_effort": effort or None,
    }
    old = (original or "").strip()
    if old and old != name:
        remove_section(target, f"{_PROVIDERS_SECTION}.{old}")
    patch_section(
        target,
        f"{_PROVIDERS_SECTION}.{name}",
        values,
        header_comment=f"桌面端添加的 LLM Provider：{name}",
    )
    _save_extra_headers(target, name, view.extra_headers)
    # 用「实际写进文件的值」做回读基准（例如 thinking 值会被归一化成小写），
    # 不能用界面上的原始输入去比，否则归一化本身会被误报成"没落盘"。
    written = ProviderView(
        name=name,
        base_url=view.base_url.strip(),
        api_key_env=view.api_key_env.strip(),
        models=[str(item).strip() for item in view.models],
        api_mode=view.api_mode.strip(),
        reasoning_effort=effort,
        extra_headers={
            str(key).strip(): str(value).strip()
            for key, value in view.extra_headers.items()
        },
    )
    _verify_provider_saved(paths, written)
    return target


def _verify_provider_saved(paths: ConfigPaths, expected: ProviderView) -> None:
    """回读 ``services.toml`` 校验界面值真的落盘；不一致时抛 :class:`ConfigEditError`。"""
    saved = load_providers(paths).get(expected.name)
    if saved is None:
        raise ConfigEditError(
            f"写入后回读不到 [providers.{expected.name}]：配置没有落到 "
            f"{os.path.basename(paths.services_path)}"
        )
    pairs = (
        ("base_url", expected.base_url, saved.base_url),
        ("api_key_env", expected.api_key_env, saved.api_key_env),
        ("models", expected.models, saved.models),
        ("api_mode", expected.api_mode, saved.api_mode),
        ("reasoning_effort", expected.reasoning_effort, saved.reasoning_effort),
        ("extra_headers", expected.extra_headers, saved.extra_headers),
    )
    mismatched = [
        label
        for label, want, got in pairs
        if _normalized(want) != _normalized(got)
    ]
    if mismatched:
        raise ConfigEditError(
            "写入后回读与界面不一致（这些值没有落到 "
            f"{os.path.basename(paths.services_path)}）：{'、'.join(mismatched)}"
        )


def _normalized(value: Any) -> Any:
    """比对用的归一化：字符串去空白、列表逐项去空白、映射按键排序。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value]
    if isinstance(value, Mapping):
        return {str(key).strip(): str(item).strip() for key, item in value.items()}
    return value


def _save_extra_headers(target: str, name: str, extra_headers: Mapping[str, Any]) -> None:
    """写 / 删 ``[providers.<name>.extra_headers]`` 子表（空则删除整个小节）。"""
    section = f"{_PROVIDERS_SECTION}.{name}.extra_headers"
    headers = {
        str(key).strip(): "" if value is None else str(value).strip()
        for key, value in (extra_headers or {}).items()
        if str(key).strip()
    }
    if headers:
        patch_section(target, section, headers, header_comment="Provider 自定义请求头")
    else:
        remove_section(target, section)


def delete_provider(paths: ConfigPaths, name: str) -> str:
    """删除一个 provider 小节（含 ``.model_configs`` 之类子表），返回文件路径。"""
    target = _providers_path(paths)
    section = f"{_PROVIDERS_SECTION}.{validate_provider_name(name)}"
    if not remove_section(target, section):
        raise ConfigEditError(f"{os.path.basename(target)} 里没有 [providers.{name}]")
    return target


def _providers_path(paths: ConfigPaths) -> str:
    """provider 写回目标：优先 ``services.toml``；只有旧 ``providers.toml`` 时写旧的。"""
    services = paths.services_path
    if os.path.exists(services):
        return services
    legacy = os.path.join(paths.config_dir, "providers.toml")
    if paths.services_is_legacy_providers or os.path.exists(legacy):
        return legacy
    return services


# ---------------------------------------------------------------------------
# 上下文窗口（services.toml）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ContextWindows:
    """界面上要展示的上下文窗口配置。

    ``default`` 为 0 表示文件里没写（后台服务会退回内置 256000）；
    ``overrides`` 的键是 ``(provider, model)``。
    """

    default: int = 0
    overrides: dict[tuple[str, str], int] = field(default_factory=dict)


def parse_context_window(value: Any) -> int:
    """把界面输入解析成 tokens：接受 ``1000000`` / ``1_000_000`` / ``1m`` / ``512k``。

    只做格式与上下限校验；真假窗口大小以后台服务解析结果为准。
    """
    raw = str(value or "").strip().lower().replace("_", "").replace(",", "").replace(" ", "")
    if not raw:
        raise ConfigEditError("上下文窗口不能为空")
    multiplier = 1
    if raw.endswith("m"):
        multiplier = 1_000_000
        raw = raw[:-1]
    elif raw.endswith("k"):
        multiplier = 1_000
        raw = raw[:-1]
    try:
        number = float(raw)
    except ValueError as exc:
        raise ConfigEditError(
            f"上下文窗口必须是正整数 tokens（例如 1000000 / 1m / 512k），当前是 {value!r}"
        ) from exc
    scaled = number * multiplier
    tokens = int(scaled)
    if abs(scaled - tokens) > 1e-6:
        raise ConfigEditError(f"上下文窗口必须是整数 tokens，当前是 {value!r}")
    if tokens < MIN_CONTEXT_WINDOW:
        raise ConfigEditError(f"上下文窗口至少 {MIN_CONTEXT_WINDOW} tokens，当前是 {tokens}")
    if tokens > MAX_CONTEXT_WINDOW:
        raise ConfigEditError(f"上下文窗口最多 {MAX_CONTEXT_WINDOW} tokens，当前是 {tokens}")
    return tokens


def load_context_windows(paths: ConfigPaths) -> ContextWindows:
    """读 ``services.toml`` 里的默认窗口与按模型覆盖（不含内置兜底）。"""
    data = read_toml(_providers_path(paths))
    meta = data.get("model_metadata")
    default = 0
    if isinstance(meta, Mapping):
        default = _as_int(meta.get("default_context_window"))
    overrides: dict[tuple[str, str], int] = {}
    providers = data.get("providers")
    if isinstance(providers, Mapping):
        for provider_name, provider in providers.items():
            if not isinstance(provider, Mapping):
                continue
            model_configs = provider.get("model_configs")
            if not isinstance(model_configs, Mapping):
                continue
            for model_name, model_config in model_configs.items():
                if not isinstance(model_config, Mapping):
                    continue
                window = _as_int(model_config.get("context_window"))
                if window > 0:
                    overrides[(str(provider_name), str(model_name))] = window
    return ContextWindows(default=default, overrides=overrides)


def save_default_context_window(paths: ConfigPaths, window: int | None) -> bool:
    """写 / 删 ``[model_metadata].default_context_window``，返回是否改动。

    ``window`` 为 ``None`` 表示删掉这个键（后台服务退回内置 256000）。
    """
    target = _providers_path(paths)
    existing = _find_section_header(target, ["model_metadata"])
    if window is None and existing is None:
        return False
    patch_section(
        target,
        "model_metadata",
        {"default_context_window": None if window is None else int(window)},
        header_comment="模型元数据：未单独覆盖的 provider/model 都用这个上下文窗口",
    )
    expected = 0 if window is None else int(window)
    saved = load_context_windows(paths).default
    if saved != expected:
        raise ConfigEditError(
            "写入后回读不一致（[model_metadata].default_context_window "
            f"期望 {expected or '未设置'}，文件里是 {saved or '未设置'}）"
        )
    if window is None:
        # 删掉最后一个键后不留下一个空 [model_metadata] 小节。
        meta = read_toml(target).get("model_metadata")
        if not meta and existing is not None:
            remove_section(target, existing)
    return True


def save_model_context_window(
    paths: ConfigPaths, provider: str, model: str, window: int
) -> bool:
    """写 ``[providers.<provider>.model_configs.<model>].context_window``。

    小节名按现有文件里的写法复用（模型名带点时是 TOML 引号键），没有才新建；
    写盘后回读 ``services.toml`` 校验确实落盘。
    """
    provider_name = validate_provider_name(provider)
    model_name = _validate_model_name(model)
    known = load_providers(paths)
    if provider_name not in known and not paths.services_is_legacy_providers:
        raise ConfigEditError(
            f"{os.path.basename(_providers_path(paths))} 里没有 [providers.{provider_name}]；"
            "先在上面「LLM Provider」页保存这个 provider"
        )
    target = _providers_path(paths)
    section = _find_section_header(
        target, ["providers", provider_name, "model_configs", model_name]
    ) or _render_context_section(provider_name, model_name)
    patch_section(
        target,
        section,
        {"context_window": int(window)},
        header_comment="按模型的上下文窗口（桌面端「上下文窗口」页维护）",
    )
    saved = load_context_windows(paths).overrides.get((provider_name, model_name), 0)
    if saved != int(window):
        raise ConfigEditError(
            "写入后回读不一致（"
            f"{provider_name}/{model_name} 期望 {int(window)}，"
            f"文件里是 {saved or '未写入'}）"
        )
    return True


def remove_model_context_window(paths: ConfigPaths, provider: str, model: str) -> bool:
    """删掉某个模型的 ``context_window`` 覆盖；该小节没别的键了就把小节一起删。"""
    provider_name = validate_provider_name(provider)
    model_name = _validate_model_name(model)
    target = _providers_path(paths)
    section = _find_section_header(
        target, ["providers", provider_name, "model_configs", model_name]
    )
    if section is None:
        raise ConfigEditError(f"{provider_name}/{model_name} 没有按模型的上下文窗口覆盖")
    patch_section(target, section, {"context_window": None})
    data = read_toml(target)
    entry: Any = None
    providers = data.get("providers")
    if isinstance(providers, Mapping):
        provider_entry = providers.get(provider_name)
        if isinstance(provider_entry, Mapping):
            configs = provider_entry.get("model_configs")
            if isinstance(configs, Mapping):
                entry = configs.get(model_name)
    if not entry:
        remove_section(target, section)
    if load_context_windows(paths).overrides.get((provider_name, model_name)):
        raise ConfigEditError(
            f"删除后回读仍能看到 {provider_name}/{model_name} 的上下文窗口覆盖"
        )
    return True


def _validate_model_name(model: str) -> str:
    name = (model or "").strip()
    if not name:
        raise ConfigEditError("模型名不能为空")
    if len(name) > 200 or any(ord(char) < 0x20 for char in name):
        raise ConfigEditError("模型名不合法（不能有控制字符，最长 200 字符）")
    return name


def _render_context_section(provider: str, model: str) -> str:
    return (
        "providers."
        + _toml_key_segment(provider)
        + ".model_configs."
        + _toml_key_segment(model)
    )


def _toml_key_segment(value: str) -> str:
    """把一个 TOML 键段渲染成字面量；带点/空格的模型名要加引号。"""
    if re.fullmatch(r"[A-Za-z0-9_-]+", value or ""):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _find_section_header(target: str, parts: list[str]) -> str | None:
    """在文件里按**路径语义**找小节头，返回它原来的文本（含引号写法）。

    不能只做字符串比较：``[providers.x.model_configs."a.b"]`` 与
    ``[providers.x.model_configs.a.b]`` 语义不同（后者是四段路径），但
    ``[providers.x.model_configs."a-b"]`` 与 ``[providers.x.model_configs.a-b]``
    是同一张表，只是写法不同；这里统一按解析后的路径段比较。
    """
    try:
        with open(target, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigEditError(f"读取 {target} 失败：{exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("[") or stripped.startswith("[["):
            continue
        if not stripped.endswith("]") or stripped.endswith("]]"):
            continue
        name = stripped[1:-1].strip()
        if not name:
            continue
        if _split_header_path(name) == parts:
            return name
    return None


def _split_header_path(name: str) -> list[str]:
    """把 ``[a.b."c.d"]`` 的中间部分拆成路径段（支持基本引号/字面量引号）。"""
    parts: list[str] = []
    index = 0
    length = len(name)
    while index < length:
        char = name[index]
        if char in ". \t":
            index += 1
            continue
        if char == '"':
            index += 1
            buffer: list[str] = []
            while index < length and name[index] != '"':
                if name[index] == "\\" and index + 1 < length:
                    buffer.append(name[index + 1])
                    index += 2
                    continue
                buffer.append(name[index])
                index += 1
            index += 1
            parts.append("".join(buffer))
            continue
        if char == "'":
            end = name.find("'", index + 1)
            if end < 0:
                parts.append(name[index + 1 :])
                index = length
            else:
                parts.append(name[index + 1 : end])
                index = end + 1
            continue
        end = index
        while end < length and name[end] not in ".[] \t":
            end += 1
        segment = name[index:end].strip()
        if segment:
            parts.append(segment)
        index = end if end > index else index + 1
    return parts


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 模型分配（state.toml）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSlot:
    """一个模型槽位：``state.toml`` 里的哪一段、界面上叫什么。"""

    key: str
    label: str
    section: str


#: 界面上可编辑的模型槽位（顺序即展示顺序）。
MODEL_SLOTS: tuple[ModelSlot, ...] = (
    ModelSlot("work", "工作模式（工具/复杂任务）", "mode_models.work"),
    ModelSlot("chat", "聊天模式（日常对话）", "mode_models.chat"),
    ModelSlot("naming", "命名模型（会话标题）", "naming_model"),
    ModelSlot("compact", "压缩模型（上下文压缩）", "compact_model"),
)

#: 后台服务启动时**必须有值**的槽位（见 ``AppConfig.validate_mode_models``）：
#: 这两个槽位清空会让 ``state.toml`` 缺段，服务启动直接失败，所以界面不能把它们清空。
REQUIRED_MODEL_SLOTS: tuple[str, ...] = ("work", "chat")


def load_model_selection(paths: ConfigPaths) -> tuple[str, dict[str, tuple[str, str]]]:
    """读默认模式与各槽位的模型分配，返回 ``(default_mode, {slot: (provider, model)})``。"""
    data = read_toml(paths.state_path) if os.path.exists(paths.state_path) else {}
    session = data.get("session")
    default_mode = _text(session.get("default_mode")) if isinstance(session, Mapping) else ""
    values: dict[str, tuple[str, str]] = {}
    for slot in MODEL_SLOTS:
        head, _, tail = slot.section.partition(".")
        entry = data.get(head)
        if tail and isinstance(entry, Mapping):
            entry = entry.get(tail)
        if isinstance(entry, Mapping):
            values[slot.key] = (_text(entry.get("provider")), _text(entry.get("model")))
        else:
            values[slot.key] = ("", "")
    return default_mode, values


def save_model_selection(
    paths: ConfigPaths,
    default_mode: str,
    values: Mapping[str, tuple[str, str]],
) -> str:
    """写回默认模式与各槽位，返回 ``state.toml`` 路径。

    1.0.2 修正（对应"设置里改了、文件里没落实"的缺陷）：

    - **全部校验通过后才动文件**：任何一个槽位非法都不写，不会留下半份配置；
    - provider 与 model 必须同时有值或同时为空；半填直接拒绝（旧实现只写非空的那个
      键，会在文件里留下"旧 provider + 新 model"的错配）；
    - 工作/聊天槽位不能清空：``state.toml`` 缺 ``[mode_models.work]`` /
      ``[mode_models.chat]`` 时后台服务启动即报 ``provider/model is required``；
    - 命名/压缩槽位清空 = 从 ``state.toml`` **删掉整个小节**（旧实现是"界面清空、
      文件里旧值还在"，重启后旧模型又生效）；
    - 写盘后回读校验，界面值与文件值不一致时抛 :class:`ConfigEditError`。
    """
    target = paths.state_path
    default = (default_mode or "").strip()
    if default and default not in ("work", "chat"):
        raise ConfigEditError(f"默认会话模式只能是 work 或 chat，当前是 {default!r}")
    plan: list[tuple[ModelSlot, dict[str, str] | None]] = []
    for slot in MODEL_SLOTS:
        if slot.key not in values:
            continue
        provider, model = values[slot.key]
        provider = (provider or "").strip()
        model = (model or "").strip()
        if not provider and not model:
            if slot.key in REQUIRED_MODEL_SLOTS:
                raise ConfigEditError(
                    f"「{slot.label}」不能清空：state.toml 缺少 {slot.section} 时后台服务"
                    "启动会直接失败（provider/model is required）。请选择 provider 和模型。"
                )
            plan.append((slot, None))
            continue
        if not provider or not model:
            raise ConfigEditError(
                f"「{slot.label}」的 provider 和模型必须同时填写"
                f"（当前 provider={provider or '空'}、model={model or '空'}）"
            )
        plan.append((slot, {"provider": provider, "model": model}))

    if default:
        patch_section(target, "session", {"default_mode": default})
    expected: dict[str, tuple[str, str]] = {}
    for slot, payload in plan:
        if payload is None:
            remove_section(target, slot.section)
            expected[slot.key] = ("", "")
            continue
        patch_section(target, slot.section, payload, header_comment="桌面端设置的模型分配")
        expected[slot.key] = (payload["provider"], payload["model"])
    _verify_slots_saved(paths, default, expected)
    return target


def _verify_slots_saved(
    paths: ConfigPaths,
    default_mode: str,
    expected: Mapping[str, tuple[str, str]],
) -> None:
    """回读 ``state.toml`` 校验模型分配真的落盘；不一致时抛 :class:`ConfigEditError`。"""
    saved_mode, saved = load_model_selection(paths)
    problems: list[str] = []
    if default_mode and saved_mode != default_mode:
        problems.append(f"默认会话模式（期望 {default_mode}，文件里是 {saved_mode or '空'}）")
    for slot in MODEL_SLOTS:
        if slot.key not in expected:
            continue
        want = tuple(_normalized(item) for item in expected[slot.key])
        got = tuple(_normalized(item) for item in saved.get(slot.key, ("", "")))
        if want != got:
            problems.append(
                f"{slot.section}（期望 {want[0] or '空'}/{want[1] or '空'}，"
                f"文件里是 {got[0] or '空'}/{got[1] or '空'}）"
            )
    if problems:
        raise ConfigEditError(
            "写入后回读与界面不一致（这些值没有落到 "
            f"{os.path.basename(paths.state_path)}）：{'；'.join(problems)}"
        )


def mask_secret(value: str) -> str:
    """把密钥渲染成掩码（只留头尾各 4 个字符）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 10:
        return "****"
    return f"{text[:4]}…{text[-4:]}"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
