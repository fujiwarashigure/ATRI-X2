"""「进阶」页生图区的本地挂载面板（:class:`MountPanelMixin`）。

ATRI-X 的生图客户端只认 OpenAI 兼容端点，自己部署生图时地址通常是「端口映射」或
「SSL 链接」两种形态之一（见 :mod:`atri_x.gui.imagemount`）。这块界面就是围绕
那两种形态的输入 + 一个只读探测按钮，最后把结果填进生图区的 Base URL。

做成 mixin 而不是独立控件，是因为它要读写宿主 ``AdvancedTab`` 的一堆成员：
``image_base_url`` / ``image_model`` / ``_stored_image_model`` / ``_catalog`` /
``_image_label`` / ``_refresh_image_models``，以及 ``require_paths`` / ``fail`` /
``notify`` / ``journal`` / ``frame``。宿主只要继承本 mixin 并在生图框里调用
``self._build_mount_box(box, row=...)`` 即可。

控件前缀统一 ``image_mount*`` / ``mount_*``，避免和生图区原有变量重名。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import imagemount, modelcatalog, widgets
from atri_x.gui.configmodel import secret_value

__all__ = ["MountPanelMixin"]


class MountPanelMixin:
    """本地生图服务挂载面板。"""

    # -- 构建 ---------------------------------------------------------------
    def _build_mount_box(self, box: Any, *, row: int) -> None:
        """「本地生图服务挂载」：端口映射 / SSL 链接 → OpenAI 兼容 Base URL。"""
        frame = ttk.LabelFrame(box, text="本地生图服务挂载（端口映射 / SSL 链接）", padding=6)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        frame.columnconfigure(1, weight=1)

        self.image_access = tk.StringVar(value=imagemount.MOUNT_EXTERNAL)
        access_row = ttk.Frame(frame)
        access_row.grid(row=0, column=0, columnspan=3, sticky="w")
        for value, label in (
            (imagemount.MOUNT_EXTERNAL, "外部 API"),
            ("local", "本地服务"),
        ):
            ttk.Radiobutton(
                access_row,
                text=label,
                value=value,
                variable=self.image_access,
                command=self._mount_changed,
            ).pack(side=tk.LEFT, padx=(0, 10))

        self.image_mount = tk.StringVar(value=imagemount.MOUNT_PORT)
        self.image_mount_host = tk.StringVar(value=imagemount.DEFAULT_HOST)
        self.image_mount_port = tk.StringVar(value=str(imagemount.DEFAULT_COMFY_PORT))
        self.image_mount_url = tk.StringVar()
        self.image_mount_prefix = tk.StringVar(value=imagemount.DEFAULT_PREFIX)

        self._local_frame = ttk.Frame(frame)
        self._local_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._local_frame.columnconfigure(1, weight=1)

        mount_row = ttk.Frame(self._local_frame)
        mount_row.grid(row=0, column=0, columnspan=3, sticky="w")
        for value, label in (
            (imagemount.MOUNT_PORT, "端口映射"),
            (imagemount.MOUNT_SSL, "SSL 链接"),
        ):
            ttk.Radiobutton(
                mount_row,
                text=label,
                value=value,
                variable=self.image_mount,
                command=self._mount_changed,
            ).pack(side=tk.LEFT, padx=(0, 10))

        self._host_row = ttk.Frame(self._local_frame)
        self._host_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Label(self._host_row, text="主机").pack(side=tk.LEFT)
        ttk.Entry(self._host_row, textvariable=self.image_mount_host, width=14).pack(
            side=tk.LEFT, padx=(4, 10)
        )
        ttk.Label(self._host_row, text="端口").pack(side=tk.LEFT)
        ttk.Entry(self._host_row, textvariable=self.image_mount_port, width=7).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        self._ssl_row = ttk.Frame(self._local_frame)
        self._ssl_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Label(self._ssl_row, text="SSL 链接").pack(side=tk.LEFT)
        ttk.Entry(self._ssl_row, textvariable=self.image_mount_url).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0)
        )

        prefix_row = ttk.Frame(self._local_frame)
        prefix_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Label(prefix_row, text="接口前缀").pack(side=tk.LEFT)
        ttk.Entry(prefix_row, textvariable=self.image_mount_prefix, width=12).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        ttk.Button(prefix_row, text="填入 Base URL", command=self._apply_mount).pack(side=tk.LEFT)
        ttk.Button(prefix_row, text="检测服务", command=self._probe_mount).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.mount_state = ttk.Label(
            self._local_frame, text="", foreground="#555555", wraplength=380, justify=tk.LEFT
        )
        self.mount_state.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(
            self._local_frame,
            text=(
                "端口映射默认 ComfyUI 8188（SSH 隧道 / frp 也映射到同一端口）；"
                "AUTODL 画图镜像走 HTTPS 自定义服务链接。ATRI-X 需要 OpenAI 兼容端点"
                "（/images/generations），ComfyUI 原生接口要先加一层转接。"
            ),
            foreground="#8a8a8a",
            wraplength=380,
            justify=tk.LEFT,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self._mount_changed()

    # -- 交互 ---------------------------------------------------------------
    def _mount_changed(self) -> None:
        """接入方式 / 挂载方式切换：显示对应的输入框。"""
        local = self.image_access.get() == "local"
        if local:
            self._local_frame.grid()
            port_mode = self.image_mount.get() == imagemount.MOUNT_PORT
            if port_mode:
                self._host_row.grid()
                self._ssl_row.grid_remove()
            else:
                self._host_row.grid_remove()
                self._ssl_row.grid()
        else:
            self._local_frame.grid_remove()
        try:
            self.mount_state.configure(text="")
        except (AttributeError, tk.TclError):  # pragma: no cover - 构建期
            pass

    def _mount_spec(self) -> imagemount.MountSpec:
        if self.image_access.get() != "local":
            return imagemount.MountSpec(
                mode=imagemount.MOUNT_EXTERNAL, url=self.image_base_url.get()
            )
        return imagemount.MountSpec(
            mode=self.image_mount.get(),
            host=self.image_mount_host.get(),
            port=_int_or(self.image_mount_port.get(), imagemount.DEFAULT_COMFY_PORT),
            url=self.image_mount_url.get(),
            prefix=self.image_mount_prefix.get(),
        )

    def _apply_mount(self) -> None:
        """按当前挂载方式生成 Base URL 并填进生图区。"""
        try:
            spec = self._mount_spec()
            base = imagemount.build_base_url(spec)
        except (imagemount.ImageMountError, ValueError) as exc:
            self.fail(f"生成 Base URL 失败：{exc}")
            return
        self.image_base_url.set(base)
        self.mount_state.configure(text=f"Base URL 已填入：{base}")
        self.journal(f"生图本地挂载（{spec.describe()}）→ {base}")
        self.notify(f"Base URL = {base}；确认后点「保存进阶配置」写进 services.toml")

    def _probe_mount(self) -> None:
        """后台探测当前挂载地址后面是什么服务（只读）。"""
        if not self.is_ready():  # type: ignore[attr-defined] - 宿主 ConfigTab 提供
            return
        try:
            spec = self._mount_spec()
            base = imagemount.build_base_url(spec)
        except (imagemount.ImageMountError, ValueError) as exc:
            self.fail(f"地址不合法：{exc}")
            return
        self.image_base_url.set(base)
        self.mount_state.configure(text="正在检测…")
        api_key = secret_value(self.require_paths(), self.image_api_env.get().strip())
        self.journal(f"检测生图服务：{base}")
        widgets.run_async(
            self.frame, lambda: imagemount.probe(base, api_key), self._mount_probed
        )

    def _mount_probed(self, result: Any, error: BaseException | None) -> None:
        if error is not None:
            self.mount_state.configure(text=f"检测失败：{error}")
            self.journal(f"检测生图服务失败：{error}")
            return
        if not isinstance(result, imagemount.ProbeResult):
            self.mount_state.configure(text="检测失败：返回结果异常")
            return
        text = imagemount.describe(result)
        self.mount_state.configure(text=text)
        self.journal(f"生图服务检测结果（{result.base_url}）：{text}")
        if result.ok and result.models:
            added = self._catalog.record(
                modelcatalog.IMAGE_KEY, list(result.models), source=result.base_url
            )
            try:
                self._catalog.save(self.require_paths().config_dir)
            except modelcatalog.CatalogError as exc:
                self.mount_state.configure(text=f"{text}；但模型目录保存失败：{exc}")
            if not self._stored_image_model and result.models:
                self._stored_image_model = result.models[0]
                self.image_model.set(self._image_label(result.models[0]))
            self._refresh_image_models()
            text += f"；已记入模型目录（新增 {len(added)}）"
        self.notify(text)

    # 宿主能力（由 ConfigTab / AdvancedTab 提供）：``require_paths`` / ``fail`` /
    # ``notify`` / ``journal`` / ``is_ready`` / ``frame`` / ``image_base_url`` /
    # ``image_model`` / ``image_api_env`` / ``_stored_image_model`` / ``_catalog`` /
    # ``_image_label`` / ``_refresh_image_models``。本 mixin 不覆盖这些成员。


def _int_or(text: str, fallback: int) -> int:
    """文本框里的整数，解析不了用默认值（端口这种给个兜底，不打断用户）。"""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return fallback
