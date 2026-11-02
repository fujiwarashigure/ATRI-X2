"""桌面端「沙箱」面板：后端连接、资源档案、队列、环境列表与重置入口。

修改清单 §15 要求 Windows 桌面端能看到后端连接/资源/队列/环境列表并执行重置，
且「后端未安装时明确显示沙箱不可用，不能显示已保护」。本面板因此只做**实测**：

* 「刷新状态」真的去问引擎（``docker version``）并列出带 ``atrix-sandbox`` 标签的
  环境；状态行不会因为配置里写着 ``container`` 就显示「已保护」；
* 「运行隔离自检」跑与安装验收相同的探测容器（只读根 / 无 capability / 无网络 /
  内存与 pids 限额 / 无 docker.sock）；
* 「停止全部环境」「重置本机环境（保留文件 / 清空文件）」直接调用沙箱服务；
  清空文件需要二次确认。

所有引擎调用都在后台线程（:func:`atri_x.gui.widgets.run_async`），Tk 主线程不阻塞。
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import widgets
from atri_x.gui.tabs.base import confirm, message

__all__ = ["SandboxPanel"]


class SandboxPanel(ttk.Frame):
    """「进阶」页里的沙箱状态/重置面板。"""

    def __init__(self, parent: Any, paths: Any) -> None:
        super().__init__(parent)
        self.paths = paths
        self._status = tk.StringVar(value="未检查：点「刷新状态」实测后端与隔离能力。")
        self._build()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        ttk.Label(self, text="用户安全域沙箱（命令/文件在按租户复用的容器里执行）").pack(
            anchor="w"
        )
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(6, 4))
        ttk.Button(bar, text="刷新状态", command=self.refresh).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bar, text="运行隔离自检", command=self.selftest).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bar, text="停止全部环境", command=self.stop_all).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bar, text="重置（保留文件）", command=lambda: self.reset("rebuild")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bar, text="重置（清空文件）", command=lambda: self.reset("wipe")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self._text = tk.Text(self, height=11, wrap="word", state="disabled")
        self._text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        ttk.Label(self, textvariable=self._status, foreground="#555555").pack(
            anchor="w", pady=(4, 0)
        )

    # -- 动作 ---------------------------------------------------------------
    def refresh(self) -> None:
        """后台实测后端状态并刷新文本。"""
        self._status.set("正在检查沙箱后端…")
        widgets.run_async(self, self._work_status, self._show_status)

    def selftest(self) -> None:
        """跑隔离能力探测（与 ``python -m atri_x.sandbox.doctor`` 同一套）。"""
        self._status.set("正在运行隔离自检（会启动一个探测容器）…")
        widgets.run_async(self, self._work_selftest, self._show_selftest)

    def stop_all(self) -> None:
        """停止全部沙箱环境（紧急停止）。"""
        if not confirm(self, "停止沙箱环境", "停止全部租户的沙箱环境？工作区数据会保留。"):
            return
        self._status.set("正在停止全部沙箱环境…")
        widgets.run_async(self, self._work_stop_all, self._show_action)

    def reset(self, mode: str) -> None:
        """重置本机（local 租户）环境；``wipe`` 需要二次确认。"""
        label = "清空全部文件并重建" if mode == "wipe" else "保留文件重建"
        if mode == "wipe":
            ok = confirm(
                self,
                "清空沙箱环境",
                "清空本机沙箱环境的**全部文件**并重建？\n流量与审计账本不会清零。",
            )
            if not ok:
                return
        self._status.set(f"正在{label}本机环境…")
        widgets.run_async(
            self,
            lambda: self._work_reset(mode),
            lambda result, error: self._show_action(result, error, label),
        )

    # -- 后台工作线程 -------------------------------------------------------
    def _work_status(self) -> dict:
        service = self._service()
        return asyncio.run(self._status_snapshot(service))

    async def _status_snapshot(self, service: Any) -> dict:
        return await service.status(refresh=True)

    def _work_selftest(self) -> dict:
        from atri_x.sandbox.doctor import check

        cfg = self._config()
        return asyncio.run(check(getattr(cfg, "sandbox", None), probe=True))

    def _work_stop_all(self) -> int:
        service = self._service()
        return int(asyncio.run(service.stop_all(reason="gui")) or 0)

    def _work_reset(self, mode: str) -> dict:
        service = self._service()
        return asyncio.run(service.reset(mode=mode))

    # -- 回调（Tk 主线程） --------------------------------------------------
    def _show_status(self, status: Any, error: BaseException | None) -> None:
        if error is not None:
            self._fail(error)
            return
        lines = [_format_status(status if isinstance(status, dict) else {})]
        self._write("\n".join(lines))
        backend = (status or {}).get("backend") or {}
        self._status.set(
            "后端可用：" + str(backend.get("backend") or "?")
            if backend.get("available")
            else "沙箱不可用：" + str(backend.get("error") or "未找到容器引擎")
        )

    def _show_selftest(self, report: Any, error: BaseException | None) -> None:
        if error is not None:
            self._fail(error)
            return
        data = report if isinstance(report, dict) else {}
        lines = ["隔离自检结论：" + ("通过" if data.get("ok") else "未通过")]
        for item in data.get("checks") or []:
            mark = "OK" if item.get("ok") else "FAIL"
            lines.append(f"[{mark}] {item.get('name')} — {item.get('detail', '')}")
        self._write("\n".join(lines))
        self._status.set("隔离自检通过" if data.get("ok") else "隔离自检未通过（沙箱会 fail-closed 拒绝执行）")

    def _show_action(self, result: Any, error: BaseException | None, label: str = "") -> None:
        if error is not None:
            self._fail(error)
            return
        if isinstance(result, dict):
            text = str(result.get("message") or label or "完成")
            text += f"（模式 {result.get('mode')}）"
        else:
            text = f"已停止 {result} 个环境" if result else "没有正在运行的环境"
        self._write(text)
        self._status.set(text)

    # -- 工具 ---------------------------------------------------------------
    def _config(self) -> Any:
        from atri_x.config import load_from_dir

        paths = self.paths
        config_dir = getattr(paths, "config_dir", "") if paths is not None else ""
        if not config_dir:
            raise RuntimeError("找不到配置目录：请先用桌面端启动一次")
        return load_from_dir(config_dir)

    def _service(self) -> Any:
        from atri_x.sandbox.manager import SandboxService
        from atri_x.sandbox.settings import from_config

        cfg = self._config()
        settings = from_config(getattr(cfg, "sandbox", None))
        if not settings.enabled:
            raise RuntimeError("沙箱未启用（[sandbox].mode=off）：当前是显式宿主 Shell 模式")
        return SandboxService(settings)

    def _write(self, text: str) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", tk.END)
        self._text.insert("1.0", text)
        self._text.configure(state="disabled")

    def _fail(self, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {exc}"
        self._write("失败：" + text)
        self._status.set("失败：" + text)
        message(self, "沙箱操作失败", text, error=True)


def _format_status(status: dict) -> str:
    """把 ``SandboxService.status`` 快照渲染成面板文本。"""
    settings = status.get("settings") or {}
    backend = status.get("backend") or {}
    queue = status.get("queue") or {}
    lines = [
        "模式：{mode}｜资源档：{profile}｜网络档：{network}".format(
            mode=settings.get("mode", "?"),
            profile=settings.get("profile", "?"),
            network=settings.get("network", "?"),
        ),
        "后端：{bm} {stat}".format(
            bm=backend.get("backend", "?"),
            stat="可用" if backend.get("available") else "不可用（" + str(backend.get("error") or "") + "）",
        ),
    ]
    probe = status.get("probe")
    if probe:
        lines.append(
            "隔离探测：" + ("通过" if probe.get("ok") else "未通过")
            + f"（{probe.get('checked_at', '')}）"
        )
    else:
        lines.append("隔离探测：尚未运行")
    lines.append(
        "准入队列：运行 {running}/{capacity}，等待 {waiting}，本租户 {per}".format(
            running=queue.get("running", 0),
            capacity=queue.get("capacity", 0),
            waiting=queue.get("waiting", 0),
            per=queue.get("per_tenant") or {},
        )
    )
    environments = status.get("environments") or []
    if environments:
        lines.append("环境列表：")
        for item in environments:
            lines.append(
                "  - {tenant}｜{state}｜空闲 {idle}s｜网络 {network}｜上/下行 {up}/{down} B".format(
                    tenant=item.get("tenant") or item.get("tenant_slug") or "?",
                    state="运行" if item.get("running") else "停止",
                    idle=item.get("idle_seconds", 0),
                    network=item.get("network", "?"),
                    up=item.get("proxy_up_bytes", 0),
                    down=item.get("proxy_down_bytes", 0),
                )
            )
    else:
        lines.append("环境列表：（当前没有沙箱环境）")
    ledger = status.get("ledger") or {}
    tenants = ledger.get("tenants") or {}
    if tenants:
        used = sum(int(item.get("bytes_total", 0) or 0) for item in tenants.values())
        lines.append(f"累计流量：{used / 1048576:.1f} MiB（{len(tenants)} 个租户；重置不清零）")
    return "\n".join(lines)
