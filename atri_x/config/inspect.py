"""严格解码框架与配置自检（未知键、内置 Skill 漂移）。

对应 Go：``internal/config/inspect.go``（``UnknownAppConfigKeys`` / ``BuiltinAssetDrift``，
它们用的是 ``toml.NewDecoder(...).DisallowUnknownFields()`` 严格解码）。

**本模块还是全配置层共用的解码基座**（放在这里是因为它服务的正是"严格解码 / 未知键"）：

- :class:`StrictConfig`：配置 dataclass 基类，提供 ``from_dict()`` / ``to_dict()``；
- :data:`TOML_OFF` / :data:`OMITEMPTY` / :data:`OFF_EMPTY`：字段级 metadata
  （对应 Go 的 ``,omitempty`` 与 ``toml:"-"``）；
- :func:`load_toml` + :class:`ConfigDecodeError` / :class:`UnknownConfigKeysError` /
  :class:`MissingConfigFileError`：带**文件路径**与**键路径**的错误。

约定（其他配置模块都依赖它）：

1. **未知键不静默丢弃**：收集全部未知键后抛 :class:`UnknownConfigKeysError`，
   信息形如 ``app.toml: unknown config keys: ops.bogus``；传入 ``unknown`` 列表时改为
   仅收集（:func:`unknown_app_config_keys` 就是这么列出所有未知键的）。
2. **类型不符立刻报错**，信息形如
   ``app.toml: [tools].max_rounds_per_turn: 期望 int，得到 string ('ten')``。
3. 字段名 = TOML 键名（snake_case），因此 ``tomllib`` 读回 ``to_dict()`` 的结果与
   ``from_dict()`` 的入参等值；``None`` 与 ``omitempty`` 标记的空值不写回。
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Union, get_args, get_origin, get_type_hints

from atri_x.errors import InvalidError

__all__ = [
    "ConfigDecodeError",
    "ConfigError",
    "MissingConfigFileError",
    "OFF_EMPTY",
    "OMITEMPTY",
    "StrictConfig",
    "TOML_OFF",
    "UnknownConfigKeysError",
    "builtin_asset_drift",
    "key_path",
    "load_toml",
    "normalize_string_list",
    "unknown_app_config_keys",
]

#: 字段级 metadata：该字段不是 TOML 键（对应 Go 的 ``toml:"-"``）。
TOML_OFF: Mapping[str, Any] = {"toml": False}
#: 字段级 metadata：空值不写回（对应 Go 的 ``,omitempty``）。
OMITEMPTY: Mapping[str, Any] = {"omitempty": True}
#: ``toml:"-"`` + ``,omitempty`` 的组合（仅运行态结构用得到）。
OFF_EMPTY: Mapping[str, Any] = {"toml": False, "omitempty": True}


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
class ConfigError(InvalidError):
    """配置层错误基类（对应 Go config 包里 ``fmt.Errorf`` 包装的错误；上游无哨兵）。"""


class ConfigDecodeError(ConfigError):
    """配置解码失败：类型或结构与声明不符。信息含文件路径与键路径。"""


class UnknownConfigKeysError(ConfigDecodeError):
    """严格解码发现未被任何字段消费的键。

    ``keys`` 是点分键路径（已排序），对应 Go ``toml.StrictMissingError.Errors``。
    """

    def __init__(self, source: str, keys: Sequence[str]) -> None:
        self.source = source
        self.keys = sorted(keys)
        super().__init__(f"{source}: unknown config keys: {', '.join(self.keys)}")


class MissingConfigFileError(FileNotFoundError):
    """配置引用的文件不存在。

    对应 Go ``loadTOML`` 返回的 ``os.ErrNotExist``（可被
    ``errors.Is(err, os.ErrNotExist)`` 判定），额外带上**键路径**
    （如 ``[config_files].services``）。
    """


def key_path(path: Sequence[str]) -> str:
    """把键路径渲染成 TOML 形态：``("a", "b", "c") -> "[a.b].c"``。

    数组元素用 ``#<下标>`` 标记，渲染成 ``targets[0]`` 形态（键名本身是数字时不会误判）。
    """
    if not path:
        return "<root>"
    segments: list[str] = []
    for part in path:
        if part.startswith("#") and part[1:].isdigit() and segments:
            segments[-1] = f"{segments[-1]}[{part[1:]}]"
        else:
            segments.append(part)
    if len(segments) == 1:
        return f"[{segments[0]}]"
    return "[" + ".".join(segments[:-1]) + "]." + segments[-1]


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------
_NONE_TYPE = type(None)
_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    cached = _HINTS.get(cls)
    if cached is None:
        cached = get_type_hints(cls)
        _HINTS[cls] = cached
    return cached


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """拆掉 ``X | None``，返回 ``(X, True)``；非可选原样返回。"""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = get_args(annotation)
        if _NONE_TYPE in args:
            rest = tuple(arg for arg in args if arg is not _NONE_TYPE)
            if len(rest) == 1:
                return rest[0], True
            return Union[rest], True  # type: ignore[return-value]
    return annotation, False


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "table"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _describe(annotation: Any) -> str:
    ann, _ = _unwrap_optional(annotation)
    if ann is Any:
        return "任意值"
    if isinstance(ann, type) and issubclass(ann, StrictConfig):
        return f"{ann.__name__} 表"
    return {bool: "bool", int: "int", float: "float", str: "string", list: "array", dict: "table"}.get(
        ann, str(ann)
    )


def _type_error(path: Sequence[str], annotation: Any, value: Any, source: str) -> ConfigDecodeError:
    return ConfigDecodeError(
        f"{source}: {key_path(path)}: 期望 {_describe(annotation)}，"
        f"得到 {_type_name(value)} ({value!r})"
    )


def _decode(
    annotation: Any, value: Any, path: tuple[str, ...], source: str, unknown: list[str] | None
) -> Any:
    ann, optional = _unwrap_optional(annotation)
    if value is None:
        if optional or ann is Any:
            return None
        raise _type_error(path, annotation, value, source)
    if ann is Any:
        return value
    origin = get_origin(ann)
    if origin in (list, tuple) or (
        origin is not None and isinstance(origin, type) and issubclass(origin, Sequence)
    ):
        if not isinstance(value, (list, tuple)):
            raise _type_error(path, annotation, value, source)
        args = get_args(ann)
        item_ann = args[0] if args else Any
        return [
            _decode(item_ann, item, (*path, f"#{index}"), source, unknown)
            for index, item in enumerate(value)
        ]
    if origin is dict or (
        origin is not None and isinstance(origin, type) and issubclass(origin, Mapping)
    ):
        if not isinstance(value, Mapping):
            raise _type_error(path, annotation, value, source)
        args = get_args(ann)
        value_ann = args[1] if len(args) > 1 else Any
        decoded: dict[str, Any] = {}
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise _type_error(path, annotation, value, source)
            decoded[item_key] = _decode(value_ann, item, (*path, item_key), source, unknown)
        return decoded
    if not isinstance(ann, type):
        return value
    if issubclass(ann, StrictConfig):
        return ann.from_dict(value, path=path, source=source, unknown=unknown)
    if ann is bool:
        if isinstance(value, bool):
            return value
        raise _type_error(path, annotation, value, source)
    if ann is int:
        # bool 是 int 的子类，但 TOML 的 true 不该被当成 1。
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error(path, annotation, value, source)
        return value
    if ann is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _type_error(path, annotation, value, source)
        return float(value)
    if ann is str:
        if isinstance(value, str):
            return value
        raise _type_error(path, annotation, value, source)
    return value


def _encode(value: Any) -> Any:
    if isinstance(value, StrictConfig):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _is_empty(value: Any) -> bool:
    """``omitempty`` 的判空；结构体（dataclass）永不为空，与 Go 一致。"""
    if value is None:
        return True
    if isinstance(value, StrictConfig):
        return False
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return not value
    return False


def _dotted(path: Sequence[str], key: str) -> str:
    return ".".join((*path, key))


class StrictConfig:
    """配置 dataclass 基类：严格 ``from_dict`` / 对称 ``to_dict``。

    子类约定：**所有字段都要有默认值**（默认值即 Go 零值 + 有效默认），
    字段名必须与 TOML 键名逐字相同；非 TOML 字段用 ``field(metadata=TOML_OFF)`` 标出。
    """

    _toml_fields: ClassVar[dict[str, dataclasses.Field]] = {}

    @classmethod
    def toml_fields(cls) -> dict[str, dataclasses.Field]:
        """返回 ``TOML 键名 -> dataclasses.Field``（不含 TOML_OFF 字段）。"""
        cached = cls.__dict__.get("_toml_fields")
        if cached is None:
            mapping: dict[str, dataclasses.Field] = {}
            for item in dataclasses.fields(cls):
                key = item.metadata.get("toml", item.name)
                if key is False or key is None:
                    continue
                mapping[key] = item
            cls._toml_fields = mapping
            cached = mapping
        return cached

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        path: tuple[str, ...] = (),
        source: str = "<config>",
        unknown: list[str] | None = None,
    ) -> Any:
        """严格解码一张 TOML 表。

        ``unknown`` 非空时改为**收集**未知键（供 :func:`unknown_app_config_keys` 一次
        列出所有未知键），不抛异常。
        """
        if not isinstance(data, Mapping):
            raise ConfigDecodeError(
                f"{source}: {key_path(path)}: 期望 {cls.__name__} 表，得到 {_type_name(data)}"
            )
        hints = _hints(cls)
        fields_by_key = cls.toml_fields()
        collected = unknown if unknown is not None else []
        values: dict[str, Any] = {}
        for key, raw in data.items():
            item = fields_by_key.get(key)
            if item is None:
                collected.append(_dotted(path, key))
                continue
            values[item.name] = _decode(hints[item.name], raw, (*path, key), source, collected)
        if collected and unknown is None:
            raise UnknownConfigKeysError(source, collected)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        """输出 TOML 形态的映射（``None`` 与 ``omitempty`` 空值不输出）。"""
        out: dict[str, Any] = {}
        for key, item in self.toml_fields().items():
            value = getattr(self, item.name)
            if value is None:
                continue
            if item.metadata.get("omitempty") and _is_empty(value):
                continue
            out[key] = _encode(value)
        return out

    def copy(self) -> Any:
        """浅拷贝（Go 里 struct 是值类型，赋值即复制）。"""
        return dataclasses.replace(self)


def normalize_string_list(values: Sequence[str]) -> list[str]:
    """去空白、丢空串、按首次出现顺序去重。对应 Go ``normalizeStringList``。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def load_toml(path: str | Path, cls: type, *, strict: bool = True, source: str | None = None) -> Any:
    """读取并严格解码一个 TOML 文件。对应 Go ``loadTOML``。

    ``strict=False`` 时未知键被忽略（对应 Go 的非严格解码路径：``state.toml`` /
    ``atrinis.toml`` / ``tool_tags.toml`` 与旧 ``providers.toml``）。
    """
    label = source if source is not None else str(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise MissingConfigFileError(f"read config {str(path)!r}: 文件不存在") from None
    except IsADirectoryError:
        raise MissingConfigFileError(f"read config {str(path)!r}: 是一个目录") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigDecodeError(f"{label}: TOML 解析失败：{exc}") from None
    except OSError as exc:
        raise ConfigDecodeError(f"read config {str(path)!r}: {exc}") from None
    return cls.from_dict(data, source=label, unknown=None if strict else [])


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def unknown_app_config_keys(config_path: str | os.PathLike[str] | None = "") -> list[str]:
    """列出 ``app.toml`` 里没有被任何已知字段消费的键（点分路径，已排序）。

    对应 Go ``UnknownAppConfigKeys``：拼错的段名（如 ``[provders.openai]``）或已废弃的
    键在启动时本会被静默忽略，这里用**严格解码**把它们挑出来。类型错误仍然抛
    :class:`ConfigDecodeError`（与 Go 一致：非未知键的解码错误作为 error 返回，
    而不是混进键列表）。
    """
    # 延迟导入：本模块是 app.py / app_sections.py 的基座，不能在模块级反向依赖它们。
    from atri_x.config.app import AppConfig
    from atri_x.config.loader import resolve_path

    path = resolve_path(config_path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise MissingConfigFileError(f"read config {path!r}: 文件不存在") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigDecodeError(f"{path}: TOML 解析失败：{exc}") from None
    except OSError as exc:
        raise ConfigDecodeError(f"read config {path!r}: {exc}") from None
    unknown: list[str] = []
    AppConfig.from_dict(data, source=path, unknown=unknown)
    return sorted(set(unknown))


def builtin_asset_drift(config_dir: str) -> list[str]:
    """比较磁盘上的**内置 Skill 文件**与内置模板，返回人可读的漂移说明。

    对应 Go ``BuiltinAssetDrift``：只比较 ``skills/`` 下的资产 —— app.toml /
    services.toml 本来就是给人改的，而内置 AgentSkill 与随包版本不一致通常意味着
    装了一份陈旧或手改过的副本。
    """
    from atri_x.config import assets as _assets

    directory = (config_dir or "").strip()
    if not directory:
        return []
    drift: list[str] = []
    for asset in _assets.DEFAULT_CONFIG_ASSETS:
        if not asset.path.startswith("skills/"):
            continue
        path = Path(directory).joinpath(*asset.path.split("/"))
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            drift.append("缺失内置 Skill 文件 " + asset.path)
            continue
        except OSError as exc:
            drift.append("无法读取内置 Skill 文件 " + asset.path + "：" + str(exc))
            continue
        if data != asset.content().encode("utf-8"):
            drift.append("内置 Skill 文件与默认版本不一致 " + asset.path)
    return sorted(drift)
