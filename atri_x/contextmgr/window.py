"""上下文窗口解析与缓存。

对应 Go：``internal/contextmgr/window.go``。

解析顺序（与 Go 一致）：

1. 进程内缓存；
2. provider 客户端的 ``ListModelMetadata``（``client_for(provider)`` 返回的对象
   若有 ``list_model_metadata`` 就用它）——最权威；
3. 配置里的手工 ``model_configs.<model>.context_window``；
4. ``default_context_window``；
5. :data:`atri_x.contextmgr.budget.FALLBACK_CONTEXT_WINDOW`。

注意 Go 的第 4 步直接返回 ``DefaultWindow`` 而**不写缓存**；这里照抄。

两处 Python 特有的地方（上游没有对应物，见 ``verification/contextmgr.md`` CM-1）：

* 第 2 步在 Python 里是 ``async`` 的，所以 :meth:`WindowResolver.resolve_sync`
  是**已解析元数据**的同步读法：它不会去发请求，但会读
  :meth:`WindowResolver.resolve` 记下来的 provider 元数据快照（粒度是
  ``provider → {model: window}``，一次取到的整份都留着）。因此"异步路径解析过一次"
  之后，**同一 provider 的任意模型**在同步路径上都能拿到 provider 上报值。
* ``client_for(provider)`` 对未知 provider 可能**抛错**（``AgentSeams`` 就是），
  而 Go 的 ``ClientFor`` 返回 nil 接口、断言失败后继续往下走；这里同样降级。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from atri_x.contextmgr.budget import FALLBACK_CONTEXT_WINDOW

#: ``provider -> LLM 客户端``。对应 Go ``ClientProvider``。
ClientProvider = Callable[[str], Any]


class WindowResolver:
    """按 ``provider/model`` 解析上下文窗口。对应 Go ``WindowResolver``。"""

    def __init__(
        self,
        default_window: int = 0,
        manual_windows: dict[str, int] | None = None,
        client_for: ClientProvider | None = None,
    ) -> None:
        self.default_window = default_window
        self.manual_windows: dict[str, int] = dict(manual_windows or {})
        self.client_for = client_for
        self._mu = threading.RLock()
        self._cache: dict[str, int] = {}
        #: ``provider -> {model: window}``：**已解析**的 provider 上报元数据快照。
        #: ``resolve`` 每次成功取到元数据就把整份记下来（不是只记当前那个模型），
        #: 这样同步路径对同一 provider 的任意模型都能取到 provider 上报值。
        self._metadata: dict[str, dict[str, int]] = {}

    # -- 解析 ---------------------------------------------------------------
    async def resolve(self, ctx: Any, provider: str, model: str) -> int:
        """解析窗口大小。对应 Go ``Resolve``（``ctx`` 仅用于取消语义）。

        客户端元数据是 async 的，所以本方法是 ``async``；其余分支同步完成。
        """
        key = f"{provider}/{model}"
        cached = self._cached(key)
        if cached > 0:
            return cached

        metadata = await self._fetch_metadata(ctx, provider)
        window = int(metadata.get(model, 0) or 0)
        if window > 0:
            return self._cache_value(key, window)

        manual = self.manual_windows.get(key, 0)
        if manual > 0:
            return self._cache_value(key, manual)
        return self._manual_or_default(key)

    def resolve_sync(self, provider: str, model: str) -> int:
        """同步版：读缓存与**已解析**的 provider 元数据，取不到再走配置/默认值。

        与 :meth:`resolve` 的差别只有一处 —— provider 适配器的
        ``list_model_metadata`` 是协程，这里不能 ``await``，所以不会现取元数据。
        只要该 provider 的元数据被 :meth:`resolve` / :meth:`prewarm` 解析过一次
        （同一进程内，例如本轮构建 prompt 预算时），两者的取值就完全一致。

        同步版存在的原因是 ``should_compact`` / ``status`` 这些调用点在
        ``AgentSeams`` 里是同步返回值。
        """
        key = f"{provider}/{model}"
        cached = self._cached(key)
        if cached > 0:
            return cached

        window = int(self._known_metadata(provider).get(model, 0) or 0)
        if window > 0:
            return self._cache_value(key, window)

        # ⚠️ 配置值/默认值**不写缓存**：同步路径还没问过 provider 的元数据，把
        # 配置值写进去会让之后的异步路径命中缓存、永远读不到 provider 上报值
        # （Go 的 Resolve 是先问元数据、失败才用配置值，所以它缓存配置值是安全的）。
        return self._manual_or_default(key)

    def _manual_or_default(self, key: str) -> int:
        """Go ``Resolve`` 的最后两级：手工配置 → 默认窗口 → 内置兜底。"""
        manual = self.manual_windows.get(key, 0)
        if manual > 0:
            return manual
        if self.default_window > 0:
            return self.default_window
        return FALLBACK_CONTEXT_WINDOW

    # -- provider 元数据 ----------------------------------------------------
    async def _fetch_metadata(self, ctx: Any, provider: str) -> dict[str, int]:
        """现取 provider 上报的窗口元数据，并整份记进快照。

        对应 Go ``Resolve`` 的第 2 步：**每次缓存未命中都会现取一次**
        （Go 也不缓存"没找到该模型"这个否定结果），成功取到就整份留档供同步路径使用。
        客户端缺失 / 不支持 / 抛错 / 未知 provider 一律降级为空（对应 Go
        ``ListModelMetadata`` 返回 err 与 ``ClientFor(provider)`` 为 nil 的两种情况）。
        """
        client = self._client_for(provider)
        if client is None:
            return {}
        lister = getattr(client, "list_model_metadata", None)
        if not callable(lister):
            return {}
        try:
            metadata = lister(ctx)
            if hasattr(metadata, "__await__"):
                metadata = await metadata
        except Exception:  # noqa: BLE001 - 元数据失败必须降级而不是中断
            return {}
        return self._remember_metadata(provider, metadata)

    def _known_metadata(self, provider: str) -> dict[str, int]:
        """已经解析过的 provider 元数据快照（没有就返回空 dict）。"""
        with self._mu:
            return self._metadata.get(provider) or {}

    async def prewarm(self, ctx: Any, providers: Any) -> None:
        """装配期一次性把各 provider 上报的窗口记进快照（上游没有对应方法）。

        这是 CM-1 的"冷缓存"半边：同步路径不能 ``await``，所以在**装配期的异步
        调用点**（``ContextRuntimeState.configure`` 之后、任何 ``/context`` 或回合
        判定之前）调一次本方法，之后 :meth:`resolve_sync` 与 :meth:`resolve` 的取值
        就与 Go 一致了。``providers`` 可以是 provider 名的可迭代对象，也可以是
        ``{provider: 配置}`` 这样的映射。每个 provider 只发一次元数据请求；
        取不到元数据的 provider 保持原样（下次 ``resolve`` 会再试，与 Go 相同）。
        """
        names = list(providers.keys()) if hasattr(providers, "keys") else list(providers or [])
        for provider in names:
            await self._fetch_metadata(ctx, str(provider))

    def _remember_metadata(self, provider: str, metadata: Any) -> dict[str, int]:
        """把一份 ``ModelMetadata`` 列表记成快照；只留 ``context_window > 0`` 的项。"""
        windows: dict[str, int] = {}
        for item in metadata or []:
            model_id = str(getattr(item, "id", "") or "")
            window = int(getattr(item, "context_window", 0) or 0)
            if model_id and window > 0:
                windows[model_id] = window
        if not windows:
            return {}
        with self._mu:
            self._metadata[provider] = windows
        return windows

    def _client_for(self, provider: str) -> Any:
        """取 provider 客户端；未知 provider 返回 ``None``。

        对应 Go 的 ``r.ClientFor(provider)`` 返回 nil 接口（断言
        ``llm.ModelMetadataProvider`` 失败后继续往下走）。Python 侧的
        ``AgentSeams.client_for_provider`` 对未知 provider **抛错**，
        不能让这个异常冒到 ``/context`` 上。
        """
        if self.client_for is None:
            return None
        try:
            return self.client_for(provider)
        except Exception:  # noqa: BLE001 - 未知 provider 按"没有客户端"处理
            return None

    # -- 缓存 ---------------------------------------------------------------
    def _cached(self, key: str) -> int:
        with self._mu:
            return self._cache.get(key, 0)

    def _cache_value(self, key: str, value: int) -> int:
        """写缓存；已存在有效值时返回既有值（与 Go 一致，避免被后来的调用覆盖）。"""
        with self._mu:
            existing = self._cache.get(key, 0)
            if existing > 0:
                return existing
            self._cache[key] = value
            return value

    def clear_cache(self) -> None:
        """清空缓存（供配置热重载后使用；上游没有对应方法）。"""
        with self._mu:
            self._cache.clear()


def new_window_resolver(
    metadata: Any,
    providers: Any,
    client_for: ClientProvider | None = None,
) -> WindowResolver:
    """从配置构造解析器。对应 Go ``NewWindowResolver``。

    ``metadata`` / ``providers`` 按**鸭子类型**读取（对应 ``config.ModelMetadataConfig``
    与 ``map[string]config.ProviderConfig``），因为配置层的字段名可能随实现微调：

    - ``metadata.default_context_window``
    - ``providers[provider].model_configs[model].context_window``
    """
    manual: dict[str, int] = {}
    default_window = _as_int(getattr(metadata, "default_context_window", 0))
    if default_window <= 0 and isinstance(metadata, dict):
        default_window = _as_int(metadata.get("default_context_window", 0))

    provider_map = _as_mapping(providers)
    for provider_name, provider in provider_map.items():
        models = _as_mapping(getattr(provider, "model_configs", None))
        if not models and isinstance(provider, dict):
            models = _as_mapping(provider.get("model_configs"))
        for model_name, model_cfg in models.items():
            window = _as_int(getattr(model_cfg, "context_window", 0))
            if window <= 0 and isinstance(model_cfg, dict):
                window = _as_int(model_cfg.get("context_window", 0))
            if window > 0:
                manual[f"{provider_name}/{model_name}"] = window

    return WindowResolver(
        default_window=default_window, manual_windows=manual, client_for=client_for
    )


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return dict(items())
        except (TypeError, ValueError):
            return {}
    return {}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["ClientProvider", "WindowResolver", "new_window_resolver"]
