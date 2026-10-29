"""「人设卡」选项卡：角色库编辑 + 系统人设（SOUL.md）。

三个子页：

1. **人设与文档**：角色的名称/别名/标签/简介/可见性，以及 ``profile.md`` /
   ``world.md`` / ``greeting.md`` / ``examples.md`` / ``image_prompt.md`` /
   ``notes/*.md`` 的正文编辑；
2. **生图预设**：``character.toml`` 的 ``[image]``（preset_prompt / negative_prompt /
   size / quality / references），图片列表只读（图片请用聊天里的
   ``character_manage`` 工具添加）；
3. **系统人设**：``SOUL.md``，即全局 System Prompt。

写入全部走 :mod:`atri_x.character` 的 ``Store.write`` / ``Store.delete``：版本号自增、
文档落盘、目录结构由它保证，和 Agent 工具链路是同一套语义。GUI 以超级管理员身份
编辑本地文件（本地进程本来就有文件权限）。角色库默认 3 秒内自动重扫，无需重启。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, simpledialog, ttk
from typing import Any

from atri_x.character import (
    DOC_ORDER,
    Character,
    CharacterError,
    ImageSettings,
    Store,
    Viewer,
    WriteRequest,
    activate_character,
    export_json,
    export_markdown,
    export_zip,
    import_file,
    import_text,
    restore_soul,
    soul_status,
)
from atri_x.gui.configedit import ConfigEditError, write_text_file
from atri_x.gui.tabs.base import ConfigTab, confirm, message

__all__ = ["CharacterTab"]

#: 本地 GUI 以超级管理员身份编辑（能看/改所有角色，包括 system 公共角色）。
VIEWER = Viewer(platform="desktop", actor_id="local", superadmin=True)

#: 文档种类 → 列表里的中文名。
_DOC_LABELS = {
    "profile": "profile（人设正文）",
    "world": "world（世界观）",
    "greeting": "greeting（开场白）",
    "examples": "examples（对话示例）",
    "image_prompt": "image_prompt（生图词）",
}

#: 角色可见性下拉项。
_VISIBILITIES = ("public", "private")

#: 新建角色的初始人设模板。
_PROFILE_TEMPLATE = "# {name}\n\n在这里写人设：性格、说话方式、背景、称呼习惯。\n"


class CharacterTab(ConfigTab):
    """角色库 + SOUL.md 编辑器。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._store: Store | None = None
        self._items: dict[str, Character] = {}
        self._current: Character | None = None
        self._docs: dict[str, str] = {}
        self._doc_kind = ""
        self._doc_kinds: list[str] = []
        #: 「表情图库」子选项卡（发送策略 + catalog.json 条目管理）。
        self.reaction_panel: Any = None
        #: 「表情图库」子选项卡对应的 notebook 页框（跳转时要选它，不能选面板自己的 frame）。
        self.reaction_page: Any = None
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        left = ttk.Frame(self.body)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        ttk.Label(left, text="角色库", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.tree = ttk.Treeview(
            left, columns=("id", "name", "vis"), show="headings", height=16, selectmode="browse"
        )
        self.tree.heading("id", text="id")
        self.tree.heading("name", text="名称")
        self.tree.heading("vis", text="可见性")
        self.tree.column("id", width=96, stretch=False)
        self.tree.column("name", width=96, stretch=False)
        self.tree.column("vis", width=56, stretch=False)
        scrollbar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._character_selected)
        buttons = ttk.Frame(left)
        buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="新建", width=7, command=self._new_character).pack(side=tk.LEFT)
        ttk.Button(buttons, text="删除", width=7, command=self._delete_character).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(buttons, text="刷新", width=7, command=self.reload).pack(side=tk.LEFT, padx=(4, 0))
        more = ttk.Frame(left)
        more.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(more, text="导入文件", width=7, command=self._import_character).pack(
            side=tk.LEFT
        )
        ttk.Button(more, text="粘贴导入", width=7, command=self._paste_import).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(more, text="导出", width=7, command=self._export_character).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        self.notebook = ttk.Notebook(self.body)
        self.notebook.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._build_character_page()
        self._build_image_page()
        self._build_soul_page()
        self._build_reaction_page()
        self.button("保存人设卡", self._save_character)
        self.button("重新载入", self.reload)

    def _build_character_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(page, text=" 人设与文档 ")
        form = ttk.Frame(page)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        self.id_label = ttk.Label(form, text="（未选中角色）", foreground="#555555")
        self.id_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.name_var = tk.StringVar()
        self.aliases_var = tk.StringVar()
        self.tags_var = tk.StringVar()
        self.visibility_var = tk.StringVar(value="public")
        self._form_row(form, 1, "名称", self.name_var)
        self._form_row(form, 2, "别名（逗号分隔）", self.aliases_var)
        self._form_row(form, 3, "标签（逗号分隔）", self.tags_var)
        vis_row = ttk.Frame(form)
        vis_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(vis_row, text="可见性", width=18).pack(side=tk.LEFT)
        ttk.Combobox(
            vis_row,
            textvariable=self.visibility_var,
            values=list(_VISIBILITIES),
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT)
        ttk.Label(form, text="简介").grid(row=5, column=0, sticky="nw", pady=2)
        self.description_text = tk.Text(form, height=3, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self.description_text.grid(row=5, column=1, sticky="ew", pady=2)

        docs = ttk.Frame(page)
        docs.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        list_frame = ttk.Frame(docs)
        list_frame.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(list_frame, text="文档").pack(anchor="w")
        self.doc_list = tk.Listbox(list_frame, width=30, height=12, exportselection=False)
        self.doc_list.pack(side=tk.LEFT, fill=tk.Y)
        doc_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.doc_list.yview)
        doc_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.doc_list.configure(yscrollcommand=doc_scroll.set)
        self.doc_list.bind("<<ListboxSelect>>", self._doc_selected)
        doc_buttons = ttk.Frame(list_frame)
        doc_buttons.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(doc_buttons, text="新增笔记", width=9, command=self._new_note).pack(side=tk.LEFT)
        ttk.Button(doc_buttons, text="删除这篇", width=9, command=self._delete_doc).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        self.doc_text = tk.Text(docs, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self.doc_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

    def _build_image_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(page, text=" 生图预设 ")
        form = ttk.Frame(page)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="生图预设提示词").grid(row=0, column=0, sticky="nw", pady=2)
        self.image_preset_text = tk.Text(
            form, height=5, wrap=tk.WORD, font=("Microsoft YaHei UI", 9)
        )
        self.image_preset_text.grid(row=0, column=1, sticky="ew", pady=2)
        self.preset_var = tk.StringVar()
        self._form_row(form, 1, "负向提示词", self.preset_var)
        self.image_size_var = tk.StringVar()
        self._form_row(form, 2, "尺寸（如 1024x1536）", self.image_size_var)
        self.image_quality_var = tk.StringVar()
        self._form_row(form, 3, "质量（low/medium/high）", self.image_quality_var)
        self.references_var = tk.StringVar()
        self._form_row(form, 4, "参考图（逗号分隔）", self.references_var)
        ttk.Label(page, text="已入库图片").pack(anchor="w", pady=(10, 2))
        self.image_tree = ttk.Treeview(
            page, columns=("name", "size", "path"), show="headings", height=6
        )
        self.image_tree.heading("name", text="文件")
        self.image_tree.heading("size", text="大小")
        self.image_tree.heading("path", text="路径")
        self.image_tree.column("name", width=140, stretch=False)
        self.image_tree.column("size", width=90, stretch=False)
        self.image_tree.column("path", width=320)
        self.image_tree.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            page,
            text="图片请用聊天里的 character_manage（add_image / remove_image）添加或删除。",
            foreground="#8a8a8a",
        ).pack(anchor="w", pady=(4, 0))

    def _build_soul_page(self) -> None:
        page = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(page, text=" 系统人设 SOUL.md ")
        ttk.Label(
            page,
            text="SOUL.md 是全局 System Prompt（所有人设卡之外的默认身份）。保存后下一轮对话生效。",
            foreground="#555555",
        ).pack(anchor="w")
        self.soul_path_label = ttk.Label(page, text="", foreground="#8a8a8a")
        self.soul_path_label.pack(anchor="w", pady=(2, 4))
        self.soul_text = tk.Text(page, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self.soul_text.pack(fill=tk.BOTH, expand=True)
        actions = ttk.Frame(page)
        actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(actions, text="保存 SOUL.md", command=self._save_soul).pack(side=tk.LEFT)
        ttk.Button(actions, text="重新载入", command=self._load_soul).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            actions, text="从当前人设卡载入…", command=self._activate_soul_from_card
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(
            actions, text="恢复上次备份", command=self._restore_soul_backup
        ).pack(side=tk.LEFT, padx=(6, 0))
        self.soul_backup_label = ttk.Label(page, text="", foreground="#8a8a8a")
        self.soul_backup_label.pack(anchor="w", pady=(4, 0))

    def _build_reaction_page(self) -> None:
        """第四个子选项卡：本地表情图库（发送策略 + catalog.json 条目管理）。"""
        from atri_x.gui.reactionpage import REACTION_TAB_TITLE, ReactionPanel

        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=REACTION_TAB_TITLE)
        self.reaction_page = page
        self.reaction_panel = ReactionPanel(self, page)
        # 面板自带 padding（ReactionPanel.frame），必须显式 pack 到页面里。
        # 1.0.4–1.0.7 漏了这一步：面板内部 60 个控件全都建在未布局的 frame 上，
        # 于是「表情图库」子选项卡整页空白、什么也点不了（功能页跳转也会 TclError）。
        self.reaction_panel.frame.pack(fill=tk.BOTH, expand=True)

    def show_reactions_tab(self) -> None:
        """跳到「表情图库」子选项卡（「功能」页双击跳转用）。"""
        page = self.reaction_page
        if page is None and self.reaction_panel is not None:
            page = self.reaction_panel.frame.master
        if page is None:
            return
        try:
            self.notebook.select(page)
        except tk.TclError:  # pragma: no cover - 页面已销毁
            return

    def _form_row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, width=18).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=2)

    # -- 数据 ---------------------------------------------------------------
    def _open_store(self) -> Store:
        paths = self.require_paths()
        if self._store is None or self._store.root != paths.character_root:
            self._store = Store(paths.character_root)
        return self._store

    def reload(self) -> None:
        """重新扫描角色库并刷新表单（不覆盖用户还没保存的编辑？——以磁盘为准）。"""
        if not self.is_ready():
            return
        try:
            store = self._open_store()
            # 手动刷新要立刻看到磁盘现状：绕过 Store 的 3 秒扫描冷却。
            store.refresh(None)
            items = store.list(None, VIEWER)
        except (ConfigEditError, CharacterError, OSError) as exc:
            self.fail(exc)
            return
        keep = self._current.id if self._current is not None else ""
        self._items = {item.id: item for item in items}
        self.tree.delete(*self.tree.get_children())
        for item in items:
            self.tree.insert("", tk.END, iid=item.id, values=(item.id, item.name, item.visibility))
        self._load_soul()
        if self.reaction_panel is not None:
            self.reaction_panel.load()
        if keep and keep in self._items:
            self._select(keep)
        elif self._items:
            self._select(sorted(self._items)[0])
        else:
            self._current = None
            self.id_label.configure(text="角色库是空的：点左下「新建」建一个人设卡。")
        self.notify(f"已载入 {len(items)} 个角色（{store.root}）")

    def _select(self, character_id: str) -> None:
        if character_id in self.tree.get_children(""):
            self.tree.selection_set(character_id)
            self.tree.see(character_id)
        item = self._items.get(character_id)
        if item is not None:
            self._load_character(item)

    def _character_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        item = self._items.get(str(selection[0]))
        if item is not None:
            self._load_character(item)

    def _load_character(self, item: Character) -> None:
        self._current = item
        self.id_label.configure(
            text=f"id: {item.id}   版本: {item.version or '-'}   来源: {item.source or '-'}"
        )
        self.name_var.set(item.name)
        self.aliases_var.set(", ".join(item.aliases))
        self.tags_var.set(", ".join(item.tags))
        self.visibility_var.set(str(item.visibility))
        self.description_text.delete("1.0", tk.END)
        self.description_text.insert("1.0", item.description)
        self.image_preset_text.delete("1.0", tk.END)
        self.image_preset_text.insert("1.0", item.image.preset_prompt)
        self.preset_var.set(item.image.negative_prompt)
        self.image_size_var.set(item.image.size)
        self.image_quality_var.set(item.image.quality)
        self.references_var.set(", ".join(item.image.references))
        self.image_tree.delete(*self.image_tree.get_children())
        for image in item.images:
            self.image_tree.insert("", tk.END, values=(image.name, image.size, image.path))
        self._docs = {kind: item.docs.get(kind, "") for kind in DOC_ORDER}
        for kind, content in item.docs.items():
            if kind.startswith("notes/"):
                self._docs[kind] = content
        self._doc_kind = ""
        self._refresh_doc_list()
        self._select_doc("profile")

    def _refresh_doc_list(self) -> None:
        self.doc_list.delete(0, tk.END)
        self._doc_kinds = list(DOC_ORDER) + sorted(
            kind for kind in self._docs if kind.startswith("notes/")
        )
        for kind in self._doc_kinds:
            label = _DOC_LABELS.get(kind, kind + ".md")
            if not self._docs.get(kind, "").strip():
                label += "（空）"
            self.doc_list.insert(tk.END, label)

    def _select_doc(self, kind: str) -> None:
        if kind not in self._doc_kinds:
            return
        self._flush_doc()
        self._doc_kind = kind
        index = self._doc_kinds.index(kind)
        self.doc_list.selection_clear(0, tk.END)
        self.doc_list.selection_set(index)
        self.doc_list.see(index)
        self.doc_text.delete("1.0", tk.END)
        self.doc_text.insert("1.0", self._docs.get(kind, ""))

    def _doc_selected(self, _event: Any = None) -> None:
        selection = self.doc_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(self._doc_kinds):
            kind = self._doc_kinds[index]
            if kind != self._doc_kind:
                self._select_doc(kind)

    def _flush_doc(self) -> None:
        if self._doc_kind:
            self._docs[self._doc_kind] = self.doc_text.get("1.0", "end-1c")

    def _load_soul(self) -> None:
        if not self.is_ready():
            return
        path = self.require_paths().soul_path
        self.soul_path_label.configure(text=path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            self.fail(f"读取 SOUL.md 失败：{exc}")
            return
        self.soul_text.delete("1.0", tk.END)
        self.soul_text.insert("1.0", text)
        self._refresh_soul_status()

    def _refresh_soul_status(self) -> None:
        """刷新 SOUL.md 大小与备份状态（不读正文，避免重复渲染）。"""
        try:
            status = soul_status(self.require_paths().soul_path)
        except (ConfigEditError, OSError):
            self.soul_backup_label.configure(text="")
            return
        backup = (
            f"有备份（{status.backup_size} bytes）"
            if status.backup_exists
            else "还没有激活备份"
        )
        self.soul_backup_label.configure(
            text=f"当前 {status.size} bytes；{backup}"
        )

    # -- 动作 ---------------------------------------------------------------
    def _new_character(self) -> None:
        name = simpledialog.askstring(
            "新建人设卡",
            "角色 id（小写字母/数字/-/_/.，最长 64；会作为 characters/<id>/ 目录名）：",
            parent=self.frame,
        )
        if not name or not name.strip():
            return
        character_id = name.strip()
        try:
            store = self._open_store()
            req = WriteRequest(
                id=character_id,
                name=character_id,
                description="",
                docs={"profile": _PROFILE_TEMPLATE.format(name=character_id)},
            )
            store.write(None, req, VIEWER)
        except (ConfigEditError, CharacterError) as exc:
            self.fail(exc)
            return
        self._current = None
        self.reload()
        self._select(character_id)
        self.notify(f"已创建角色 {character_id}（角色库 3 秒内自动重扫）")

    def _delete_character(self) -> None:
        if self._current is None:
            message(self.frame, "删除人设卡", "先选中一个角色。")
            return
        character_id = self._current.id
        if not confirm(
            self.frame,
            "删除人设卡",
            f"永久删除角色 {character_id}？\n\n整个目录（正文、图片）都会被删掉，不能撤销。",
        ):
            return
        try:
            self._open_store().delete(None, character_id, VIEWER)
        except (ConfigEditError, CharacterError) as exc:
            self.fail(exc)
            return
        self._current = None
        self.reload()
        self.notify(f"已删除角色 {character_id}")

    def _new_note(self) -> None:
        if self._current is None:
            message(self.frame, "新增笔记", "先选中一个角色。")
            return
        name = simpledialog.askstring(
            "新增笔记", "笔记名（会成为 notes/<名字>.md）：", parent=self.frame
        )
        if not name or not name.strip():
            return
        kind = "notes/" + name.strip()
        self._docs[kind] = ""
        self._refresh_doc_list()
        self._select_doc(kind)
        self.notify(f"已新增 {kind}（点「保存人设卡」落盘）")

    def _delete_doc(self) -> None:
        if not self._doc_kind:
            return
        kind = self._doc_kind
        if not confirm(self.frame, "删除文档", f"删除 {kind} 的内容？（保存后落盘）"):
            return
        self._docs[kind] = ""
        self.doc_text.delete("1.0", tk.END)
        self._refresh_doc_list()
        self.notify(f"已清空 {kind}（保存后删除文件）")

    def _save_character(self) -> None:
        try:
            if self._current is None:
                raise ConfigEditError("先选一个角色，或点「新建」")
            self._flush_doc()
            self._open_store().write(
                None,
                WriteRequest(
                    id=self._current.id,
                    name=self.name_var.get().strip(),
                    aliases=_split(self.aliases_var.get()),
                    tags=_split(self.tags_var.get()),
                    description=self.description_text.get("1.0", "end-1c").strip(),
                    visibility=self.visibility_var.get().strip(),
                    docs={kind: text for kind, text in self._docs.items()},
                    image=ImageSettings(
                        preset_prompt=self.image_preset_text.get("1.0", "end-1c").strip(),
                        negative_prompt=self.preset_var.get().strip(),
                        size=self.image_size_var.get().strip(),
                        quality=self.image_quality_var.get().strip(),
                        references=_split(self.references_var.get()),
                    ),
                ),
                VIEWER,
            )
        except (ConfigEditError, CharacterError) as exc:
            self.fail(exc)
            return
        character_id = self._current.id
        self.reload()
        self._select(character_id)
        self.notify(f"已保存 {character_id}（角色库 3 秒内自动重扫，无需重启）")

    def _save_soul(self) -> None:
        try:
            path = self.require_paths().soul_path
            write_text_file(path, self.soul_text.get("1.0", "end-1c"))
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._refresh_soul_status()
        self.notify(f"已保存 {os.path.basename(path)}（下一轮对话生效）")

    # -- 导入 / 导出 ---------------------------------------------------------
    def _import_character(self) -> None:
        """从文件导入人设卡（zip / json / toml / md / txt）。"""
        path = filedialog.askopenfilename(
            parent=self.frame,
            title="导入人设卡",
            filetypes=[
                ("人设卡（zip / json / toml / md）", "*.zip *.json *.toml *.md *.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        self._run_import(
            lambda: import_file(self._open_store(), None, path, VIEWER),
            os.path.basename(path),
        )

    def _paste_import(self) -> None:
        """打开「粘贴文本导入」对话框：一步建卡，可顺带设为全局人设。"""
        dialog = _PasteImportDialog(self)
        self.frame.wait_window(dialog.window)

    def _run_import(self, action: Any, label: str) -> None:
        try:
            result = action()
        except (ConfigEditError, CharacterError, OSError, ValueError) as exc:
            self.fail(exc)
            return
        item = result.character
        self.reload()
        self._select(item.id)
        summary = (
            f"已导入 {label} → 角色 {item.id}（{'新建' if result.created else '更新'}）"
        )
        self.journal(summary)
        self.notify(summary)
        if result.warnings:
            message(self.frame, "导入完成（有提示）", summary + "\n\n" + "\n".join(result.warnings))

    def _export_character(self) -> None:
        """导出当前角色：ZIP（含图片）/ JSON / Markdown（只含 profile）。"""
        if self._current is None:
            message(self.frame, "导出人设卡", "先在左边选中一个角色。")
            return
        character_id = self._current.id
        path = filedialog.asksaveasfilename(
            parent=self.frame,
            title="导出人设卡",
            initialfile=character_id + ".zip",
            defaultextension=".zip",
            filetypes=[
                ("ZIP（含文档与图片）", "*.zip"),
                ("JSON（文档全文，不含图片字节）", "*.json"),
                ("Markdown（仅 profile.md）", "*.md"),
            ],
        )
        if not path:
            return
        lower = path.lower()
        store = self._open_store()
        try:
            if lower.endswith(".json"):
                export_json(store, None, character_id, path, VIEWER)
            elif lower.endswith(".md") or lower.endswith(".txt"):
                export_markdown(store, None, character_id, path, VIEWER)
            else:
                export_zip(store, None, character_id, path, VIEWER)
        except (ConfigEditError, CharacterError, OSError, ValueError) as exc:
            self.fail(exc)
            return
        self.journal(f"导出人设卡：{character_id} → {path}")
        self.notify(f"已导出 {character_id} → {path}")

    # -- SOUL 激活 / 回滚 ----------------------------------------------------
    def _activate_soul_from_card(self) -> None:
        """把当前选中的人设卡写成全局系统人设 SOUL.md。"""
        if self._current is None:
            message(self.frame, "设为全局系统人设", "先在左边选中一个角色。")
            return
        item = self._current
        try:
            soul_path = self.require_paths().soul_path
        except ConfigEditError as exc:
            self.fail(exc)
            return
        if not confirm(
            self.frame,
            "设为全局系统人设",
            f"把 {item.name}（{item.id}）写成 SOUL.md？\n\n"
            "之后所有**没有专属人设**的用户都会使用该人设；其他人的专属人设不受影响。\n"
            f"当前 SOUL.md 会备份到 {soul_path}.bak，可在这里点「恢复上次备份」。\n"
            "下一轮对话生效。",
        ):
            return
        try:
            result = activate_character(self._open_store(), None, item.id, soul_path, VIEWER)
        except (ConfigEditError, CharacterError, OSError, ValueError) as exc:
            self.fail(exc)
            return
        self._load_soul()
        self.journal(f"把人设卡 {result.character.id} 设为全局系统人设")
        self.notify(f"已把 {result.character.name} 设为全局系统人设（备份：{result.backup_path}）")

    def _restore_soul_backup(self) -> None:
        """恢复激活前的 SOUL.md 备份。"""
        try:
            soul_path = self.require_paths().soul_path
        except ConfigEditError as exc:
            self.fail(exc)
            return
        status = soul_status(soul_path)
        if not status.backup_exists:
            message(self.frame, "恢复 SOUL 备份", "还没有可恢复的备份（SOUL.md.bak 不存在）。")
            return
        if not confirm(
            self.frame,
            "恢复 SOUL 备份",
            "把 SOUL.md 回滚到上一次激活前的版本？\n\n当前内容会另存为 SOUL.md.restore.bak。",
        ):
            return
        try:
            restore_soul(soul_path)
        except OSError as exc:
            self.fail(exc)
            return
        self._load_soul()
        self.journal("恢复 SOUL.md 备份")
        self.notify("已恢复 SOUL.md 备份（下一轮对话生效）")


def _split(text: str) -> list[str]:
    """逗号分隔的输入 → 去空白、去空值的列表（中英文逗号都接受）。"""
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


class _PasteImportDialog:
    """「粘贴导入」对话框：一段文本 → 一张角色卡（可顺带设为全局人设）。

    这是"客户端里一步完成"的桌面端入口：用户把聊天里/网页上复制的人设全文贴进来，
    点一次导入即可建卡；勾选「同时设为全局系统人设」时直接写 SOUL.md（旧内容自动
    备份）。不需要先手工创建目录、也不需要去对话里让模型翻文件。
    """

    def __init__(self, tab: "CharacterTab") -> None:
        self.tab = tab
        self.window = tk.Toplevel(tab.frame)
        self.window.title("粘贴文本导入人设卡")
        self.window.transient(tab.frame)
        self.window.geometry("760x560")
        body = ttk.Frame(self.window, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        form = ttk.Frame(body)
        form.pack(fill=tk.X)
        form.columnconfigure(1, weight=1)
        self.id_var = tk.StringVar()
        self.name_var = tk.StringVar()
        ttk.Label(form, text="角色 id（可留空，自动生成）").grid(
            row=0, column=0, sticky="w", pady=2
        )
        ttk.Entry(form, textvariable=self.id_var).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(form, text="角色名（可留空，取正文第一个标题）").grid(
            row=1, column=0, sticky="w", pady=2
        )
        ttk.Entry(form, textvariable=self.name_var).grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(body, text="人设正文（会写入 profile.md）").pack(anchor="w", pady=(8, 2))
        wrap = ttk.Frame(body)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(wrap, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.activate_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body,
            text="同时设为全局系统人设（写 SOUL.md，旧内容自动备份，可回滚）",
            variable=self.activate_var,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            body,
            text="导入后可在本页继续编辑；角色库 3 秒内自动重扫，无需重启。",
            foreground="#8a8a8a",
        ).pack(anchor="w")
        actions = ttk.Frame(body)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(actions, text="导入", command=self._submit).pack(side=tk.RIGHT)
        ttk.Button(actions, text="取消", command=self.window.destroy).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        self.text.focus_set()

    def _submit(self) -> None:
        text = self.text.get("1.0", "end-1c")
        if not text.strip():
            message(self.window, "粘贴导入", "人设正文是空的。")
            return
        try:
            store = self.tab._open_store()
            result = import_text(
                store,
                None,
                text,
                VIEWER,
                character_id=self.id_var.get().strip(),
                name=self.name_var.get().strip(),
            )
            activated = False
            if self.activate_var.get():
                paths = self.tab.require_paths()
                activate_character(store, None, result.character.id, paths.soul_path, VIEWER)
                activated = True
        except (ConfigEditError, CharacterError, OSError, ValueError) as exc:
            self.tab.fail(exc)
            return
        self.window.destroy()
        self.tab.reload()
        self.tab._select(result.character.id)
        item = result.character
        summary = f"已导入角色 {item.name}（id={item.id}）"
        if activated:
            summary += "，并设为全局系统人设"
        self.tab.journal(summary)
        self.tab.notify(summary)
        if result.warnings:
            message(
                self.tab.frame,
                "导入完成（有提示）",
                summary + "\n\n" + "\n".join(result.warnings),
            )
