"""模型目录：从上游 ``/models`` 拉可用模型，并把「备注名」缓存在本地。

为什么单独一个文件：GUI 只读写配置、不跑 Agent，而**模型清单和备注名都不是上游认识
的配置键**——写进 ``services.toml`` 只会多出未知键（严格解码会报错），所以落在配置
目录的 ``gui/model_catalog.json`` 里：

```json
{"version": 1, "entries": {
  "providers.deepseek": {"models": ["deepseek-chat"], "aliases": {"deepseek-chat": "主力"},
                         "fetched_at": "2026-01-01 12:00:00", "source": "https://api.deepseek.com"},
  "image_generation":   {"models": ["gpt-image-2.5"], "aliases": {}}
}}
```

键的约定：LLM provider 用 ``providers.<名字>``，生图用 :data:`IMAGE_KEY`（两者不会撞）。

网络口径：只发一个 ``GET {base_url}/models``（OpenAI 兼容；``/v1`` 缺失时补试一次）。
不读配置、不碰密钥；密钥由调用方从 ``.env`` / 进程环境取好传进来。所有失败都抛
:class:`CatalogError`，消息直接可展示给用户。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

__all__ = [
    "CATALOG_FILE",
    "CATALOG_VERSION",
    "IMAGE_KEY",
    "MAX_ALIAS_RUNES",
    "MAX_MODELS",
    "Catalog",
    "CatalogEntry",
    "CatalogError",
    "catalog_key",
    "catalog_path",
    "clean_label",
    "display_label",
    "fetch_models",
    "load",
    "model_of_label",
    "parse_models",
    "provider_key",
]
#: GUI 私有目录名与文件名。
CATALOG_FILE = "model_catalog.json"

#: 文件格式版本（将来改结构时用来迁移）。
CATALOG_VERSION = 1

#: 生图模型目录的键（LLM provider 的键是 ``providers.<名字>``）。
IMAGE_KEY = "image_generation"

#: 单次最多收多少个模型（上游偶尔返回几百个）。
MAX_MODELS = 500

#: 备注名最大字符数。
MAX_ALIAS_RUNES = 40

#: 拉取超时（秒）。
FETCH_TIMEOUT = 15.0

#: 响应体上限（正常模型清单远小于它）。
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CatalogError(Exception):
    """取模型目录的可预期失败（消息直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 键与标签
# ---------------------------------------------------------------------------
def provider_key(name: str) -> str:
    """LLM provider 的名字 → 目录键。"""
    return "providers." + (name or "").strip()


def catalog_key(name: str) -> str:
    """``load_providers`` 里的名字 → 目录键（空名字回落到生图键）。"""
    value = (name or "").strip()
    return provider_key(value) if value else IMAGE_KEY


def clean_label(model: str) -> str:
    """模型名的空白清理（上游有时带前后空格）。"""
    return (model or "").strip()


def display_label(model: str, alias: str = "") -> str:
    """下拉框里显示的名字：有备注名就 ``备注（模型）``，否则直接用模型名。"""
    model = clean_label(model)
    alias = (alias or "").strip()
    return f"{alias}（{model}）" if alias else model


def model_of_label(model: str, models: Iterable[str] = ()) -> str:
    """把下拉框里的显示名还原成模型名（``备注（模型）`` → ``模型``）。

    ``models`` 是**真模型名**列表（不是显示名）：本身就是真模型名时原样返回；
    带 ``（…)`` / ``(…)`` 包裹的是"备注（模型）"形式，取括号里的部分。
    """
    value = (model or "").strip()
    known = {text for text in (clean_label(item) for item in models) if text}
    if value in known:
        return value
    for opener, closer in (("（", "）"), ("(", ")")):
        if value.endswith(closer) and opener in value:
            inner = value[value.rindex(opener) + len(opener) : -len(closer)].strip()
            if inner:
                return inner
    return value


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class CatalogEntry:
    """某个后端（provider 或生图）的模型清单 + 备注名。"""

    models: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    fetched_at: str = ""
    source: str = ""

    def alias_of(self, model: str) -> str:
        return (self.aliases.get(clean_label(model)) or "").strip()

    def set_alias(self, model: str, alias: str) -> None:
        model = clean_label(model)
        if not model:
            raise CatalogError("先选一个模型，再起备注名")
        alias = (alias or "").strip()[:MAX_ALIAS_RUNES]
        if alias:
            self.aliases[model] = alias
        else:
            self.aliases.pop(model, None)

    def labels(self) -> list[str]:
        """下拉框候选（含备注名的显示形式）。"""
        return [display_label(model, self.alias_of(model)) for model in self.models]

    def merge(self, models: Iterable[str], *, source: str = "") -> list[str]:
        """记录一次拉取结果：模型去重合并、保留已有备注名，返回本次新增的模型。"""
        seen = {clean_label(model) for model in self.models}
        added: list[str] = []
        for raw in models:
            model = clean_label(raw)
            if not model or model in seen:
                continue
            seen.add(model)
            self.models.append(model)
            added.append(model)
        # 被 MAX_MODELS 截掉的模型也算"上游仍然有"，备注名留着；真正下架的清掉。
        self.aliases = {model: alias for model, alias in self.aliases.items() if model in seen}
        self.models = self.models[:MAX_MODELS]
        self.fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if source:
            self.source = source.strip()
        return added


