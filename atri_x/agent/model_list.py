"""模型列表：缓存、并发合并、按 provider 聚合与命名模型档。

对应 Go：``internal/agent/model.go`` 的模型列举部分（``modelListCacheEntry`` /
``modelListCall`` / ``modelListCacheTTL`` / ``cachedProviderModels`` /
``mergeModelNames`` / ``sortedProviderModels`` / ``modelOptions`` / ``Models`` /
``ModelProfiles``）。

三条不能简化的语义：

1. **成功与失败的缓存窗口不同**：成功的列表很少变（10 分钟），失败常常是瞬时的
   （网络抖动、限流），只给 30 秒的负缓存，而不是永久缓存。
2. **刷新合并**：同一 provider 上并发到达的列举请求只发一次上游调用
   （Go 用 ``modelListInflight`` + channel；Python 用 ``asyncio.Task`` + ``shield``，
   等待者被取消不会连带取消共享的那次调用）。
3. **失败不抹掉上一次的好结果**：刷新失败时把 ``last_good`` 合并回来，让
   provider 短暂不可用时菜单降级成警告而不是变空。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from atri_x.agent.model import (
    ModelListOptions,
    ModelListResult,
    ModelOption,
    ModelProfile,
    ModelProviderError,
    ensure_model_runtime,
    host_of,
)
from atri_x.errors import InvalidError
from atri_x.storage.models import SessionModeChat, SessionModeWork

#: 可以装到 Agent 实例上的方法（``model_profiles`` 因与 Agent 字段撞名而排除：
#: 它由 :class:`atri_x.agent.model.ModelService` 门面提供）。
MODEL_LIST_METHODS: tuple[str, ...] = (
    "models",
    "model_list",
    "model_options",
    "cached_provider_models",
    "sorted_provider_models",
)

#: 成功列举的缓存时长。对应 Go ``modelListSuccessTTL``。
MODEL_LIST_SUCCESS_TTL = 600.0

#: 失败列举的缓存时长。对应 Go ``modelListErrorTTL``。
MODEL_LIST_ERROR_TTL = 30.0

#: 上游列举的超时。对应 Go ``sortedProviderModels`` 里的 ``5*time.Second``。
MODEL_LIST_FETCH_TIMEOUT = 5.0

#: ``/model`` 菜单里标记的模式槽位。对应 Go ``modelOptions`` 里的字面量列表。
MODEL_MODE_MARKS: tuple[str, ...] = (
    SessionModeChat,
    SessionModeWork,
    "atriwisp1",
    "atriwisp2",
    "atriwisp3",
)

FetchModels = Callable[[str], Awaitable[tuple[list[str], BaseException | None]]]


def model_list_cache_ttl(err: BaseException | None) -> float:
    """失败短缓存、成功长缓存。对应 Go ``modelListCacheTTL``。"""
    return MODEL_LIST_ERROR_TTL if err is not None else MODEL_LIST_SUCCESS_TTL


@dataclass
class ModelListCacheEntry:
    """一次 provider 列举的缓存项。对应 Go ``modelListCacheEntry``。"""

    models: list[str] = field(default_factory=list)
    err: BaseException | None = None
    expires_at: float = 0.0
    #: 最近一次成功的结果。刷新失败时合并回来，避免菜单变空。
    last_good: list[str] = field(default_factory=list)

    def fresh(self, now: float) -> bool:
        """对应 Go ``fresh``。"""
        return now < self.expires_at


class ModelListCache:
    """按 provider 的模型列表缓存 + 在途合并。对应 Go ``modelRuntimeState`` 的三件套。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        success_ttl: float = MODEL_LIST_SUCCESS_TTL,
        error_ttl: float = MODEL_LIST_ERROR_TTL,
    ) -> None:
        self.clock = clock
        self.success_ttl = success_ttl
        self.error_ttl = error_ttl
        self.cache: dict[str, ModelListCacheEntry] = {}
        self.inflight: dict[str, "asyncio.Task[tuple[list[str], BaseException | None]]"] = {}
        self.lock = asyncio.Lock()

    def ttl_for(self, err: BaseException | None) -> float:
        return self.error_ttl if err is not None else self.success_ttl

    async def load(
        self, provider: str, fresh: bool, fetch: FetchModels
    ) -> tuple[list[str], BaseException | None]:
        """取某个 provider 的模型列表（必要时并发合并地刷新）。"""
        async with self.lock:
            entry = self.cache.get(provider)
            if not fresh and entry is not None and entry.fresh(self.clock()):
                return list(entry.models), entry.err
            # 同一 provider 上的并发刷新合并成一次上游调用。
            task = self.inflight.get(provider)
            if task is None:
                task = asyncio.ensure_future(self._run(provider, fetch, entry))
                self.inflight[provider] = task
        # shield：等待者被取消时共享调用继续跑完，其他等待者仍能拿到结果。
        return await asyncio.shield(task)

    async def _run(
        self,
        provider: str,
        fetch: FetchModels,
        previous: ModelListCacheEntry | None,
    ) -> tuple[list[str], BaseException | None]:
        try:
            models, err = await fetch(provider)
        except asyncio.CancelledError:
            await self._forget(provider)
            raise
        except Exception as exc:  # noqa: BLE001 - Go 侧错误是返回值，不中断其他 provider
            models, err = [], exc
        models = [str(name) for name in models]
        if err is not None:
            # 保留 last-known-good：provider 不可达时仍显示它上次列出的模型。
            models = merge_model_names(models, previous.last_good if previous else [])
        entry = ModelListCacheEntry(
            models=list(models),
            err=err,
            expires_at=self.clock() + self.ttl_for(err),
            last_good=list(previous.last_good) if previous else [],
        )
        if err is None:
            entry.last_good = list(models)
        async with self.lock:
            self.cache[provider] = entry
            self.inflight.pop(provider, None)
        return list(models), err

    async def _forget(self, provider: str) -> None:
        async with self.lock:
            self.inflight.pop(provider, None)

    def clear(self) -> None:
        """清空缓存（配置热重载后用；上游没有对应方法）。"""
        self.cache.clear()


