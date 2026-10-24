"""「人设卡 → 表情图库」子选项卡（Python 侧新增，无 Go 对应）。

对应运行期 ``[reactions]`` 配置段 + ``<配置目录>/reactions/catalog.json``：

* **发送策略**（``app.toml`` 的 ``[reactions]``）：总开关、群/私聊范围、回复冷却、
  会话/群冷却、去重张数、概率、长文抑制、抑制词、图库目录、可用情绪标签；
* **图库条目**（``catalog.json``）：每张图的情绪 / 名称 / 权重 / 启用状态。

生效方式：``[reactions]`` 在 app.toml → **重启后台服务**；``catalog.json`` 由后台
按文件修改时间自动重载 → **保存即生效**。

写盘边界：``[reactions]`` 走 :func:`atri_x.gui.configedit.patch_section`（只改这一段、
保留注释）并回读 ``atri_x.config.load`` 校验；``catalog.json`` 原子替换写入，
写前用 :func:`atri_x.reaction.parse_catalog` 复核，坏条目不会被写出去。
"""

from __future__ import annotations

import json
import os
import shutil
import tkinter as tk
from dataclasses import fields
from tkinter import filedialog, ttk
from typing import Any

from atri_x.config.app_sections import ReactionsConfig
from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml
from atri_x.reaction import DEFAULT_EMOTIONS, CatalogItem, load_catalog, parse_catalog

__all__ = [
    "CATALOG_FILENAME",
    "REACTION_TAB_TITLE",
    "ReactionPanel",
    "gallery_dir_of",
    "import_gallery_image",
    "load_gallery_items",
    "load_reactions_policy",
    "save_gallery_items",
    "save_reactions_policy",
]

CATALOG_FILENAME = "catalog.json"

REACTION_TAB_TITLE = " 表情图库 "

#: 策略面板里的整数/浮点字段：``(键, 标签, 说明)``。
_NUMBER_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("cooldown_text_replies", "回复冷却（条）", "发图后至少再经过几条不带图的回复"),
    ("session_cooldown_seconds", "会话冷却（秒）", "同一会话两次发图的最短间隔"),
    ("group_cooldown_seconds", "群冷却（秒）", "同一群两次发图的最短间隔（私聊忽略）"),
    ("dedupe_recent", "去重张数", "最近多少张图不重复；0 = 不去重"),
    ("probability", "触发概率", "通过全部策略后的发送概率，0~1；1 = 一定发"),
    ("max_text_chars", "长文抑制（字）", "正文超过该字数不配图；0 = 不限制"),
)


# ---------------------------------------------------------------------------
# 数据层（无 Tk，方便单测）
# ---------------------------------------------------------------------------
def gallery_dir_of(paths: Any) -> str:
    """解析图库目录：``[reactions].gallery_dir`` 相对配置目录。"""
    section = _reactions_section(paths)
    raw = str(section.get("gallery_dir", "reactions") or "reactions").strip()
    if not raw:
        raw = "reactions"
    if os.path.isabs(raw) or os.path.splitdrive(raw)[0]:
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(paths.config_dir, raw))


def load_reactions_policy(paths: Any) -> dict[str, Any]:
    """读 ``[reactions]``（缺字段用 :class:`ReactionsConfig` 默认值）。"""
    section = _reactions_section(paths)
    config = ReactionsConfig()
    for item in fields(ReactionsConfig):
        if item.name in section:
            setattr(config, item.name, section[item.name])
    normalized = config.normalized()
    out: dict[str, Any] = {}
    for item in fields(ReactionsConfig):
        value = getattr(normalized, item.name)
        out[item.name] = list(value) if isinstance(value, (list, tuple)) else value
    return out


def save_reactions_policy(paths: Any, values: dict[str, Any]) -> None:
    """把 ``[reactions]`` 写回 app.toml 并回读校验。"""
    patch_section(
        paths.config_path,
        "reactions",
        dict(values),
        header_comment="本地表情图库（桌面端人设卡 → 表情图库）",
    )
    from atri_x.config.loader import load

    try:
        load(paths.config_path, strict=True)
    except Exception as exc:  # noqa: BLE001 - 统一转成 GUI 可展示的错误
        raise ConfigEditError(f"回读校验失败（配置会被后台服务拒绝）：{exc}") from exc


