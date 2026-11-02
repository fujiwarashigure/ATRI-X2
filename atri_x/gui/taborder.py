"""主窗口选项卡顺序、名字索引与「功能」页跳转键（1.1.8 从 ``app.py`` 拆出）。

拆出来的原因：``app.py`` 在行数棘轮里只许变短，而主窗口本身已经很长；这张纯数据表
与 Tk 无关、测试也要直接断言（``tests/test_gui_tabs.py`` 按元素顺序检查页签），
放独立模块既守住预算，也让"加一页"只改一处。

``TABS`` 的**顺序就是界面上页签的顺序**，``_TAB_INDEX`` / ``TAB_KEYS`` 都由它派生：
新增页签时只改 ``TABS`` 并在 ``app.py`` 的工厂表里加一项；「功能」页要能双击跳转的
再加一条 ``TAB_KEYS`` 映射。
"""

from __future__ import annotations

__all__ = ["TAB_INDEX", "TAB_KEYS", "TABS"]

#: 选项卡顺序（索引 → 名字）；菜单与 Ctrl+N 快捷键共用。
TABS: tuple[str, ...] = (
    "对话",
    "模型",
    "人设卡",
    "平台",
    "在线",
    "群友",
    "知识库",
    "进阶",
    "时间",
    "日志",
    "功能",
    "工具权限",
    "研究",  # [atri-research]
    "权限",
    "提示词",
    "内容安全",  # 1.1.8
    "性能",  # 1.1.8
    "DSH",  # 1.1.9：内置 DSH 内核的深度路由配置
)

#: 选项卡名字 → 索引（供「功能」页双击跳转）。
TAB_INDEX: dict[str, int] = {name: index for index, name in enumerate(TABS)}

#: 「功能」页里的跳转键 → 选项卡名。
TAB_KEYS: dict[str, str] = {
    "model": "模型",
    "character": "人设卡",
    "platform": "平台",
    "advanced": "进阶",
    "time": "时间",
    "members": "群友",
    "knowledge": "知识库",
    "logs": "日志",
    "toolperm": "工具权限",
    # 「工具权限」页的第三个子选项卡「工具参数（全局）」：旧 [safety] 等全局参数在这里。
    "toolperm_params": "工具权限",
    # Python 版新增的子选项卡：工具进度预览（工具权限页）/ 表情图库（人设卡页）。
    "toolperm_preview": "工具权限",
    "character_reactions": "人设卡",
    # 超级管理员名单 / 风险阈值 / 工具轮次 / 成员建卡开关。
    "permission": "权限",
    # 内容安全策略 / 词表 / 复核队列 / 审计（1.1.8）。
    "safety": "内容安全",
    # 并发预设 / 上限 / 实况（1.1.8）。
    "performance": "性能",
    # 内置 DSH 内核的深度路由配置（1.1.9）。
    "dsh": "DSH",
}
