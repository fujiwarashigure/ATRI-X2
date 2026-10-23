"""工具参数定义：「文本 / 联网 / 文件 / 记忆 / 群聊」类工具。

对应界面：「工具权限」选项卡的第三个子选项卡「工具参数（全局）」。这里只放**静态定义**
（哪些键、什么类型、默认值、说明），读写与渲染分别是 :mod:`atri_x.gui.toolsettings`
和 :mod:`atri_x.gui.toolparams`；图片 / 画师 / 视觉类的定义在
:mod:`atri_x.gui.toolsettings_media`。

除了第一个「全局参数」面板，这里还有两个**无工具**面板 ``safety_chat`` /
``safety_work``：内容安全（道德限制 + 敏感信息过滤）的「按会话模式覆盖」
（``app.toml`` 的 ``[safety.chat]`` / ``[safety.work]``，``None`` = 继承全局
``[safety]``）。它们不挂任何工具，但同样是整机生效的全局作用域面板，所以由
:mod:`atri_x.gui.toolparams` 排在「全局参数」之后、各工具分类之前渲染，并且同一套
:class:`~atri_x.gui.toolparams.SettingPanel` 还会嵌进「chat 模式」/「work 模式」
两个子选项卡的顶部。

维护约定：

- ``tools`` 里的工具名必须真的在 :mod:`atri_x.gui.toolcatalog` 的目录里（导入时会校验），
  面板标题右边会显示这些工具名；
- 只收录**真的会被对应工具读**的配置键（在 ``atri_x/app/tool_runtime.py`` /
  ``atri_x/app/runtime.py`` 里能找到消费方），不要凭文档想象加键；
- ``where`` 用来提示"同一份配置在别的界面页也能改"，避免用户以为两处会互相覆盖。
"""

from __future__ import annotations

from atri_x.gui.toolsettings_base import SettingField, SettingGroup

__all__ = ["GENERAL_GROUPS"]

_F = SettingField

#: 风险等级下拉（``[security]`` 的两处阈值共用）。
_RISK_CHOICES = (
    ("", "（默认）"),
    ("safe", "safe 安全"),
    ("low", "low 低风险"),
    ("medium", "medium 中风险"),
    ("high", "high 高风险"),
)


def _safety_override_fields(section: str) -> tuple[SettingField, ...]:
    """内容安全「按会话模式覆盖」的 7 个字段（chat / work 各一套，只有小节名不同）。

    这里只放**能按模式覆盖**的键：``audit_path`` 是全局唯一的审计文件、词表类别 /
    敏感信息类别 / 逐类动作 / 白名单要手改 ``app.toml``（``categories`` /
    ``sensitive`` / ``sensitive_actions`` / ``allowlist``），所以都不进覆盖面板。
    """
    return (
        _F(
            "enabled",
            "启用内容安全",
            "tristate",
            section,
            "app",
            help="（默认）= 继承全局；开 / 关 = 本模式单独生效（另一个模式不受影响）。",
        ),
        _F(
            "mode",
            "内容安全执行模式",
            "choice",
            section,
            "app",
            None,
            (
                ("", "（继承全局）"),
                ("off", "off（关闭）"),
                ("audit", "audit（只记审计，不改输出）"),
                ("enforce", "enforce（真正拦截/脱敏）"),
            ),
            help="（继承全局）= 跟随 [safety].mode；建议先在全局用 audit 观察误杀再切 enforce。",
        ),
        _F(
            "block_reply",
            "入站拦截回复文本",
            "text",
            section,
            "app",
            help="留空 = 继承全局；全局也没配就用内置文案。",
        ),
        _F(
            "max_scan_chars",
            "安全扫描字符上限",
            "int",
            section,
            "app",
            help="留空 = 继承全局；全局也没配就用内置默认 8000。",
        ),
        _F(
            "strip_tool_images",
            "工具结果剥离图片",
            "tristate",
            section,
            "app",
            help="（默认）= 继承全局；开启后本模式的工具结果里图片段变成文字占位。",
        ),
        _F(
            "block_image_output",
            "禁止出站图片",
            "tristate",
            section,
            "app",
            help="（默认）= 继承全局；开启后本模式不再把图片发到平台并记审计。",
        ),
        _F(
            "block_file_output",
            "禁止出站文件",
            "tristate",
            section,
            "app",
            help="（默认）= 继承全局；开启后本模式不再把文件发到平台并记审计。",
        ),
    )