def merge_model_names(current: Iterable[str], last_good: Iterable[str]) -> list[str]:
    """当前与 last-known-good 的排序并集。对应 Go ``mergeModelNames``。"""
    last_good = list(last_good)
    if not last_good:
        return list(current)
    seen: set[str] = set()
    merged: list[str] = []
    for name in list(current) + last_good:
        name = str(name)
        if not name or name in seen:
            continue
        seen.add(name)
        merged.append(name)
    merged.sort()
    return merged


class ModelListMixin:
    """``internal/agent/model.go`` 里模型列举相关的方法。

    需要的宿主成员：``self.model_runtime``（:class:`atri_x.agent.model.ModelRuntimeState`）、
    ``self.client_for_provider(name)``、``self.model_for_mode(mode)``、
    ``self.model_mode_marks(modes)``、``self.configured_naming_model()``、
    ``self.compact_selection_for_session(session)``（:mod:`atri_x.agent.context_runtime`）、
    ``self.model_profiles`` / ``self.model_aliases``。
    """

    async def models(self, query: str = "") -> list[ModelOption]:
        """兼容入口：``/model`` 列表（不强制刷新）。对应 Go ``Models``。"""
        return (await self.model_list(query, ModelListOptions())).options

    async def model_list(
        self, query: str = "", opts: ModelListOptions | None = None
    ) -> ModelListResult:
        """对应 Go ``ModelList``。"""
        opts = opts or ModelListOptions()
        return await self.model_options(query, opts)

    async def model_options(
        self, query: str = "", opts: ModelListOptions | None = None
    ) -> ModelListResult:
        """列举所有 provider 的模型并标注当前选择。对应 Go ``modelOptions``。"""
        opts = opts or ModelListOptions()
        query = (query or "").strip().lower()
        runtime = ensure_model_runtime(self)
        providers = sorted(runtime.providers.keys())

        results = await asyncio.gather(
            *(
                self.cached_provider_models(provider, opts.fresh)
                for provider in providers
            ),
            return_exceptions=True,
        )
        models_by_provider: list[list[str]] = []
        errors_by_provider: list[BaseException | None] = []
        for provider, result in zip(providers, results):
            if isinstance(result, BaseException):
                models_by_provider.append([])
                errors_by_provider.append(result)
                continue
            models, err = result
            models_by_provider.append(list(models))
            errors_by_provider.append(err)

        chat = self.model_for_mode(SessionModeChat)
        work = self.model_for_mode(SessionModeWork)
        mode_marks = self.model_mode_marks(list(MODEL_MODE_MARKS))
        from atri_x.agent.context_runtime import compact_selection_for_session

        compact = compact_selection_for_session(self, None)
        naming = self.configured_naming_model()

        options: list[ModelOption] = []
        index = 1
        for i, provider in enumerate(providers):
            for model in models_by_provider[i]:
                option = ModelOption(
                    index=index,
                    provider=provider,
                    model=model,
                    chat_current=provider == chat.provider and model == chat.model,
                    work_current=provider == work.provider and model == work.model,
                    mode_marks=list(
                        mode_marks.get(f"{provider}\x00{model}", [])
                    ),
                    compact=provider == compact.provider and model == compact.model,
                    naming=provider == naming.provider and model == naming.model,
                )
                option.current = len(option.mode_marks) > 0
                index += 1
                if (
                    query
                    and query not in provider.lower()
                    and query not in model.lower()
                ):
                    continue
                options.append(option)

        runtime.all_models = [option.model for option in options]
        errors = [
            ModelProviderError(provider=provider, err=errors_by_provider[i])
            for i, provider in enumerate(providers)
            if errors_by_provider[i] is not None
        ]
        return ModelListResult(options=options, errors=errors)

    async def cached_provider_models(
        self, provider_name: str, fresh: bool = False
    ) -> tuple[list[str], BaseException | None]:
        """带缓存与并发合并的 provider 列举。对应 Go ``cachedProviderModels``。"""
        cache = ensure_model_runtime(self).model_list
        return await cache.load(provider_name, fresh, self._fetch_provider_models)

    async def _fetch_provider_models(
        self, provider_name: str
    ) -> tuple[list[str], BaseException | None]:
        return await sorted_provider_models(self, provider_name)

    def model_profiles(self) -> list[ModelProfile]:
        """列出命名模型档。对应 Go ``Agent.ModelProfiles``。

        注意：Agent 自己持有同名字段 ``model_profiles``（字典），实例字段会盖住本方法；
        命令层请用 :class:`atri_x.agent.model.ModelService` 门面，或直接调
        :func:`model_profiles_of`。本方法只为「宿主没有同名字段」的场景保留。
        """
        return model_profiles_of(host_of(self))