def load_gallery_items(gallery: str) -> tuple[list[dict[str, Any]], list[str]]:
    """读图库条目（路径同时给出绝对路径与相对图库目录的展示路径）。"""
    catalog = load_catalog(gallery)
    items: list[dict[str, Any]] = []
    for item in catalog.items:
        rel = os.path.relpath(item.path, gallery).replace("\\", "/")
        items.append(
            {
                "id": item.id,
                "emotion": item.emotion,
                "path": item.path,
                "rel": rel,
                "name": item.name,
                "description": item.description,
                "weight": item.weight,
                "enabled": item.enabled,
            }
        )
    return items, list(catalog.warnings)


def save_gallery_items(gallery: str, items: list[dict[str, Any]]) -> None:
    """原子写回 ``catalog.json``，写前用 :func:`parse_catalog` 复核。"""
    os.makedirs(gallery, exist_ok=True)
    payload: list[dict[str, Any]] = []
    for item in items:
        rel = str(item.get("rel") or "").replace("\\", "/").strip()
        if not rel:
            rel = os.path.relpath(str(item.get("path") or ""), gallery).replace("\\", "/")
        payload.append(
            {
                "id": str(item.get("id") or "").strip(),
                "emotion": str(item.get("emotion") or "").strip(),
                "path": rel,
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "weight": float(item.get("weight") or 1.0),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    raw = {"version": 1, "items": payload}
    parsed = parse_catalog(raw, gallery)
    if parsed.warnings:
        raise ConfigEditError("图库校验失败：" + "；".join(parsed.warnings[:4]))
    path = os.path.join(gallery, CATALOG_FILENAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def import_gallery_image(gallery: str, source: str) -> str:
    """把外部图片拷进 ``<gallery>/assets/``，返回相对图库目录的路径。"""
    src = os.path.abspath(source)
    if not os.path.isfile(src):
        raise ConfigEditError(f"图片不存在：{source}")
    assets = os.path.join(gallery, "assets")
    os.makedirs(assets, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        raise ConfigEditError(f"不支持的图片格式：{ext}")
    import hashlib

    stamp = hashlib.sha1(src.encode("utf-8", "surrogateescape")).hexdigest()[:10]
    name = "img_" + stamp + ext
    target = os.path.join(assets, name)
    if not os.path.exists(target):
        shutil.copyfile(src, target)
    return "assets/" + name


def _reactions_section(paths: Any) -> dict[str, Any]:
    try:
        data = read_toml(paths.config_path)
    except ConfigEditError:
        return {}
    section = data.get("reactions")
    return dict(section) if isinstance(section, dict) else {}


def _split_list(value: str) -> list[str]:
    text = str(value or "").replace("，", ",").replace("、", ",")
    out: list[str] = []
    for part in text.split(","):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------
class ReactionPanel:
    """「表情图库」子选项卡内容（由 :class:`CharacterTab` 持有）。"""

    def __init__(self, page: Any, parent: Any) -> None:
        self.page = page
        self.frame = ttk.Frame(parent, padding=8)
        self.enabled_var = tk.BooleanVar(value=False)
        self.group_var = tk.BooleanVar(value=True)
        self.private_var = tk.BooleanVar(value=True)
        self.number_vars: dict[str, tk.StringVar] = {
            key: tk.StringVar() for key, _label, _help in _NUMBER_FIELDS
        }
        self.emotions_var = tk.StringVar()
        self.keywords_var = tk.StringVar()
        self.gallery_label_var = tk.StringVar(value="")
        self.items: list[dict[str, Any]] = []
        self.selected_id = ""
        self.item_emotion_var = tk.StringVar()
        self.item_name_var = tk.StringVar()
        self.item_weight_var = tk.StringVar(value="1.0")
        self.item_enabled_var = tk.BooleanVar(value=True)
        self.item_path_var = tk.StringVar(value="（未选择图片）")
        self.status_var = tk.StringVar(value="")
        self._build()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        policy = ttk.LabelFrame(self.frame, text="发送策略（app.toml [reactions]）", padding=8)
        policy.pack(fill=tk.X)
        top = ttk.Frame(policy)
        top.pack(fill=tk.X)
        ttk.Checkbutton(top, text="启用表情图库", variable=self.enabled_var).pack(side=tk.LEFT)
        ttk.Checkbutton(top, text="群聊允许", variable=self.group_var).pack(
            side=tk.LEFT, padx=(12, 0)
        )
        ttk.Checkbutton(top, text="私聊允许", variable=self.private_var).pack(
            side=tk.LEFT, padx=(12, 0)
        )
        gallery_row = ttk.Frame(policy)
        gallery_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(gallery_row, text="图库目录").pack(side=tk.LEFT)
        ttk.Label(gallery_row, textvariable=self.gallery_label_var, foreground="#555555").pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(gallery_row, text="打开目录", command=self._open_gallery).pack(side=tk.RIGHT)

        grid = ttk.Frame(policy)
        grid.pack(fill=tk.X, pady=(6, 0))
        for index, (key, label, helper) in enumerate(_NUMBER_FIELDS):
            column = 0 if index % 3 == 0 else (1 if index % 3 == 1 else 2)
            row = index // 3 * 2
            ttk.Label(grid, text=label, width=14).grid(row=row, column=column, sticky="w")
            ttk.Entry(grid, textvariable=self.number_vars[key], width=10).grid(
                row=row, column=column, sticky="e"
            )
            ttk.Label(grid, text=helper, foreground="#8a8a8a").grid(
                row=row + 1, column=column, sticky="w", columnspan=1
            )
        list_row = ttk.Frame(policy)
        list_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(list_row, text="可用情绪（逗号分隔）", width=20).pack(side=tk.LEFT)
        ttk.Entry(list_row, textvariable=self.emotions_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        keyword_row = ttk.Frame(policy)
        keyword_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(keyword_row, text="抑制关键词（命中不配图）", width=20).pack(side=tk.LEFT)
        ttk.Entry(keyword_row, textvariable=self.keywords_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        policy_buttons = ttk.Frame(policy)
        policy_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(policy_buttons, text="保存发送策略", command=self.save_policy).pack(side=tk.LEFT)
        ttk.Button(policy_buttons, text="重新载入", command=self.load).pack(side=tk.LEFT, padx=(6, 0))

        gallery = ttk.LabelFrame(
            self.frame, text="图库（reactions/catalog.json，保存即生效）", padding=8
        )
        gallery.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        tree_wrap = ttk.Frame(gallery)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("id", "emotion", "name", "weight", "enabled", "file"),
            show="headings",
            height=8,
        )
        for name, title, width in (
            ("id", "id", 130),
            ("emotion", "情绪", 70),
            ("name", "名称", 110),
            ("weight", "权重", 60),
            ("enabled", "启用", 50),
            ("file", "图片", 200),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, stretch=name in ("file", "name"))
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_select())

        form = ttk.Frame(gallery)
        form.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(form, text="情绪").pack(side=tk.LEFT)
        self.emotion_combo = ttk.Combobox(
            form, textvariable=self.item_emotion_var, values=list(DEFAULT_EMOTIONS), width=8
        )
        self.emotion_combo.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(form, text="名称").pack(side=tk.LEFT)
        ttk.Entry(form, textvariable=self.item_name_var, width=14).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        ttk.Label(form, text="权重").pack(side=tk.LEFT)
        ttk.Entry(form, textvariable=self.item_weight_var, width=6).pack(
            side=tk.LEFT, padx=(4, 8)
        )
        ttk.Checkbutton(form, text="启用", variable=self.item_enabled_var).pack(side=tk.LEFT)
        path_row = ttk.Frame(gallery)
        path_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(path_row, text="图片").pack(side=tk.LEFT)
        ttk.Label(path_row, textvariable=self.item_path_var, foreground="#555555").pack(
            side=tk.LEFT, padx=(6, 0)
        )
        item_buttons = ttk.Frame(gallery)
        item_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(item_buttons, text="添加图片…", command=self._add_image).pack(side=tk.LEFT)
        ttk.Button(item_buttons, text="应用修改", command=self._apply_item).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(item_buttons, text="启用 / 停用", command=self._toggle_item).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(item_buttons, text="删除条目", command=self._remove_item).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(item_buttons, text="保存图库", command=self.save_catalog).pack(
            side=tk.RIGHT
        )
        ttk.Label(gallery, textvariable=self.status_var, foreground="#8a8a8a").pack(
            anchor="w", pady=(6, 0)
        )

    # -- 数据 ---------------------------------------------------------------
    def load(self) -> None:
        paths = self.page.require_paths()
        policy = load_reactions_policy(paths)
        self.enabled_var.set(bool(policy.get("enabled", False)))
        self.group_var.set(bool(policy.get("show_in_group", True)))
        self.private_var.set(bool(policy.get("show_in_private", True)))
        for key, _label, _help in _NUMBER_FIELDS:
            self.number_vars[key].set(str(policy.get(key, "")))
        self.emotions_var.set("、".join(policy.get("emotions") or []))
        self.keywords_var.set("、".join(policy.get("suppress_keywords") or []))
        gallery = gallery_dir_of(paths)
        self.gallery_label_var.set(gallery)
        try:
            self.items, warnings = load_gallery_items(gallery)
        except Exception as exc:  # noqa: BLE001 - 图库坏了不该让页面起不来
            self.items, warnings = [], [str(exc)]
        self._refresh_tree()
        self.status_var.set(
            f"图库目录：{gallery}；条目 {len(self.items)} 个"
            + (("；警告：" + "；".join(warnings[:3])) if warnings else "")
        )

    def save_policy(self) -> bool:
        try:
            paths = self.page.require_paths()
            values: dict[str, Any] = {
                "enabled": bool(self.enabled_var.get()),
                "gallery_dir": str(gallery_dir_of(paths)),
                "show_in_group": bool(self.group_var.get()),
                "show_in_private": bool(self.private_var.get()),
                "emotions": _split_list(self.emotions_var.get()),
                "suppress_keywords": _split_list(self.keywords_var.get()),
            }
            for key, _label, _help in _NUMBER_FIELDS:
                values[key] = _number(self.number_vars[key].get(), key)
            save_reactions_policy(paths, values)
        except ConfigEditError as exc:
            self.page.fail(exc)
            return False
        self.page.journal(f"保存表情发送策略：enabled={values['enabled']}")
        self.page.notify("已保存 [reactions]；重启后台服务后生效（catalog.json 保存即生效）")
        self.load()
        return True

    def save_catalog(self) -> bool:
        try:
            paths = self.page.require_paths()
            save_gallery_items(gallery_dir_of(paths), self.items)
        except ConfigEditError as exc:
            self.page.fail(exc)
            return False
        except OSError as exc:
            self.page.fail(ConfigEditError(f"写图库失败：{exc}"))
            return False
        self.page.journal(f"保存表情图库：{len(self.items)} 个条目")
        self.page.notify(f"已保存 {CATALOG_FILENAME}（{len(self.items)} 个条目）；保存即生效")
        self.load()
        return True

    # -- 图库条目 -----------------------------------------------------------
    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.items):
            self.tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    item.get("id", ""),
                    item.get("emotion", ""),
                    item.get("name", ""),
                    item.get("weight", 1.0),
                    "是" if item.get("enabled", True) else "否",
                    item.get("rel") or item.get("path", ""),
                ),
            )
        self.selected_id = ""

    def _on_select(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(str(selection[0]))
        if index < 0 or index >= len(self.items):
            return
        item = self.items[index]
        self.selected_id = str(item.get("id") or "")
        self.item_emotion_var.set(str(item.get("emotion") or ""))
        self.item_name_var.set(str(item.get("name") or ""))
        self.item_weight_var.set(str(item.get("weight") or 1.0))
        self.item_enabled_var.set(bool(item.get("enabled", True)))
        self.item_path_var.set(str(item.get("rel") or item.get("path") or ""))

    def _add_image(self) -> None:
        gallery = self.gallery_label_var.get()
        source = filedialog.askopenfilename(
            title="选择表情图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.gif *.webp"), ("全部文件", "*.*")],
        )
        if not source:
            return
        try:
            rel = import_gallery_image(gallery, source)
        except (ConfigEditError, OSError) as exc:
            self.page.fail(ConfigEditError(f"导入图片失败：{exc}"))
            return
        emotion = str(self.item_emotion_var.get() or "").strip() or DEFAULT_EMOTIONS[0]
        name = str(self.item_name_var.get() or "").strip() or (emotion + "表情")
        item_id = _unique_id(self.items, emotion)
        self.items.append(
            {
                "id": item_id,
                "emotion": emotion,
                "path": os.path.join(gallery, rel),
                "rel": rel,
                "name": name,
                "description": "",
                "weight": _number(self.item_weight_var.get(), "weight"),
                "enabled": bool(self.item_enabled_var.get()),
            }
        )
        self._refresh_tree()
        self.tree.selection_set(str(len(self.items) - 1))
        self.status_var.set(f"已导入 {rel}（记得点「保存图库」）")

    def _apply_item(self) -> None:
        item = self._selected_item()
        if item is None:
            self.status_var.set("先在列表里选一个条目")
            return
        item["emotion"] = str(self.item_emotion_var.get() or "").strip() or item["emotion"]
        item["name"] = str(self.item_name_var.get() or "").strip()
        item["weight"] = _number(self.item_weight_var.get(), "weight")
        item["enabled"] = bool(self.item_enabled_var.get())
        self._refresh_tree()
        self.status_var.set("已应用修改（记得点「保存图库」）")

    def _toggle_item(self) -> None:
        item = self._selected_item()
        if item is None:
            self.status_var.set("先在列表里选一个条目")
            return
        item["enabled"] = not bool(item.get("enabled", True))
        self.item_enabled_var.set(bool(item["enabled"]))
        self._refresh_tree()
        self.status_var.set("已切换启用状态（记得点「保存图库」）")

    def _remove_item(self) -> None:
        item = self._selected_item()
        if item is None:
            self.status_var.set("先在列表里选一个条目")
            return
        self.items = [entry for entry in self.items if entry is not item]
        self._refresh_tree()
        self.status_var.set("已从图库移除（图片文件保留在 assets/；记得点「保存图库」）")

    def _selected_item(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            return None
        index = int(str(selection[0]))
        if 0 <= index < len(self.items):
            return self.items[index]
        return None

    def _open_gallery(self) -> None:
        from atri_x.gui.process import open_path

        gallery = self.gallery_label_var.get()
        if not gallery:
            return
        os.makedirs(os.path.join(gallery, "assets"), exist_ok=True)
        if not open_path(gallery):
            self.page.fail(ConfigEditError(f"打不开图库目录：{gallery}"))


def _number(value: Any, key: str) -> float | int:
    """按字段把文本框解析成 int / float；非法值回落字段默认值。"""
    default = float(getattr(ReactionsConfig(), key, 1.0))
    text = str(value or "").strip()
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return int(default) if key != "probability" else default
    if key == "probability":
        return max(0.0, min(1.0, parsed))
    return max(0, int(parsed))


def _unique_id(items: list[dict[str, Any]], emotion: str) -> str:
    base = "reaction_" + str(len(items) + 1).zfill(3)
    existing = {str(item.get("id") or "") for item in items}
    candidate = base
    suffix = 1
    while candidate in existing:
        suffix += 1
        candidate = f"reaction_{emotion}_{suffix}"
    return candidate


def catalog_item_to_dict(item: CatalogItem, gallery: str) -> dict[str, Any]:
    """把 :class:`CatalogItem` 转成 GUI 用的条目字典（测试/导出用）。"""
    return {
        "id": item.id,
        "emotion": item.emotion,
        "path": item.path,
        "rel": os.path.relpath(item.path, gallery).replace("\\", "/"),
        "name": item.name,
        "description": item.description,
        "weight": item.weight,
        "enabled": item.enabled,
    }
