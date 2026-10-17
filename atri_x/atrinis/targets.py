"""Atrinis 投递目标解析：``all`` 展开、禁用规则、去重排序与落库 JSON 还原。

对应 Go：``internal/atrinis/targets.go``。

解析分两级：

1. **配置级禁用**（``delivery_disabled.targets`` 与每个 atriwisp 的
   ``disabled_targets``）。禁用项是 ``platform`` + 可选 ``type``/``id`` 的模式：
   只写 platform 表示"该平台下一律禁投"，写了 type/id 才精确匹配。
2. **``platform="all"`` 展开**成"当前已启用的平台"，展开后再过一次禁用规则——
   所以禁用某个平台可以同时挡住显式目标与 ``all``。

``direct`` 模式下解析结果为空是**硬错误**（"no allowed delivery targets"）：
direct 的语义就是"投出去"，没有落点等于调用方写错了。
"""

from __future__ import annotations

import json

from atri_x.atriena import ModeDirect, Request, Target
from atri_x.atrinis.prepare import unique_targets
from atri_x.config import AtrinisTargetConfig
from atri_x.errors import InvalidError

__all__ = [
    "TargetsMixin",
    "config_targets_to_atrinis",
    "decode_resolved_targets",
    "disabled_target_matches",
]


def disabled_target_matches(disabled: Target, target: Target) -> bool:
    """禁用模式是否命中目标。对应 Go ``disabledTargetMatches``。

    ``type`` 与 ``id`` 都空表示"该平台全禁"；否则要求 type 与 id 都相等
    （注意 Go 用的是**相等**而不是"模式匹配"，所以禁 ``private:1`` 不会连带
    禁 ``private:2``）。
    """
    if disabled.platform != target.platform:
        return False
    if not disabled.type and not disabled.id:
        return True
    return disabled.type == target.type and disabled.id == target.id


def config_targets_to_atrinis(targets: list[AtrinisTargetConfig] | None) -> list[Target]:
    """配置项 → 协议目标（逐字段 trim）。对应 Go ``configTargetsToAtrinis``。"""
    out: list[Target] = []
    for target in targets or []:
        out.append(
            Target(
                platform=(target.platform or "").strip(),
                type=(target.type or "").strip(),
                id=(target.id or "").strip(),
            )
        )
    return out


def decode_resolved_targets(raw: str) -> list[Target]:
    """从落库 JSON 还原解析后的目标列表。对应 Go ``decodeResolvedTargets``。

    解析失败抛 :class:`ValueError`（Go 返回 error）。``null`` 还原成空列表。
    """
    targets = json.loads(raw or "")
    if targets is None:
        return []
    if not isinstance(targets, list):
        raise ValueError("resolved targets must be a JSON array")
    return [Target.from_json(item) for item in targets]


class TargetsMixin:
    """目标解析。对应 Go ``targets.go`` 里的 ``Service`` 方法。

    混入方需提供 ``cfg`` 与 ``enabled_platforms``（见
    :class:`atri_x.atrinis.service.Service`）。
    """

    def resolve_targets(self, req: Request) -> list[Target]:
        """解析请求目标。对应 Go ``resolveTargets``（返回 error 时抛异常）。"""
        resolved: list[Target] = []
        for target in req.targets or []:
            if target.platform == "all":
                for platform_name in self.enabled_target_platforms():
                    candidate = Target(platform=platform_name)
                    if not self.target_disabled(req.atriwisp.name, candidate):
                        resolved.append(candidate)
                continue
            if not self.target_disabled(req.atriwisp.name, target):
                resolved.append(target)
        resolved = unique_targets(resolved)
        if not resolved and req.mode == ModeDirect:
            raise InvalidError("no allowed delivery targets")
        return resolved

    def enabled_target_platforms(self) -> list[str]:
        """``all`` 展开时使用的平台清单。对应 Go ``enabledTargetPlatforms``。"""
        return self.enabled_platforms

    def target_disabled(self, atriwisp_name: str, target: Target) -> bool:
        """目标是否被全局或该 atriwisp 的配置禁用。对应 Go ``targetDisabled``。"""
        for disabled in config_targets_to_atrinis(self.cfg.delivery_disabled.targets):
            if disabled_target_matches(disabled, target):
                return True
        policy = (self.cfg.atriwisps or {}).get(atriwisp_name)
        if policy is not None:
            for disabled in config_targets_to_atrinis(policy.disabled_targets):
                if disabled_target_matches(disabled, target):
                    return True
        return False