def model_profiles_of(agent: Any) -> list[ModelProfile]:
    """列出 ``services.toml`` 的命名模型选择。对应 Go ``Agent.ModelProfiles``。

    以**别名表的键**为准列出（别名先于同名 profile 解析，正是用户在 ``@model:<名字>``
    里实际输入的名字），没有任何别名指向的 profile 也补列出来；``available`` 反映该
    provider 在当前进程里是否真的有客户端 —— 没有客户端的名字用不了，列出来会被误认为
    可用。
    """
    profiles = getattr(agent, "model_profiles", None)
    aliases = getattr(agent, "model_aliases", None)
    profiles = profiles if isinstance(profiles, Mapping) else {}
    aliases = aliases if isinstance(aliases, Mapping) else {}
    client_for = getattr(agent, "client_for_provider", None)

    out: list[ModelProfile] = []
    seen: set[str] = set()

    def available(provider: str) -> bool:
        if not callable(client_for):
            return False
        try:
            return client_for(provider) is not None
        except Exception:  # noqa: BLE001 - 缺客户端的 provider 视为不可用
            return False

    def append_profile(name: str, selection: Any) -> None:
        name = str(name or "").strip()
        key = name.lower()
        provider = str(getattr(selection, "provider", "") or "")
        model = str(getattr(selection, "model", "") or "")
        if not name or key in seen or not provider or not model:
            return
        seen.add(key)
        out.append(
            ModelProfile(
                name=name,
                provider=provider,
                model=model,
                available=available(provider),
            )
        )

    for alias, name in aliases.items():
        selection = profiles.get(str(name or "").strip())
        if selection is None:
            continue
        append_profile(str(alias).strip().lower(), selection)
    # 没有被任何别名指向的 profile 仍要列出来。
    for name, selection in profiles.items():
        append_profile(str(name), selection)
    out.sort(key=lambda profile: profile.name)
    return out


