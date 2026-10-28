"""群友档案的 JSON 存储：按文件指纹缓存、读写互斥、损坏文件不静默覆盖。

存储格式见 :mod:`atri_x.memberprofile.model`：``<配置目录>/gui/members.json``。
第一版继续用这个文件（不迁 SQLite），这样界面上已经录好的人设升级后可以直接用。

三条约定：

1. **按指纹热更新**：用 ``(mtime_ns, size)`` 判断文件有没有变；文件被删除时指纹从
   "有值"变成 ``None``，缓存随之清空。保存后下一轮对话就能读到新内容，不需要重启。
2. **损坏不静默**：读取失败时 ``load()`` 仍返回空表（界面照常能打开），但会记一条
   WARN；``upsert()`` / ``remove()`` 会直接抛 :class:`MemberDataError`，**不会**把损坏
   的文件当空表覆盖掉。
3. **写入原子**：先写 ``.tmp`` 再 ``os.replace``；进程内用锁串行化。``os.replace`` 只能
   保证不出现半写文件，不能防止多个写入者互相覆盖——后续如增加群命令编辑，需要
   版本检查或统一写入服务。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from atri_x.memberprofile.model import (
    MEMBERS_FILE,
    MEMBERS_VERSION,
    MemberDataError,
    MemberProfile,
    member_key,
    normalize_profile,
    profile_from_dict,
    profile_to_dict,
)

__all__ = [
    "MemberProfileStore",
    "load_profiles",
    "members_path",
    "remove_profile",
    "save_profiles",
    "upsert_profile",
]

#: 群友档案在配置目录下的相对位置。
GUI_SUBDIR = "gui"

#: 文件更新时间格式（与旧版 ``gui/members.json`` 一致）。
UPDATED_AT_FORMAT = "%Y-%m-%d %H:%M:%S"


def members_path(config_dir: str) -> str:
    """档案文件路径（``<配置目录>/gui/members.json``）。"""
    return os.path.join(config_dir or "", GUI_SUBDIR, MEMBERS_FILE)


def _file_signature(path: str) -> tuple[int, int] | None:
    """文件指纹 ``(mtime_ns, size)``；不存在返回 ``None``，其他错误返回 ``None``。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def _read_file(path: str) -> tuple[dict[str, MemberProfile], str]:
    """读全表，返回 ``(profiles, error)``；``error`` 非空表示文件存在但读不出来。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"读取群友档案失败：{exc}"
    if not isinstance(data, dict):
        return {}, "群友档案结构无效：顶层不是对象"
    raw_members = data.get("members")
    if not isinstance(raw_members, dict):
        return {}, "群友档案结构无效：缺少 members 表"
    profiles: dict[str, MemberProfile] = {}
    for key, raw in raw_members.items():
        profile = profile_from_dict(raw)
        if profile.platform and profile.user_id:
            profiles[str(key)] = profile
    return profiles, ""


class MemberProfileStore:
    """群友档案存储（进程内缓存 + 文件指纹热更新；线程安全）。

    GUI 与后台服务各持有一个实例也可以：读取按指纹自动跟随文件变化，写入走
    ``.tmp`` + ``os.replace``，不会读到半写内容。
    """

    def __init__(self, config_dir: str, *, logger: Any = None) -> None:
        self.config_dir = config_dir or ""
        self.logger = logger
        self._lock = threading.RLock()
        self._loaded = False
        self._signature: tuple[int, int] | None = None
        self._profiles: dict[str, MemberProfile] = {}
        self._error = ""
        self._logged_error = ""

    # -- 元信息 -------------------------------------------------------------
    @property
    def path(self) -> str:
        """档案文件路径。"""
        return members_path(self.config_dir)

    @property
    def last_error(self) -> str:
        """最近一次读取失败的原因（成功读取后清空）。"""
        with self._lock:
            return self._error

    # -- 读取 ---------------------------------------------------------------
    def load(self) -> dict[str, MemberProfile]:
        """读全部档案（文件不存在/损坏时返回空表）。"""
        with self._lock:
            self._refresh_locked()
            return dict(self._profiles)

    def list(self) -> list[MemberProfile]:
        """全部档案（按 key 排序，便于展示）。"""
        with self._lock:
            self._refresh_locked()
            return [self._profiles[key] for key in sorted(self._profiles)]

    def get(
        self, *, platform: str, scope_id: str, user_id: str
    ) -> MemberProfile | None:
        """按 ``平台 + 完整作用域 + 用户 id`` 精确取一份档案。"""
        key = member_key(platform, scope_id, user_id)
        if not (platform or "").strip() or not (user_id or "").strip():
            return None
        with self._lock:
            self._refresh_locked()
            return self._profiles.get(key)

    def _refresh_locked(self) -> bool:
        """文件变了就重读；返回是否真的重读了。"""
        signature = _file_signature(self.path)
        if self._loaded and signature == self._signature:
            return False
        profiles, error = _read_file(self.path)
        self._loaded = True
        self._profiles = profiles
        self._error = error
        if error:
            # 读失败时不记指纹、也清掉"已加载"：下次调用再试一次（可能是文件正在被
            # 替换或被临时占用）。损坏文件通常很小，重读成本可以忽略。
            self._loaded = False
            self._signature = None
            if error != self._logged_error:
                self._logged_error = error
                self._warn("群友档案读取失败，已按空表处理", error)
            return True
        self._logged_error = ""
        self._signature = signature
        return True

    # -- 写入 ---------------------------------------------------------------
    def save(self, profiles: dict[str, MemberProfile]) -> str:
        """整表写回；返回文件路径。空表也会写（保留一个合法的空文件）。"""
        with self._lock:
            return self._save_locked(profiles)

    def upsert(self, profile: MemberProfile) -> MemberProfile:
        """保存一份档案（新增或覆盖），返回归一化并盖了时间戳的对象。"""
        normalized = normalize_profile(profile)
        normalized.updated_at = datetime.now().strftime(UPDATED_AT_FORMAT)
        with self._lock:
            self._read_for_write_locked()
            profiles = dict(self._profiles)
            profiles[normalized.key] = normalized
            self._save_locked(profiles)
        return normalized

    def remove(self, key: str) -> MemberProfile | None:
        """删一份档案，返回被删的对象（没找到返回 ``None``）。"""
        key = (key or "").strip()
        if not key:
            return None
        with self._lock:
            self._read_for_write_locked()
            profile = self._profiles.pop(key, None)
            if profile is None:
                return None
            self._save_locked(self._profiles)
            return profile

    def _read_for_write_locked(self) -> None:
        """写前先读；读失败时拒绝写，避免用空表覆盖掉用户的档案。"""
        self._refresh_locked()
        if self._error:
            raise MemberDataError(
                "群友档案文件损坏或无法读取，已拒绝覆盖；请先修复或删除 "
                f"{self.path}（{self._error}）"
            )

    def _save_locked(self, profiles: dict[str, MemberProfile]) -> str:
        path = self.path
        directory = os.path.dirname(path)
        payload = {
            "version": MEMBERS_VERSION,
            "members": {
                key: profile_to_dict(profile) for key, profile in sorted(profiles.items())
            },
        }
        temporary = path + ".tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise MemberDataError(f"写入群友档案失败：{exc}") from exc
        # 写完直接命中新内容，不必等下一次调用再读一遍。
        self._loaded = True
        self._profiles = dict(profiles)
        self._signature = _file_signature(path)
        self._error = ""
        return path

    def _warn(self, message: str, error: str) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.warn(message, event="member_profile_unreadable", error=error)
        except Exception:  # noqa: BLE001 - 日志失败不能影响对话
            pass


# ---------------------------------------------------------------------------
# 便捷函数：旧 GUI 接口（每次新建临时 store，语义与直接读写文件一致）
# ---------------------------------------------------------------------------
def load_profiles(config_dir: str) -> dict[str, MemberProfile]:
    """读全部档案；文件不存在/损坏时返回空表。"""
    return MemberProfileStore(config_dir).load()


def save_profiles(config_dir: str, profiles: dict[str, MemberProfile]) -> str:
    """整表写回；返回文件路径。空表也会写（保留一个合法的空文件）。"""
    return MemberProfileStore(config_dir).save(profiles)


def upsert_profile(config_dir: str, profile: MemberProfile) -> MemberProfile:
    """保存一份档案（新增或覆盖），返回归一化后并盖了时间戳的对象。"""
    return MemberProfileStore(config_dir).upsert(profile)


def remove_profile(config_dir: str, key: str) -> MemberProfile | None:
    """删一份档案，返回被删的对象（没找到返回 ``None``）。"""
    return MemberProfileStore(config_dir).remove(key)
