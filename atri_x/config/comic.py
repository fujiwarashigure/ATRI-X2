"""``[comic]`` 段：聊天漫画的业务策略（Python 侧新增，1.1.6）。

单独成文件的原因：``services.py`` 已接近行数上限，而漫画策略与生图端点配置是
两块不同关注点——生图端点/密钥仍然在 ``[image_generation]``，这里只管"漫画怎么
编排"：规划器用哪个模型槽、一页几格、取材多少条、提示词上限。

"哪个平台能出头像"**不**在这里配置：平台适配器自己声明能力
（``platform.UserAvatarProvider``），配置里写死平台名单会和适配器实现漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from atri_x.config.app_sections import StrictConfig

__all__ = ["ComicConfig"]


@dataclass
class ComicConfig(StrictConfig):
    """``[comic]``：聊天漫画的业务策略。"""

    enabled: bool = True
    #: ``auto``：有规划模型就用模型，否则规则规划；``rules`` / ``llm`` 强制其中一种。
    planner: str = "auto"
    #: 规划模型槽：``naming`` / ``compact`` / ``chat`` / ``work``（缺省跟随 work）。
    plan_model: str = "work"
    language: str = "zh"
    layout: str = "grid"
    default_panel_count: int = 6
    max_panels: int = 12
    max_characters: int = 4
    context_limit: int = 12
    max_scene_runes: int = 240
    max_dialogue_runes: int = 120
    max_prompt_runes: int = 6000
    send_by_default: bool = True
