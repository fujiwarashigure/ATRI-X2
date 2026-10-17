"""``internal/tool/runtimeinfo`` 的 Python 移植。

Go 侧的 ``runtimeinfo.Info`` 是一小包“运行期环境事实”：配置目录 / 配置文件路径 /
沙箱根 / 文件投递设置，外加一个**可注入的时钟**（``Now func() time.Time``），
让工具实现与测试都不必直接依赖 ``time.Now``。

Python 侧保持同样的语义：:class:`Info` 是 frozen dataclass，:meth:`Info.normalize`
返回补全默认值后的**新实例**（对应 Go 的值语义），时钟字段可注入
``Callable[[], datetime]``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Optional

from ..atriyph import rule_card as _atriyph_rule_card
from ..config import FileDeliveryConfig, default as default_config

__all__ = ["Info", "first", "atriyph_rule_card"]


def _local_now() -> datetime:
    """带时区的当前时间。对应 Go ``time.Now()``。"""
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Info:
    """工具执行期可见的环境事实。对应 Go ``runtimeinfo.Info``。"""

    config_dir: str = ""
    config_path: str = ""
    sandbox_root: str = ""
    file_delivery: FileDeliveryConfig = field(default_factory=FileDeliveryConfig)
    now: Optional[Callable[[], datetime]] = None

    def normalize(self) -> "Info":
        """补全零值字段。对应 Go ``Info.Normalize``。

        规则（与 Go 逐条一致）：

        * ``config_dir`` 为空而 ``config_path`` 非空 → 取 ``dirname(clean(config_path))``；
        * ``config_path`` 为空而 ``config_dir`` 非空 → 取 ``config_dir/app.toml``；
        * ``sandbox_root`` 为空 → 取全局默认配置的 ``sandbox.root``；
        * ``file_delivery`` 里 ``max_direct_base64_bytes <= 0`` / ``backend == ""`` /
          ``s3_region == ""`` 三项按默认配置补齐（其余字段原样保留）；
        * ``now`` 为空 → 用本机当前时间函数。

        Go 用值接收者返回新结构体，这里返回 ``replace`` 出来的新实例。
        """
        config_dir = self.config_dir
        config_path = self.config_path
        if not config_dir and config_path:
            config_dir = os.path.dirname(os.path.normpath(config_path))
        if not config_path and config_dir:
            config_path = os.path.join(config_dir, "app.toml")

        sandbox_root = self.sandbox_root
        if not sandbox_root:
            sandbox_root = default_config().sandbox.root

        delivery = self.file_delivery
        defaults = default_config().file_delivery
        changes: dict = {}
        if delivery.max_direct_base64_bytes <= 0:
            changes["max_direct_base64_bytes"] = defaults.max_direct_base64_bytes
        if not delivery.backend:
            changes["backend"] = defaults.backend
        if not delivery.s3_region:
            changes["s3_region"] = defaults.s3_region
        if changes:
            delivery = replace(delivery, **changes)

        return replace(
            self,
            config_dir=config_dir,
            config_path=config_path,
            sandbox_root=sandbox_root,
            file_delivery=delivery,
            now=self.now or _local_now,
        )

    def current_time(self) -> datetime:
        """当前时间。对应 Go ``Info.CurrentTime``。

        注意：与 Go 一致，**不**隐式调用 :meth:`normalize`；``now`` 为空时直接取本机时间。
        """
        if self.now is not None:
            return self.now()
        return _local_now()


def first(*values: Info) -> Info:
    """取第一个并补全默认值；无参时返回补全后的空 ``Info``。对应 Go ``First``。"""
    if not values:
        return Info().normalize()
    return values[0].normalize()


def atriyph_rule_card() -> str:
    """对应 Go ``AtriyphRuleCard``（转发 ``atriyph.RuleCard()``）。"""
    return _atriyph_rule_card()