#: 只读查询用的空条目（**不要改它**；写操作走 :meth:`Catalog.entry`）。
_EMPTY_ENTRY = CatalogEntry()


@dataclass
class Catalog:
    """``gui/model_catalog.json`` 的运行时视图。"""

    entries: dict[str, CatalogEntry] = field(default_factory=dict)

    def entry(self, key: str) -> CatalogEntry:
        """取某个键的条目（不存在时就地建一个空条目；写操作用它）。"""
        key = (key or "").strip() or IMAGE_KEY
        entry = self.entries.get(key)
        if entry is None:
            entry = CatalogEntry()
            self.entries[key] = entry
        return entry

    def peek(self, key: str) -> CatalogEntry:
        """只读地取条目（不存在时返回一个临时空条目，**不**污染目录）。"""
        return self.entries.get((key or "").strip() or IMAGE_KEY, _EMPTY_ENTRY)

    def models(self, key: str) -> list[str]:
        return list(self.peek(key).models)

    def aliases(self, key: str) -> dict[str, str]:
        return dict(self.peek(key).aliases)

    def alias_of(self, key: str, model: str) -> str:
        return self.peek(key).alias_of(model)

    def labels(self, key: str, configured: Iterable[str] = ()) -> list[str]:
        """下拉候选（显示形式：有备注名时 ``备注（模型）``）。"""
        entry = self.peek(key)
        return [display_label(model, entry.alias_of(model)) for model in self.names(key, configured)]

    def names(self, key: str, configured: Iterable[str] = ()) -> list[str]:
        """真模型名列表：已拉取的模型 + 配置文件里手写的模型（去重，保持顺序）。"""
        entry = self.peek(key)
        names: list[str] = list(entry.models)
        seen = set(names)
        for raw in configured:
            model = clean_label(raw)
            if model and model not in seen:
                seen.add(model)
                names.append(model)
        return names

    def record(self, key: str, models: Iterable[str], *, source: str = "") -> list[str]:
        return self.entry(key).merge(models, source=source)

    def set_alias(self, key: str, model: str, alias: str) -> None:
        self.entry(key).set_alias(model, alias)

    # -- 落盘 ---------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "version": CATALOG_VERSION,
            "entries": {
                key: {
                    "models": list(entry.models),
                    "aliases": dict(entry.aliases),
                    "fetched_at": entry.fetched_at,
                    "source": entry.source,
                }
                for key, entry in self.entries.items()
            },
        }

    def save(self, config_dir: str) -> str:
        """写回 ``<配置目录>/gui/model_catalog.json``（原子替换），返回路径。"""
        path = catalog_path(config_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.to_json(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise CatalogError(f"写入模型目录失败：{exc}") from exc
        return path


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def catalog_path(config_dir: str) -> str:
    """模型目录文件路径（``<配置目录>/gui/model_catalog.json``）。"""
    return os.path.join(config_dir or "", "gui", CATALOG_FILE)


def load(config_dir: str) -> Catalog:
    """读模型目录；文件不存在/损坏时返回空目录（不抛异常，界面照常可用）。"""
    path = catalog_path(config_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return Catalog()
    if not isinstance(data, dict):
        return Catalog()
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, dict):
        return Catalog()
    return Catalog(entries=_entries_from(raw_entries))


def _entries_from(raw_entries: dict[str, Any]) -> dict[str, CatalogEntry]:
    entries: dict[str, CatalogEntry] = {}
    for key, raw in raw_entries.items():
        if not isinstance(raw, dict):
            continue
        models = [clean_label(str(item)) for item in _as_list(raw.get("models"))]
        aliases_raw = raw.get("aliases")
        aliases: dict[str, str] = {}
        if isinstance(aliases_raw, dict):
            for model, alias in aliases_raw.items():
                model = clean_label(str(model))
                text = str(alias or "").strip()[:MAX_ALIAS_RUNES]
                if model and text:
                    aliases[model] = text
        entries[str(key)] = CatalogEntry(
            models=[model for model in models if model][:MAX_MODELS],
            aliases=aliases,
            fetched_at=str(raw.get("fetched_at") or ""),
            source=str(raw.get("source") or ""),
        )
    return entries


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


# ---------------------------------------------------------------------------
# 拉取
# ---------------------------------------------------------------------------
def fetch_models(base_url: str, api_key: str = "", *, timeout: float = FETCH_TIMEOUT) -> list[str]:
    """``GET {base_url}/models``，返回模型 id 列表；失败抛 :class:`CatalogError`。

    兼容 OpenAI（``{"data":[{"id":...}]}``）、``{"models":[...]}`` 与裸数组三种返回；
    ``/v1`` 缺失/多余时会补试一次另一种拼法（很多中转站两种都认）。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise CatalogError("先填 Base URL（例如 https://api.deepseek.com）")
    headers = {"Accept": "application/json", "User-Agent": "ATRI-X-GUI"}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    errors: list[str] = []
    for url in _candidate_urls(base):
        try:
            models = parse_models(_get_json(url, headers, timeout))
        except CatalogError as exc:
            errors.append(str(exc))
            continue
        if models:
            return models
        errors.append(f"{url} 没有返回模型清单")
    raise CatalogError("；".join(errors) or "获取模型目录失败")


def _candidate_urls(base: str) -> list[str]:
    if base.endswith("/models"):
        return [base]
    urls = [base + "/models"]
    if base.endswith("/v1"):
        urls.append(base[: -len("/v1")] + "/models")
    else:
        urls.append(base + "/v1/models")
    return urls


def _get_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 用户配置的地址
            status = int(getattr(response, "status", 200) or 200)
            body = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        message = _http_error_text(url, exc)
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - 关不掉也不影响提示
            pass
        raise CatalogError(message) from exc
    except urllib.error.URLError as exc:
        raise CatalogError(f"连不上 {url}：{getattr(exc, 'reason', exc)}") from exc
    except TimeoutError as exc:
        raise CatalogError(f"{url} 超时（{timeout:.0f} 秒）") from exc
    except OSError as exc:
        raise CatalogError(f"请求 {url} 失败：{exc}") from exc
    if status != 200:
        raise CatalogError(f"{url} 返回 HTTP {status}")
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise CatalogError(f"{url} 返回的不是 JSON（可能是网页或错误页）") from exc


def _http_error_text(url: str, exc: urllib.error.HTTPError) -> str:
    code = int(getattr(exc, "code", 0) or 0)
    if code in (401, 403):
        return f"{url} 鉴权失败（HTTP {code}）：检查密钥环境变量与 .env"
    if code == 404:
        return f"{url} 不存在（HTTP 404）：检查 Base URL 末尾要不要带 /v1"
    return f"{url} 返回 HTTP {code}"


def parse_models(data: Any) -> list[str]:
    """从 ``/models`` 的几种常见返回结构里取模型 id（去重、保序、限量）。"""
    raw_items: list[Any] = []
    if isinstance(data, dict):
        for key in ("data", "models", "model_list", "items"):
            value = data.get(key)
            if isinstance(value, (list, tuple)):
                raw_items = list(value)
                break
    elif isinstance(data, (list, tuple)):
        raw_items = list(data)
    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model = _model_name(item)
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
        if len(models) >= MAX_MODELS:
            break
    return models


def _model_name(item: Any) -> str:
    if isinstance(item, str):
        return clean_label(item)
    if isinstance(item, dict):
        for key in ("id", "name", "model", "model_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""