#: 覆盖面板底部说明的共同部分（两个模式只差名字）。
_SAFETY_OVERRIDE_NOTE = (
    "这几项是「按会话模式覆盖全局默认」：留「（默认）」/ 留空 = 继承全局 [safety]，"
    "保存时只写你改过的键（写进 app.toml 的 [{section}]）；保存后重启后台服务才生效。"
    "细调项（词表类别 categories / 敏感信息类别 sensitive / 逐类动作 / 白名单 allowlist）"
    "与审计日志路径 audit_path 仍只认全局，需要手改 app.toml。"
)

GENERAL_GROUPS: tuple[SettingGroup, ...] = (
    # -- 全局（所有工具）----------------------------------------------------
    SettingGroup(
        "global",
        "全局参数（所有工具）",
        (),
        (
            _F(
                "max_rounds_per_turn",
                "一轮最多工具调用",
                "int",
                "tools",
                "app",
                10,
                help="一轮对话里最多执行几次工具调用；超过后模型必须直接回答。",
            ),
            _F(
                "user_max_tool_risk",
                "普通用户可用最高风险",
                "choice",
                "security",
                "app",
                "low",
                _RISK_CHOICES,
                help="普通群友能用到的最高风险等级；超过即拒绝（超级管理员不受此限）。",
            ),
            _F(
                "superadmin_confirm_risk",
                "超管二次确认起点",
                "choice",
                "security",
                "app",
                "high",
                _RISK_CHOICES,
                help="超级管理员调用的工具达到该风险等级时，执行前要再确认一次。",
            ),
            _F(
                "tool_timeout_seconds",
                "单次工具超时（秒）",
                "int",
                "ops",
                "app",
                600,
                help="超过该时长的工具调用会被取消；0 表示不限时。",
            ),
            _F(
                "max_concurrent_tools",
                "并发工具上限",
                "int",
                "ops",
                "app",
                10,
                help="同时运行的工具数量上限；0 表示不限制（超出会排队或拒绝）。",
            ),
            # -- 内容安全（道德限制 + 敏感信息过滤，1.0.3）----------------------
            _F(
                "enabled",
                "启用内容安全",
                "tristate",
                "safety",
                "app",
                help="默认关；关着时完全不影响任何输出（不配置=不改变行为）。",
            ),
            _F(
                "mode",
                "内容安全执行模式",
                "choice",
                "safety",
                "app",
                None,
                (
                    ("", "（默认 audit）"),
                    ("off", "off（关闭）"),
                    ("audit", "audit（只记审计，不改输出）"),
                    ("enforce", "enforce（真正拦截/脱敏）"),
                ),
                help="建议先 audit 观察一两天误杀，再切 enforce。",
            ),
            _F(
                "audit_path",
                "安全审计日志路径",
                "path",
                "safety",
                "app",
                path_kind="file",
                help="留空 = <配置目录>/safety-audit.jsonl；只存 sha256 + 脱敏预览，不存原文。",
            ),
            _F(
                "block_reply",
                "入站拦截回复文本",
                "text",
                "safety",
                "app",
                help="入站消息被拦截时回给用户的话；留空用内置文案。",
            ),
            _F(
                "max_scan_chars",
                "安全扫描字符上限",
                "int",
                "safety",
                "app",
                help="超长文本只扫描前 N 个字符，避免拖慢链路；留空用内置默认 8000。",
            ),
            _F(
                "strip_tool_images",
                "工具结果剥离图片",
                "tristate",
                "safety",
                "app",
                help="开启后工具结果里的图片段变成文字占位（模型看不到图）；默认关。",
            ),
            _F(
                "block_image_output",
                "禁止出站图片",
                "tristate",
                "safety",
                "app",
                help="开启后图片不再发到平台，替换成一条文本说明并记审计；默认关。",
            ),
            _F(
                "block_file_output",
                "禁止出站文件",
                "tristate",
                "safety",
                "app",
                help="开启后文件不再发到平台，替换成一条文本说明并记审计；默认关。",
            ),
        ),
        note=(
            "工具授权（谁能调用）在 chat / work 两个子选项卡里按群配置；这里改的是"
            "所有工具共用的执行参数。内容安全的词表类别、敏感信息类别与逐类动作在"
            "app.toml 的 [safety] 段手工配置。"
        ),
    ),
    # -- 内容安全：按会话模式覆盖（1.0.3）----------------------------------
    SettingGroup(
        "safety_chat",
        "内容安全：chat 模式覆盖",
        (),
        _safety_override_fields("safety.chat"),
        note=_SAFETY_OVERRIDE_NOTE.format(section="safety.chat"),
        where=(
            "同一套字段还嵌在「工具权限 → chat 模式」子选项卡的顶部（两处保存同一份配置："
            "在那边保存后这里会自动更新，反过来保存后需要重新载入本页）。"
        ),
    ),
    SettingGroup(
        "safety_work",
        "内容安全：work 模式覆盖",
        (),
        _safety_override_fields("safety.work"),
        note=_SAFETY_OVERRIDE_NOTE.format(section="safety.work"),
        where=(
            "同一套字段还嵌在「工具权限 → work 模式」子选项卡的顶部（两处保存同一份配置："
            "在那边保存后这里会自动更新，反过来保存后需要重新载入本页）。"
        ),
    ),
    # -- 联网 ---------------------------------------------------------------
    SettingGroup(
        "web_search",
        "网页搜索通道（web_search）",
        ("web_search",),
        (
            _F(
                "engine_order",
                "搜索通道顺序",
                "list",
                "web_search",
                "services",
                help=(
                    "按顺序逐条尝试：deepseek / tavily / firecrawl / keenable / exa / "
                    "searxng / antigravity / grok。留空 = 环境变量默认链。"
                ),
            ),
        ),
        note="通道的密钥、服务地址与增删操作在「模型 → 搜索服务」子页里配，这里只调顺序。",
    ),
    SettingGroup(
        "web_extract",
        "网页抓取通道（web_extract）",
        ("web_extract",),
        (
            _F(
                "engine_order",
                "抓取通道顺序",
                "list",
                "web_extract",
                "services",
                help=(
                    "按顺序逐条尝试：jina / deepseek / direct。留空 = Jina（有 key 才用）"
                    "→ 直接抓取。"
                ),
            ),
        ),
        note="通道的密钥与服务地址在「模型 → 抓取服务」子页里配，这里只调顺序。",
    ),
    # -- 文件与执行 ---------------------------------------------------------
    SettingGroup(
        "file_exec",
        "工作区与命令执行",
        ("workspace", "read_file", "edit_file", "rollback_file", "shell"),
        (
            _F(
                "root",
                "沙箱根目录",
                "path",
                "sandbox",
                "app",
                help="shell 与文件工具的相对路径基准；留空 = 配置目录下的 sandbox。",
                path_kind="dir",
            ),
            _F(
                "background_shell_allow",
                "后台 shell 命令白名单",
                "list",
                "security",
                "app",
                help=(
                    "定时任务 / Atrinis 等无人值守场景允许自动执行的命令名；留空 = 内置"
                    "只读白名单；填 none = 后台 shell 全部拒绝。"
                ),
            ),
            _F(
                "background_shell_allow_all",
                "后台 shell 全放行",
                "bool",
                "security",
                "app",
                False,
                help="危险开关：后台只拦 critical 命令，其余自动放行（默认关）。",
            ),
        ),
        note="shell 的具体风险判定在 atri_x/tool/builtin/shell_risk.py，界面只控制白名单。",
    ),
    SettingGroup(
        "send_file",
        "发送文件（send_file）",
        ("send_file",),
        (
            _F(
                "send_file_mode",
                "QQ 发文件模式",
                "choice",
                "platform.qqonebot",
                "app",
                "base64",
                (("", "（默认 base64）"), ("base64", "base64 跨机可用"), ("file_uri", "file_uri 共享盘")),
                help="base64 任何机器都能用；file_uri 需要协议端与 ATRI-X 共享同一个文件系统。",
            ),
        ),
    ),
    # -- 图片媒体与视觉 -----------------------------------------------------
    SettingGroup(
        "media_vision",
        "媒体与视觉回退（get_media / view_image）",
        ("get_media", "view_image"),
        (
            _F(
                "llm_image_compression_threshold_bytes",
                "图片压缩阈值（字节）",
                "int",
                "media",
                "app",
                4194304,
                help="超过该大小的图片入库时会被压成 JPEG；4 MiB = 4194304。",
            ),
            _F(
                "llm_image_max_length",
                "图片最长边（像素）",
                "int",
                "media",
                "app",
                4096,
                help="压缩后图片的最长边上限。",
            ),
            _F(
                "enabled",
                "视觉回退",
                "tristate",
                "vision",
                "services",
                help="文本模型拒绝图片时，自动用视觉模型描述图片再重试。默认关。",
            ),
            _F("provider", "视觉 provider", "str", "vision", "services", help="留空 = 复用图片转提示词的 provider。"),
            _F("model", "视觉 model", "str", "vision", "services", help="留空 = 复用图片转提示词的 model。"),
            _F("max_edge", "视觉长边上限", "int", "vision", "services", 1536, help="上传前把长边缩到该像素。"),
            _F(
                "max_image_bytes",
                "视觉图片字节上限",
                "int",
                "vision",
                "services",
                12582912,
                help="单张图片上传上限；12 MiB = 12582912。",
            ),
            _F(
                "language",
                "描述语言",
                "choice",
                "vision",
                "services",
                None,
                (("", "（默认 zh）"), ("zh", "中文"), ("en", "英文")),
                help="视觉模型写图片描述用的语言。",
            ),
            _F(
                "detail",
                "图片精度（detail）",
                "choice",
                "vision",
                "services",
                None,
                (
                    ("", "（默认 original）"),
                    ("original", "original（推荐，原始分辨率）"),
                    ("high", "high"),
                    ("auto", "auto"),
                    ("low", "low（会缩到 512×512，不推荐）"),
                ),
                help="传给上游的图片精度；low 会把细节糊掉，认人/认字场景别用。",
            ),
            _F(
                "trigger",
                "识图触发时机",
                "choice",
                "vision",
                "services",
                None,
                (
                    ("", "（默认 on_error）"),
                    ("on_error", "on_error（仅模型拒图时兜底）"),
                    ("always", "always（每轮带图先描述成文字，两段式）"),
                ),
                help="always = 视觉模型只描述、主模型结合人设下结论；会多一次视觉请求（有缓存）。",
            ),
            _F(
                "probe_timeout_seconds",
                "前置描述超时（秒）",
                "float",
                "vision",
                "services",
                4.0,
                help="trigger=always 时的短超时；拿不到就降级说“图没取到”，不编造。范围 1~30。",
            ),
            _F(
                "allow_external_images",
                "允许图片外发",
                "tristate",
                "vision",
                "services",
                help="隐私总开关：关掉后任何图片都不发给外部视觉 API（私聊+群聊）。",
            ),
            _F(
                "allow_group_images",
                "允许群图外发",
                "tristate",
                "vision",
                "services",
                help="群聊隐私开关：关掉后群里的图不送外部视觉 API，私聊不受影响。",
            ),
            _F(
                "identity_enabled",
                "以图搜图兜底",
                "tristate",
                "vision",
                "services",
                help="用户明确问“这是谁/出自哪”且画面置信度低时，用 SauceNAO 搜一次（默认关，需 key）。",
            ),
            _F(
                "identity_api_key_env",
                "SauceNAO key 环境变量",
                "str",
                "vision",
                "services",
                "SAUCENAO_API_KEY",
                help="读 key 的环境变量名（也可写进配置目录 .env）；不填 key 时该功能不生效。",
            ),
            _F(
                "identity_daily_limit",
                "以图搜图每日上限",
                "int",
                "vision",
                "services",
                100,
                help="每天最多调用几次（UTC 日切）；0 表示不限。",
            ),
            _F(
                "identity_min_similarity",
                "以图搜图最低相似度",
                "float",
                "vision",
                "services",
                0.7,
                help="低于该相似度视为没找到，不写进描述。0~1。",
            ),
            _F(
                "identity_timeout_seconds",
                "以图搜图超时（秒）",
                "int",
                "vision",
                "services",
                8,
                help="SauceNAO 请求超时；失败只记原因、不编造。",
            ),
        ),
    ),
    # -- 记忆 ---------------------------------------------------------------
    SettingGroup(
        "resident_memory",
        "常驻记忆（resident_memory*）",
        (
            "resident_memory",
            "resident_memory_read",
            "resident_memory_normal",
            "resident_memory_core",
        ),
        (
            _F("core_max_units", "核心记忆上限", "int", "resident_memory", "app", 500, help="核心记忆的字符单位上限。"),
            _F("normal_max_units", "普通记忆上限", "int", "resident_memory", "app", 500, help="普通记忆的字符单位上限。"),
            _F(
                "normal_write_min_interval_seconds",
                "普通记忆写入间隔（秒）",
                "int",
                "resident_memory",
                "app",
                5,
                help="两次写入的最小间隔；显式写 0 可关闭该限制。",
            ),
            _F(
                "normal_write_window_seconds",
                "普通记忆限流窗口（秒）",
                "int",
                "resident_memory",
                "app",
                60,
                help="与下面「窗口内最大写入次数」配合使用。",
            ),
            _F(
                "normal_write_max_per_window",
                "窗口内最大写入次数",
                "int",
                "resident_memory",
                "app",
                6,
                help="一个限流窗口内最多写几次普通记忆。",
            ),
            _F("normal_max_lines", "普通记忆最大条数", "int", "resident_memory", "app", 20, help="0 表示不限制。"),
            _F(
                "normal_max_units_per_entry",
                "单条普通记忆上限",
                "int",
                "resident_memory",
                "app",
                80,
                help="单条普通记忆的字符单位上限；0 表示不限制。",
            ),
            _F(
                "normal_block_instruction_patterns",
                "拦截指令类内容",
                "tristate",
                "resident_memory",
                "app",
                help="拒绝「忽略之前的指令」这类提示词注入内容；默认开。",
            ),
        ),
    ),
    SettingGroup(
        "angel_memory",
        "长期记忆（angel_remember / recall / forget）",
        ("angel_remember", "angel_recall", "angel_forget"),
        (
            _F("enabled", "启用长期记忆", "tristate", "angel_memory", "app", help="默认开。"),
            _F("retention_days", "保留天数", "int", "angel_memory", "app", 365, help="0 表示不按时间清理。"),
            _F(
                "forget_on_recall",
                "撤回时删除派生记忆",
                "tristate",
                "angel_memory",
                "app",
                help="源消息被撤回时删除由它派生的长期记忆；默认开。",
            ),
            _F(
                "allow_tool_forget",
                "允许模型删除记忆",
                "tristate",
                "angel_memory",
                "app",
                help="放行 angel_forget 工具；默认关（模型发起是 opt-in）。",
            ),
            _F("max_content_runes", "单条记忆上限", "int", "angel_memory", "app", 1000, help="超过会拒绝写入；留空用内置默认 1000。"),
            _F("max_per_scope", "单个会话记忆上限", "int", "angel_memory", "app", 1000, help="留空用内置默认 1000。"),
            _F(
                "max_writes_per_minute",
                "每分钟写入上限",
                "int",
                "angel_memory",
                "app",
                30,
                help="单个会话每分钟最多写几次；留空用内置默认 30。",
            ),
            _F("max_context_runes", "每轮注入上限", "int", "angel_memory", "app", 1200, help="留空用内置默认 1200。"),
        ),
    ),
    SettingGroup(
        "self_learning",
        "自学习复核（self_learning_review）",
        ("self_learning_review",),
        (
            _F("enabled", "启用自学习", "tristate", "self_learning", "app", help="默认开；只有复核通过的内容才会注入上下文。"),
            _F("retention_days", "保留天数", "int", "self_learning", "app", 365, help="0 表示不按时间清理。"),
            _F("min_count", "进入复核的最少出现次数", "int", "self_learning", "app", 3, help="留空用内置默认 3。"),
            _F("min_users", "进入复核的最少用户数", "int", "self_learning", "app", 2, help="设为 1 允许单人复读形成候选。"),
            _F("max_meaning_runes", "候选含义上限", "int", "self_learning", "app", 200, help="单个候选含义的最大字符数。"),
            _F("max_context_runes", "每轮注入上限", "int", "self_learning", "app", 1200, help="每轮最多注入多少字符的学习上下文。"),
            _F("max_observation_runes", "单条观察上限", "int", "self_learning", "app", 1000, help="超过会截断后入库。"),
            _F(
                "max_observations_per_scope",
                "单会话观察上限",
                "int",
                "self_learning",
                "app",
                5000,
                help="超过会拒绝写入。",
            ),
            _F(
                "max_observation_writes_per_minute",
                "每分钟观察写入上限",
                "int",
                "self_learning",
                "app",
                60,
                help="单个会话每分钟最多写多少条观察。",
            ),
            _F("max_mine_chars", "单次挖掘扫描字符", "int", "self_learning", "app", 200000, help="单次挖掘最多扫描多少字符。"),
            _F("mine_timeout_seconds", "挖掘超时（秒）", "int", "self_learning", "app", 10, help="单次挖掘的超时时间。"),
        ),
    ),
    # -- 群聊与角色 ---------------------------------------------------------
    SettingGroup(
        "group_analysis",
        "群聊统计（group_analysis）",
        ("group_analysis",),
        (
            _F("enabled", "启用群聊统计", "tristate", "group_analysis", "app", help="默认开。"),
            _F("max_messages", "单次最多读取消息", "int", "group_analysis", "app", 5000, help="统计时最多扫描多少条本地历史消息。"),
            _F("report_enabled", "定时报告", "tristate", "group_analysis", "app", help="每天定时把统计发到指定会话；默认关。"),
            _F("report_schedule", "报告 cron", "str", "group_analysis", "app", "0 9 * * *", help="5 段 cron 表达式，默认每天 9:00。"),
            _F("report_platform", "报告平台", "str", "group_analysis", "app", help="例如 qqonebot；report_enabled 打开时必填。"),
            _F("report_scope_id", "报告目标会话", "str", "group_analysis", "app", help="例如 group:123456789。"),
            _F("report_days", "报告统计天数", "int", "group_analysis", "app", 1, help="每次报告统计最近几天。"),
        ),
    ),
    SettingGroup(
        "character_library",
        "角色库（character_*）",
        ("character_list", "character_read", "character_search", "character_manage", "character_delete"),
        (
            _F("enabled", "启用角色库", "tristate", "character_library", "app", help="默认开；关掉后角色相关工具全部不可用。"),
            _F(
                "root",
                "角色库目录",
                "path",
                "character_library",
                "app",
                "characters",
                help="相对配置目录；默认 characters。",
                path_kind="dir",
            ),
        ),
        where="角色的增删改也可以直接走「人设卡」选项卡。",
    ),
)
