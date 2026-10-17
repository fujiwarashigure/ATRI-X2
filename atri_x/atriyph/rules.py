"""对应 Go：``internal/atriyph/rules.go`` —— Atriyph 的固定常量与规则卡。

``rule_card()`` 的输出与 Go 的 ``RuleCard()`` **逐字相同**（含末尾的示例，
``parser_test.go`` 的 ``TestRuleCardExampleParses`` 依赖 ``例：`` 之后的内容
能被 :func:`atri_x.atriyph.parser.parse_skill` 解析）。
"""

from __future__ import annotations

__all__ = ["FORMAT", "SKILL_FILE_NAME", "rule_card"]

#: ATRI-X 原生 skill 的强结构说明文件名（Go：``SkillFileName``）。
SKILL_FILE_NAME = "SKILL.atriyph"

#: 语言标识（Go：``Format``）。
FORMAT = "atriyph"


def rule_card() -> str:
    """返回 discover Atriyph skill detail 时按需注入给 LLM 的极短规则卡。"""
    return (
        "Atriyph v0.3规则：非空行首必须是#,//,<-,->,$,=>,**,~,?if,?else,each,step,>,@tool,@skill,}；"
        "符号：#头；//整行注释；<-输入；->输出；$直接赋值；=>推导；**约束；~禁止（内容不再用否定形式）；"
        "?if/?else分支；each限次循环；step命名阶段（名首字符小写字母或数字，禁嵌套/空/重名；"
        "单句用 step name: 语句 单行形式，块内至少2句）；>输出文本；@tool工具；@skill技能；}闭块。"
        "#skill/#task 名称 - 描述；IO：<- $x:type!、-> $y:type；+按左侧type，int/num相加，str拼接；"
        "推导三元：=> $x = 条件 ? 真 : 假；块用{ }且}独行。\n"
        "** 注：Atriyph内容应精简、保持少歧义。** 为原子约束，每行独立。不当成md格式使用\n"
        "例：\n"
        "// 注释只能整行\n"
        "#skill weather - 查天气\n"
        "<- $city:str!\n"
        "<- $days:list?\n"
        "-> $remind:str\n"
        "** 根据查询结果判断\n"
        "~ 编造天气\n"
        "step fetch {\n"
        "  $q:str = $city + 天气\n"
        "  $w = @tool web_search(query=$q)\n"
        "}\n"
        "step decide {\n"
        "  => $rain = $w 是否下雨 ? 是 : 否\n"
        "  ?if($rain) {\n"
        "    > 记得带伞\n"
        "  }\n"
        "  ?else {\n"
        "    > 今天不用带伞\n"
        "  }\n"
        "}\n"
        "step notify: > 已通知 $city\n"
        "each($day in $days, limit=3) {\n"
        "  @skill weather_day(city=$city, day=$day)\n"
        "}"
    )
