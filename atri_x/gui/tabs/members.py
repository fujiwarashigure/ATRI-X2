"""「群友」选项卡：按群管理群友的角色数据（备注名 / 标签 / 角色数据 / 头像）。

分工：

- **群友列表**来自后台服务的聊天历史库（``chat_messages`` 表，只读）：按群看"谁发过言、
  发了多少、最后什么时候"，不用手工一个个录；
- **角色数据**是 GUI 与后台共用的档案（``<配置目录>/gui/members.json``，
  见 :mod:`atri_x.memberprofile`）：备注名、标签、自由文本，加上一张头像（头像缓存到
  ``<配置目录>/gui/avatars/*.png``）。

写入只走 :mod:`atri_x.gui.memberdata`（委托共享存储；原子替换 + 损坏不覆盖由它保证）；
头像的下载/解码/缩放走 :mod:`atri_x.gui.avatars`。

关于生效时机：**勾选「参与对话」并保存后，下一轮对话就生效，不需要重启后台服务**。
后台 Agent 每轮按「平台 + 完整作用域 + 用户 id」读取当前发言人的档案并注入系统提示词；
页面下方的「提示词预览」展示的就是会被注入的那段文字。头像只是界面缓存，不会发给模型。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any

from atri_x.gui import avatars, memberdata, widgets
from atri_x.gui.memberdata import GroupScope, MemberProfile, MemberSeen
from atri_x.gui.tabs.base import ConfigTab, confirm, message
from atri_x.memberprofile import render_member_profile

__all__ = ["MembersTab"]

#: 缩略图边长（列表里用）。
_THUMB_SIZE = 22

#: 头像预览区尺寸提示。
_PREVIEW_SIZE = 96

#: 本地文件对话框的图片过滤。
_IMAGE_TYPES = (
    ("图片", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"),
    ("所有文件", "*.*"),
)


class MembersTab(ConfigTab):
    """群友列表 + 档案编辑器。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._scopes: list[GroupScope] = []
        self._seen: list[MemberSeen] = []
        self._profiles: dict[str, MemberProfile] = {}
        self._rows: dict[str, MemberProfile] = {}
        self._db_error = ""
        self._current: MemberProfile | None = None
        self._pending_avatars: dict[str, str] = {}
        self._thumb_refs: dict[str, Any] = {}
        self._preview_ref: Any = None
        self._placeholder: Any = None
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        left = ttk.Frame(self.body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        picker = ttk.Frame(left)
        picker.pack(fill=tk.X)
        ttk.Label(picker, text="群组").pack(side=tk.LEFT)
        self.scope_var = tk.StringVar()
        self.scope_box = widgets.dropdown(
            picker, self.scope_var, width=32, height=12, state="readonly"
        )
        self.scope_box.pack(side=tk.LEFT, padx=(4, 6))
        self.scope_box.bind("<<ComboboxSelected>>", self._scope_changed)
        ttk.Button(picker, text="刷新群友", command=self.reload).pack(side=tk.LEFT)
        self.source_label = ttk.Label(
            left, text="", foreground="#555555", wraplength=420, justify=tk.LEFT
        )
        self.source_label.pack(fill=tk.X, pady=(4, 4))

        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("name", "uid", "talk", "count", "last"),
            show="tree headings",
            height=18,
            selectmode="browse",
        )
        self.tree.heading("#0", text="头像")
        self.tree.heading("name", text="群友")
        self.tree.heading("uid", text="QQ / ID")
        self.tree.heading("talk", text="对话")
        self.tree.heading("count", text="发言")
        self.tree.heading("last", text="最近")
        self.tree.column("#0", width=44, stretch=False, anchor=tk.CENTER)
        self.tree.column("name", width=150, stretch=True)
        self.tree.column("uid", width=110, stretch=False)
        self.tree.column("talk", width=46, stretch=False, anchor=tk.CENTER)
        self.tree.column("count", width=52, stretch=False, anchor=tk.E)
        self.tree.column("last", width=130, stretch=False)
        scrollbar = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._member_selected)

        right = ttk.Frame(self.body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        header = ttk.Frame(right)
        header.pack(fill=tk.X)
        self.avatar_label = ttk.Label(
            header,
            text="（无头像）",
            width=14,
            anchor=tk.CENTER,
            relief=tk.GROOVE,
            padding=2,
        )
        self.avatar_label.pack(side=tk.LEFT)
        info = ttk.Frame(header)
        info.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self.id_label = ttk.Label(
            info, text="（未选中群友）", foreground="#555555", wraplength=430, justify=tk.LEFT
        )
        self.id_label.pack(anchor=tk.W)
        avatar_buttons = ttk.Frame(info)
        avatar_buttons.pack(anchor=tk.W, pady=(4, 0))
        ttk.Button(avatar_buttons, text="下载头像", command=self._download_avatar).pack(
            side=tk.LEFT
        )
        ttk.Button(avatar_buttons, text="选择本地图片", command=self._pick_avatar).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(avatar_buttons, text="移除头像", command=self._remove_avatar).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        form = ttk.Frame(right)
        form.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        form.columnconfigure(1, weight=1)
        self.name_var = tk.StringVar()
        self.alias_var = tk.StringVar()
        self.tags_var = tk.StringVar()
        self._form_row(form, 0, "昵称", self.name_var)
        self._form_row(form, 1, "备注名（列表优先显示）", self.alias_var)
        self._form_row(form, 2, "标签（逗号分隔）", self.tags_var)
        ttk.Label(form, text="角色数据").grid(row=3, column=0, sticky="nw", pady=2)
        self.profile_text = tk.Text(
            form, height=9, wrap=tk.WORD, font=("Microsoft YaHei UI", 9)
        )
        self.profile_text.grid(row=3, column=1, sticky="nsew", pady=2)
        form.rowconfigure(3, weight=1)
        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            form,
            text="参与对话：把这份档案作为「当前发言人」的资料注入系统提示词",
            variable=self.enabled_var,
            command=self._update_preview,
        ).grid(row=4, column=1, sticky="w", pady=(4, 0))

        actions = ttk.Frame(right)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(actions, text="保存档案", command=self._save).pack(side=tk.LEFT)
        ttk.Button(actions, text="删除档案", command=self._delete).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="重新载入", command=self.reload).pack(side=tk.LEFT, padx=(6, 0))

        preview_box = ttk.LabelFrame(right, text="提示词预览（保存后下一轮对话生效）")
        preview_box.pack(fill=tk.BOTH, expand=False, pady=(8, 0))
        self.preview_text = tk.Text(
            preview_box,
            height=7,
            wrap=tk.WORD,
            font=("Microsoft YaHei UI", 9),
            foreground="#333333",
            background="#f6f6f6",
        )
        self.preview_text.pack(fill=tk.BOTH, expand=True)
        self.preview_text.configure(state=tk.DISABLED)
        # 编辑时实时刷新预览（保存与否不影响预览内容，只是让操作者看到注入形态）。
        for variable in (self.name_var, self.alias_var, self.tags_var):
            variable.trace_add("write", lambda *_args: self._update_preview())
        self.profile_text.bind("<KeyRelease>", lambda _event: self._update_preview())

        ttk.Label(
            right,
            text=(
                "角色数据存在本机 <配置目录>/gui/members.json（GUI 与后台服务共用，不进 app.toml）；"
                "群友列表来自后台服务的聊天历史库。\n"
                "档案按「平台 + 群 + 用户 id」精确匹配：同一个人在不同群可以有不同的设定；"
                "停用「参与对话」或删除档案后，下一轮就恢复为不注入。"
            ),
            foreground="#8a8a8a",
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

    def _form_row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, width=18).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=2)

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        """重新读群列表与档案，并刷新当前群的群友表。"""
        if not self.is_ready():
            return
        paths = self.require_paths()
        self._profiles = memberdata.load_profiles(paths.config_dir)
        self._db_error = ""
        try:
            scopes = memberdata.list_scopes(paths)
        except memberdata.MemberDataError as exc:
            scopes = []
            self._db_error = str(exc)
        # 聊天历史被清理、或库还没建时，已建档的群仍然要能进去看，所以把档案里的群并进来。
        self._scopes = _merge_scopes(scopes, self._profiles)
        labels = [scope.label for scope in self._scopes]
        keep = self.scope_var.get().strip()
        self.scope_box.configure(values=labels)
        if keep not in labels:
            self.scope_var.set(labels[0] if labels else "")
        if not labels:
            self._rows = {}
            self._seen = []
            self.tree.delete(*self.tree.get_children())
            self.source_label.configure(text=self._snapshot_text(None))
            self._clear_editor()
            self.notify("还没有可管理的群")
            return
        self._refresh_members()

    def _refresh_members(self) -> None:
        """按当前群刷新群友表（聊天历史里的发言者 + 本群已建档的人）。"""
        paths = self.require_paths()
        scope = self._current_scope()
        self._profiles = memberdata.load_profiles(paths.config_dir)
        self._rows = {}
        self._thumb_refs.clear()
        self.tree.delete(*self.tree.get_children())
        self._placeholder = self._placeholder or _placeholder_image()
        if scope is None:
            self.source_label.configure(text=self._snapshot_text(None))
            self._clear_editor()
            self.notify("还没有可管理的群")
            return
        error = ""
        try:
            self._seen = memberdata.discover_members(paths, scope.platform, scope.scope_id)
        except memberdata.MemberDataError as exc:
            self._seen = []
            error = str(exc)
        in_scope = {
            key: profile
            for key, profile in self._profiles.items()
            if profile.platform == scope.platform and profile.scope_id == scope.scope_id
        }
        rows: list[tuple[int, str, MemberProfile, MemberSeen | None]] = []
        seen_keys: set[str] = set()
        for item in self._seen:
            seen_keys.add(item.key)
            profile = in_scope.get(item.key) or MemberProfile(
                platform=item.platform, scope_id=item.scope_id, user_id=item.user_id
            )
            profile.display_name = item.display_name or profile.display_name
            if item.key in self._pending_avatars:
                profile.avatar = self._pending_avatars[item.key]
            rows.append((item.message_count, profile.title, profile, item))
        for key, profile in in_scope.items():
            if key not in seen_keys:
                if key in self._pending_avatars:
                    profile.avatar = self._pending_avatars[key]
                rows.append((0, profile.title, profile, None))
        rows.sort(key=lambda row: (-row[0], row[1]))
        for _count, _title, profile, item in rows:
            key = profile.key
            self._rows[key] = profile
            self.tree.insert(
                "",
                tk.END,
                iid=key,
                image=self._thumb(profile),
                values=(
                    profile.title,
                    profile.user_id,
                    "✓" if profile.enabled else "—",
                    (item.message_count if item is not None else 0) or "-",
                    (item.last_at if item is not None else profile.updated_at) or "-",
                ),
            )
        detail = (
            f"数据来源：{paths.chat_history_db_path}\n"
            f"{scope.label}：{len(self._seen)} 位发言者，本群已建档 {len(in_scope)} 位"
        )
        if error:
            detail += f"\n提示：{error}"
        self.source_label.configure(text=detail)
        if self._current is not None and self._current.key in self._rows:
            self._select(self._current.key)
        elif self._rows:
            first = next(iter(self._rows))
            self._select(first)
        else:
            self._clear_editor()
        self.notify(f"已载入 {len(rows)} 位群友（{scope.label}）")

    def _scope_changed(self, _event: Any = None) -> None:
        self._current = None
        self._refresh_members()

    def _current_scope(self) -> GroupScope | None:
        label = self.scope_var.get().strip()
        for scope in self._scopes:
            if scope.label == label:
                return scope
        return None

    def _snapshot_text(self, scope: GroupScope | None) -> str:
        """数据来源提示（库读不到时把原因一起说明）。"""
        paths = self.require_paths()
        if scope is None:
            text = (
                "聊天历史里还没有任何群/会话，也没有已建档的群友。先用桌面端启动后台服务、"
                "让机器人收几条群消息，再点「刷新群友」。\n"
                f"聊天历史库：{paths.chat_history_db_path}"
            )
        else:
            text = f"数据来源：{paths.chat_history_db_path}\n{scope.label}"
        if self._db_error:
            text += f"\n提示：{self._db_error}"
        return text

    # -- 列表 ---------------------------------------------------------------
    def _thumb(self, profile: MemberProfile) -> Any:
        """列表行的头像（缓存文件不在了就退回灰色占位图）。"""
        path = avatars.avatar_file(self._require_config_dir(), profile.avatar)
        if path and os.path.exists(path):
            try:
                image = tk.PhotoImage(file=path)
                factor = max(1, avatars.AVATAR_SIZE // _THUMB_SIZE)
                thumb = image.subsample(factor, factor)
                self._thumb_refs[profile.key] = (image, thumb)
                return thumb
            except tk.TclError:  # pragma: no cover - 文件损坏
                pass
        return self._placeholder

    def _select(self, key: str) -> None:
        if key in self._rows and self.tree.exists(key):
            self.tree.selection_set(key)
            self.tree.see(key)
            self._load_editor(self._rows[key])

    def _member_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        profile = self._rows.get(str(selection[0]))
        if profile is not None:
            self._load_editor(profile)

    # -- 编辑器 -------------------------------------------------------------
    def _load_editor(self, profile: MemberProfile) -> None:
        self._current = profile
        updated = f"最近保存：{profile.updated_at}" if profile.updated_at else "还没保存过档案"
        self.id_label.configure(
            text=f"{profile.platform} · {profile.scope_id}\n用户 id：{profile.user_id}   {updated}"
        )
        self.name_var.set(profile.display_name)
        self.alias_var.set(profile.alias)
        self.tags_var.set(", ".join(profile.tags))
        self.enabled_var.set(bool(profile.enabled))
        self.profile_text.delete("1.0", tk.END)
        self.profile_text.insert("1.0", profile.profile)
        self._show_avatar(profile)
        self._update_preview()

    def _clear_editor(self) -> None:
        self._current = None
        self.id_label.configure(text="（未选中群友）")
        self.name_var.set("")
        self.alias_var.set("")
        self.tags_var.set("")
        self.enabled_var.set(True)
        self.profile_text.delete("1.0", tk.END)
        self._show_avatar(None)
        self._update_preview()

    def _update_preview(self) -> None:
        """把当前编辑器里的内容渲染成"会注入的提示词"预览。"""
        if not hasattr(self, "preview_text"):
            return
        text = "（未选中群友）"
        if self._current is not None:
            draft = MemberProfile(
                platform=self._current.platform,
                scope_id=self._current.scope_id,
                user_id=self._current.user_id,
                display_name=self.name_var.get().strip(),
                alias=self.alias_var.get().strip(),
                tags=_split(self.tags_var.get()),
                profile=self.profile_text.get("1.0", "end-1c").strip(),
                enabled=bool(self.enabled_var.get()),
            )
            if not draft.enabled:
                text = "当前设为「不参与对话」：保存后这份档案不会被注入系统提示词。"
            else:
                text = render_member_profile(draft)
                if not text:
                    text = (
                        "这份档案还没有可注入的内容：填称呼、标签或角色资料，"
                        "勾选「参与对话」并保存。"
                    )
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state=tk.DISABLED)

    def _show_avatar(self, profile: MemberProfile | None) -> None:
        path = ""
        if profile is not None:
            path = avatars.avatar_file(self._require_config_dir(), profile.avatar)
        if path and os.path.exists(path):
            try:
                image = tk.PhotoImage(file=path)
                self._preview_ref = image
                self.avatar_label.configure(image=image, text="")
                return
            except tk.TclError:  # pragma: no cover - 文件损坏
                pass
        self._preview_ref = None
        self.avatar_label.configure(
            image="", text=f"（无头像）\n{_PREVIEW_SIZE}×{_PREVIEW_SIZE}"
        )

    # -- 动作 ---------------------------------------------------------------
    def _require_current(self) -> MemberProfile | None:
        if self._current is None:
            message(self.frame, "群友档案", "先在左边的列表里选一个群友。")
            return None
        return self._current

    def _save(self) -> None:
        profile = self._require_current()
        if profile is None:
            return
        try:
            saved = memberdata.upsert_profile(
                self._require_config_dir(),
                MemberProfile(
                    platform=profile.platform,
                    scope_id=profile.scope_id,
                    user_id=profile.user_id,
                    display_name=self.name_var.get(),
                    alias=self.alias_var.get(),
                    tags=_split(self.tags_var.get()),
                    profile=self.profile_text.get("1.0", "end-1c"),
                    avatar=profile.avatar,
                    enabled=bool(self.enabled_var.get()),
                ),
            )
        except (memberdata.MemberDataError, OSError) as exc:
            self.fail(exc)
            return
        self._current = saved
        self._pending_avatars.pop(saved.key, None)
        self.journal(f"保存群友档案：{saved.key}（备注名 {saved.alias or '-'}）")
        self._refresh_members()
        self._select(saved.key)
        self.notify(f"已保存 {saved.title} 的角色数据")

    def _delete(self) -> None:
        profile = self._require_current()
        if profile is None:
            return
        if not confirm(
            self.frame,
            "删除群友档案",
            f"删除 {profile.title} 的档案（备注名 / 标签 / 角色数据 / 头像缓存）？\n\n"
            "聊天历史不受影响，之后还能重新建档。",
        ):
            return
        try:
            removed = memberdata.delete_profile(self._require_config_dir(), profile.key)
        except (memberdata.MemberDataError, OSError) as exc:
            self.fail(exc)
            return
        self.journal(f"删除群友档案：{profile.key}" if removed else "删除群友档案：未找到")
        self._current = None
        self._refresh_members()
        self.notify(f"已删除 {profile.title} 的档案")

    # -- 头像 ---------------------------------------------------------------
    def _download_avatar(self) -> None:
        profile = self._require_current()
        if profile is None:
            return
        url = avatars.avatar_url(profile.platform, profile.user_id)
        if not url:
            message(
                self.frame,
                "下载头像",
                f"{profile.platform} 没有公开的头像接口；可以用「选择本地图片」手动指定。",
            )
            return
        self.notify("正在下载头像…")
        self.journal(f"下载头像：{profile.key} ← {url}")
        widgets.run_async(
            self.frame,
            lambda: avatars.fetch_avatar_png(url),
            self._avatar_downloaded,
        )

    def _avatar_downloaded(self, png: Any, error: BaseException | None) -> None:
        if error is not None:
            self.journal(f"下载头像失败：{error}")
            self.fail(f"下载头像失败：{error}")
            return
        self._apply_avatar_bytes(bytes(png or b""))

    def _pick_avatar(self) -> None:
        profile = self._require_current()
        if profile is None:
            return
        path = filedialog.askopenfilename(
            parent=self.frame, title="选择头像图片", filetypes=list(_IMAGE_TYPES)
        )
        if not path:
            return
        key = profile.key
        config_dir = self._require_config_dir()
        self.notify("正在处理这张图片…")
        widgets.run_async(
            self.frame,
            lambda: avatars.cache_avatar_file(config_dir, key, path),
            self._avatar_picked,
        )

    def _avatar_picked(self, relative: Any, error: BaseException | None) -> None:
        if error is not None:
            self.journal(f"设置本地头像失败：{error}")
            self.fail(f"设置头像失败：{error}")
            return
        self._apply_avatar_relative(str(relative or ""))

    def _apply_avatar_bytes(self, png: bytes) -> None:
        profile = self._current
        if profile is None:
            return
        relative = avatars.cache_avatar(self._require_config_dir(), profile.key, png)
        self._apply_avatar_relative(relative)

    def _apply_avatar_relative(self, relative: str) -> None:
        profile = self._current
        if profile is None:
            return
        # 头像缓存已经落盘；档案要等「保存档案」才写 JSON，所以先在内存里记住待保存的头像，
        # 刷新列表时再贴回档案，避免"刚下载的头像刷新一下就没了"。
        self._pending_avatars[profile.key] = relative
        profile.avatar = relative
        self._refresh_members()
        self._select(profile.key)
        self.journal(f"更新头像缓存：{profile.key} → {relative}")
        self.notify("头像已缓存；点「保存档案」写入档案")

    def _remove_avatar(self) -> None:
        profile = self._require_current()
        if profile is None:
            return
        if not profile.avatar:
            self.notify("这个群友本来就没有头像")
            return
        avatars.clear_avatar(self._require_config_dir(), profile.avatar)
        self._pending_avatars[profile.key] = ""
        profile.avatar = ""
        self._refresh_members()
        self._select(profile.key)
        self.journal(f"移除头像：{profile.key}")
        self.notify("已移除头像缓存；点「保存档案」写入档案")

    # -- 路径 ---------------------------------------------------------------
    def _require_config_dir(self) -> str:
        return self.require_paths().config_dir


def _split(text: str) -> list[str]:
    """逗号分隔 → 去空白、去空值（中英文逗号都接受）。"""
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


def _merge_scopes(
    scopes: list[GroupScope], profiles: dict[str, MemberProfile]
) -> list[GroupScope]:
    """聊天历史里的群 + 档案里出现但历史里暂时没有的群（历史被清理时也要能看）。"""
    merged = list(scopes)
    known = {(scope.platform, scope.scope_id) for scope in merged}
    for profile in profiles.values():
        pair = (profile.platform, profile.scope_id)
        if not pair[0] or not pair[1] or pair in known:
            continue
        known.add(pair)
        merged.append(GroupScope(platform=pair[0], scope_id=pair[1]))
    return merged


def _placeholder_image() -> Any:
    """灰色占位头像（Tk 自带 PhotoImage，不依赖 Pillow）。"""
    image = tk.PhotoImage(width=_THUMB_SIZE, height=_THUMB_SIZE)
    image.put("#d0d0d0", to=(0, 0, _THUMB_SIZE, _THUMB_SIZE))
    return image