async def sorted_provider_models(
    host: Any, provider_name: str
) -> tuple[list[str], BaseException | None]:
    """列出单个 provider 的模型（API 结果 ∪ 本地配置）。对应 Go ``sortedProviderModels``。"""
    runtime = ensure_model_runtime(host)
    provider = runtime.providers.get(provider_name)
    if provider is None:
        return [], None

    names: set[str] = set()
    try:
        client = host.client_for_provider(provider_name)
    except Exception:  # noqa: BLE001 - Agent 的接缝版会对未知 provider 抛错
        client = None
    _, current_provider_name, _ = runtime.selection()
    base_url = str(getattr(provider, "base_url", "") or "")
    api_key = str(getattr(provider, "api_key", "") or "")
    api_key_env = str(getattr(provider, "api_key_env", "") or "")
    fetch_from_api = provider_name == current_provider_name or (
        base_url != "" and api_key != ""
    )

    fetch_err: BaseException | None = None
    if api_key.strip() == "" and api_key_env.strip() != "" and base_url.strip() != "":
        fetch_err = InvalidError(f'api_key_env "{api_key_env}" is not set')
    elif fetch_from_api:
        if client is None:
            fetch_err = InvalidError(f'provider "{provider_name}" has no client')
        else:
            try:
                fetched = await _list_models_with_timeout(client, None)
            except Exception as exc:  # noqa: BLE001 - 列举失败只降级，不丢本地配置
                fetch_err = exc
            else:
                names.update(str(name) for name in fetched)
    names.update(str(name) for name in (getattr(provider, "models", None) or []))
    return sorted(names), fetch_err


async def _list_models_with_timeout(client: Any, ctx: Any) -> list[str]:
    """调 ``client.list_models``，按 Go 的 5 秒超时。"""
    lister = getattr(client, "list_models", None)
    if not callable(lister):
        return []
    result = lister(ctx)
    if not hasattr(result, "__await__"):
        return list(result or [])
    return list(await asyncio.wait_for(result, MODEL_LIST_FETCH_TIMEOUT) or [])


def provider_names_in_order(providers: dict[str, Any] | None) -> list[str]:
    """provider 名排序（``modelOptions`` 的第一步）。"""
    return sorted((providers or {}).keys())


def model_profile_name_exists(host: Any, name: str) -> bool:
    """这个名字是否是静态模型选择（profile 名或它的别名）。对应 Go ``modelProfileNameExists``。"""
    name = (name or "").strip()
    if not name:
        return False
    agent = host_of(host)
    profiles = getattr(agent, "model_profiles", None)
    if isinstance(profiles, Mapping) and name in profiles:
        return True
    aliases = getattr(agent, "model_aliases", None)
    if isinstance(aliases, Mapping) and name.lower() in aliases:
        return True
    return False


def filter_model_options(
    options: Sequence[ModelOption], query: str
) -> list[ModelOption]:
    """按 provider/model 子串过滤（大小写不敏感）。对应 Go ``modelOptions`` 的 query 分支。"""
    query = (query or "").strip().lower()
    if not query:
        return list(options)
    return [
        option
        for option in options
        if query in option.provider.lower() or query in option.model.lower()
    ]


__all__ = [
    "FetchModels",
    "MODEL_LIST_ERROR_TTL",
    "MODEL_LIST_FETCH_TIMEOUT",
    "MODEL_LIST_METHODS",
    "MODEL_LIST_SUCCESS_TTL",
    "MODEL_MODE_MARKS",
    "ModelListCache",
    "ModelListCacheEntry",
    "ModelListMixin",
    "filter_model_options",
    "merge_model_names",
    "model_list_cache_ttl",
    "model_profile_name_exists",
    "model_profiles_of",
    "provider_names_in_order",
    "sorted_provider_models",
]
