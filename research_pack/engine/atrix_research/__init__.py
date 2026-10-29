"""ATRI-X 科研研究引擎（原创实现）。

设计约束（决定本包长什么样）：

* **确定性优先**：每个工具的输出必须可由输入复算；网络结果全部落盘缓存，
  同一份缓存下重跑结果一致。
* **不调用 LLM**：需要"写"的环节由 AgentSkill 提示词完成，本包只提供结构、
  校验与聚合，因此换任何模型都不改变流程与判定标准。v0.2 起把"下一步顺序"（flow）、
  "格式与引用标记"（compose）、"可度量评分"（audit）也收进代码，提示词只剩内容与表达。
* **零依赖可降级**：仅用标准库即可完成检索 / 去重 / 筛选 / 核验 / 引用 /
  QA / 评审聚合；``python-docx``、``reportlab``、``pypdf`` 装在独立 venv 里，
  缺失时导出降级为 HTML/Markdown 并在结果里显式声明，绝不假装成功。
"""

from __future__ import annotations

#: 引擎版本；``doctor`` 与运行记录都写它，便于对账。
VERSION = "0.2.0"

#: 运行记录 / 产物 JSON 的 schema 标识。
SCHEMA = "atrix-research/1"

__all__ = ["SCHEMA", "VERSION"]
