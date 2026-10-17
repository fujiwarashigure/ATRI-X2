"""Skill 管理器：装载、重载、删除与 AgentSkill 清单写入。

对应 Go：``internal/tool/skill/manager.go``。

``EnsureLoaded`` 是全仓唯一的「首次装载去重」入口：并发调用只会触发一次扫描，
其余等待同一个结果（Go 用 ``done chan struct{}``，Python 用 ``asyncio.Future``）。
``WriteAgentSkillConfig`` 在重载失败时把清单文件回滚到写入前的内容。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from atri_x.processenv import Environment
from atri_x.utils.fileops import atomic_write_file
from atri_x.tool.skill.agent_manifest import (
    AGENT_SKILL_CONFIG_FILE,
    agent_skill_config_path,
    parse_agent_skill_manifest,
)
from atri_x.tool.skill.catalog import Catalog, Kind
from atri_x.tool.skill.scanner import FilesystemScanner, new_filesystem_scanner

__all__ = [
    "Manager",
    "FileSnapshot",
    "new_manager",
]


@dataclass
class FileSnapshot:
    """一个文件在写入前的内容快照。对应 Go ``fileSnapshot``。"""

    exists: bool = False
    data: bytes = b""
    mode: int = 0o644

    def restore(self, path: str) -> None:
        """回滚到快照状态。对应 Go ``fileSnapshot.restore``。"""
        if self.exists:
            atomic_write_file(path, self.data, self.mode)
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def capture_file(path: str) -> FileSnapshot:
    """读一个文件的内容与权限位。对应 Go ``captureFile``。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return FileSnapshot()
    except OSError as exc:
        raise OSError(f"read existing {AGENT_SKILL_CONFIG_FILE}: {exc}") from exc
    try:
        info = os.stat(path)
    except OSError as exc:
        raise OSError(f"stat existing {AGENT_SKILL_CONFIG_FILE}: {exc}") from exc
    return FileSnapshot(exists=True, data=data, mode=info.st_mode & 0o777)


class Manager:
    """一个 skill 根目录的管理器。对应 Go ``Manager``。"""

    def __init__(
        self,
        root: str,
        registry: Any = None,
        environment: Environment | None = None,
        scanner: FilesystemScanner | None = None,
    ) -> None:
        if scanner is None:
            scanner = new_filesystem_scanner(root)
        self.root = scanner.root
        self.catalog: Catalog = scanner.catalog
        self.scanner = scanner
        self.registry = registry
        self.process_env = environment
        # Go 的 reloadMu / mu 都是 sync.Mutex；这里两条路径都在事件循环里，
        # 用 asyncio.Lock（PORTING.md §1.6）。
        self._reload_mu = asyncio.Lock()
        self._mu = asyncio.Lock()
        self._loaded = False
        self._loading = False
        self._done: asyncio.Future | None = None
        self._last_err: BaseException | None = None

    # -- 状态 ---------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def last_error(self) -> BaseException | None:
        return self._last_err

    async def _set_reload_result(self, err: BaseException | None) -> None:
        async with self._mu:
            self._loaded = err is None
            self._last_err = err

    # -- 装载 ---------------------------------------------------------------

    async def reload(self, ctx: Any) -> None:
        """强制重扫。对应 Go ``Manager.Reload``。"""
        if self.scanner.catalog is None:
            return
        async with self._reload_mu:
            try:
                await self.scanner.reload(ctx, self.registry)
                err: BaseException | None = None
            except BaseException as exc:  # noqa: BLE001 - 失败要记录而不是丢失
                err = exc
            await self._set_reload_result(err)
        if err is not None:
            raise err

    async def ensure_loaded(self, ctx: Any) -> None:
        """首次装载（并发去重）。对应 Go ``Manager.EnsureLoaded``。

        只有第一个调用者真正扫描，其余等同一个完成信号；等待期间 ``ctx`` 取消
        就立刻返回。失败时把同一个错误抛给所有等待者（与 Go 返回 ``lastErr``
        一致）。
        """
        async with self._mu:
            if self._loaded:
                if self._last_err is not None:
                    raise self._last_err
                return
            done = self._done
            if self._loading and done is not None:
                leader = False
            else:
                leader = True
                self._loading = True
                done = asyncio.get_running_loop().create_future()
                self._done = done
        if not leader:
            await _wait_cancellable(ctx, done)
            async with self._mu:
                err = self._last_err
            if err is not None:
                raise err
            return

        err: BaseException | None = None
        try:
            async with self._reload_mu:
                await self.scanner.reload(ctx, self.registry)
        except BaseException as exc:  # noqa: BLE001 - 失败要记录给等待者
            err = exc
        async with self._mu:
            self._loaded = err is None
            self._loading = False
            self._last_err = err
            if not done.done():
                done.set_result(None)
        if err is not None:
            raise err

    def start_delayed_reload(self, ctx: Any, delay: float) -> asyncio.Task:
        """延迟装载（Go 的 ``go func()``）。对应 Go ``StartDelayedReload``。

        必须在事件循环里调用（返回创建好的 Task，调用方可以留引用或不管）。
        """

        async def _run() -> None:
            if delay > 0:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
            try:
                await self.ensure_loaded(ctx)
            except BaseException:  # noqa: BLE001 - 后台任务不向外抛
                return

        return asyncio.get_running_loop().create_task(_run())

    async def remove(self, ctx: Any, name: str) -> None:
        """删除一个 skill。对应 Go ``Manager.Remove``。"""
        async with self._reload_mu:
            try:
                await self.scanner.remove(ctx, self.registry, name)
                err: BaseException | None = None
            except BaseException as exc:  # noqa: BLE001
                err = exc
            await self._set_reload_result(err)
        if err is not None:
            raise err

    # -- AgentSkill 清单 ----------------------------------------------------

    async def write_agent_skill_config(self, ctx: Any, name: str, content: str) -> None:
        """写入 AgentSkill 清单，失败时回滚。对应 Go ``WriteAgentSkillConfig``。"""
        from atri_x.tool.skill.creator import validate_skill_name

        if self.catalog is None or self.registry is None:
            raise ValueError("skill manager is not configured")
        name = (name or "").strip()
        validate_skill_name(name)
        if (content or "").strip() == "":
            raise ValueError("toml is required for write")
        parse_agent_skill_manifest(content)

        async with self._reload_mu:
            _raise_if_cancelled(ctx)
            record, ok = self.catalog.get(name)
            if not ok or record is None or record.kind != Kind.AGENT:
                raise ValueError(f"AgentSkill {name!r} not found")
            path = agent_skill_config_path(record.root)
            previous = capture_file(path)
            atomic_write_file(path, content.encode("utf-8"), _existing_file_mode(path))
            try:
                await self.scanner.reload(ctx, self.registry)
            except BaseException as exc:  # noqa: BLE001
                try:
                    previous.restore(path)
                except BaseException as rollback_exc:  # noqa: BLE001
                    combined = RuntimeError(
                        f"reload and restore previous {AGENT_SKILL_CONFIG_FILE} failed: "
                        f"{exc}; rollback: {rollback_exc}"
                    )
                    await self._set_reload_result(combined)
                    raise combined from exc
                failure = RuntimeError(
                    f"reload failed; restored previous {AGENT_SKILL_CONFIG_FILE}: {exc}"
                )
                await self._set_reload_result(failure)
                raise failure from exc
            await self._set_reload_result(None)


def new_manager(
    root: str,
    registry: Any = None,
    environment: Environment | None = None,
) -> Manager:
    """对应 Go ``NewManager``。"""
    return Manager(root=root, registry=registry, environment=environment)


def _existing_file_mode(path: str) -> int:
    """已存在文件的权限位，否则 ``0o644``。对应 Go ``existingFileMode``。"""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return 0o644


async def _wait_cancellable(ctx: Any, done: asyncio.Future) -> None:
    """等完成信号，或 ``ctx`` 先被取消。对应 Go 的 ``select { done / ctx.Done() }``。"""
    if ctx is None:
        await asyncio.shield(done)
        return
    if ctx.is_cancelled():
        _raise_if_cancelled(ctx)
        return
    loop = asyncio.get_running_loop()
    cancelled: asyncio.Future = loop.create_future()

    def _on_cancel() -> None:
        if not cancelled.done():
            cancelled.set_result(None)

    remove = ctx.add_callback(_on_cancel)
    try:
        await asyncio.wait({done, cancelled}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        remove()
        if not cancelled.done():
            cancelled.cancel()
    if not done.done():
        _raise_if_cancelled(ctx)


def _raise_if_cancelled(ctx: Any) -> None:
    if ctx is None:
        return
    if ctx.is_cancelled():
        error = ctx.error()
        if error is not None:
            raise error
        from atri_x.safecontext import CancelledError

        raise CancelledError("context canceled")
