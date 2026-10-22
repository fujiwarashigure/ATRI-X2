"""工具参数定义：「图片 / 画师串 / 视觉」类工具。

对应界面：「工具权限 → 工具参数（全局）」子选项卡里 `image_generate`、`image_to_prompt`
与 5 个 `artist_*` 工具的面板（一个工具对应一个或多个面板）。静态定义与写盘规则的分工见
:mod:`atri_x.gui.toolsettings_general` 的模块说明。

维护约定：只收录 ``atri_x/config/services.py`` 里真的被工具读的键；生图的
Base URL / 密钥 / 模型名仍在「进阶」页（那里有「获取可用模型」），这里只放行为参数。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.toolsettings_base import SettingField, SettingGroup

__all__ = ["MEDIA_GROUPS"]

_F = SettingField


def _require_vision_backend(
    section: str, title: str
) -> Callable[[Mapping[str, Any]], None]:
    """跨字段校验：显式「开」了视觉功能，就必须同时填 provider + model。

    对应后台 :func:`atri_x.app.image_to_prompt.build_image_prompt_service` /
    :func:`atri_x.app.vision.build_vision_describer` 的启动期硬校验：只写
    ``enabled = true`` 会让下一次「重启后台服务」直接失败（不再是"模型从不被调用"）。
    """
    def validate(values: Mapping[str, Any]) -> None:
        if values.get("enabled") is not True:
            return
        provider = str(values.get("provider") or "").strip()
        model = str(values.get("model") or "").strip()
        if provider and model:
            return
        raise ConfigEditError(
            f"「{title}」选了「开」就必须同时填 provider 和 model"
            f"（当前 provider={provider!r} model={model!r}）；"
            f"否则后台服务重启时会因 [{section}] is enabled but incomplete 起不来。"
            f"没有可用的看图模型就先选「（默认）」或「关」。"
        )

    return validate

#: 生图质量下拉。
_QUALITY_CHOICES = (
    ("", "（默认 high）"),
    ("low", "low 低"),
    ("medium", "medium 中"),
    ("high", "high 高"),
)

#: 生图输出格式下拉。
_FORMAT_CHOICES = (
    ("", "（默认 png）"),
    ("png", "png"),
    ("jpeg", "jpeg"),
    ("webp", "webp"),
)

MEDIA_GROUPS: tuple[SettingGroup, ...] = (
    # -- 生图：基础行为 -----------------------------------------------------
    SettingGroup(
        "image_core",
        "生图：基础行为（image_generate）",
        ("image_generate",),
        (
            _F("enabled", "启用生图工具", "bool", "image_generation", "services", False, help="关掉后 image_generate 不注册。"),
            _F("size", "图片尺寸", "str", "image_generation", "services", "1024x1024", help="例如 1024x1024、1536x1024。"),
            _F("quality", "质量档位", "choice", "image_generation", "services", "high", _QUALITY_CHOICES, help="取决于中转站是否支持。"),
            _F("output_format", "输出格式", "choice", "image_generation", "services", "png", _FORMAT_CHOICES, help="png / jpeg / webp。"),
            _F("timeout_seconds", "单次生成超时（秒）", "int", "image_generation", "services", 180, help="出图请求的总超时。"),
            _F(
                "auto_character",
                "自动选角",
                "tristate",
                "image_generation",
                "services",
                help="从提示词里识别角色名 / 别名并自动带上角色设定；默认开。",
            ),
            _F(
                "auto_context",
                "自动拉群聊上下文",
                "tristate",
                "image_generation",
                "services",
                help="出图前自动读取当前群聊最近几条消息做参考；默认开。",
            ),
            _F("context_default_limit", "自动上下文条数", "int", "image_generation", "services", 6, help="auto_context 生效时取多少条消息。"),
            _F(
                "superadmin_only",
                "仅超级管理员可用",
                "tristate",
                "image_generation",
                "services",
                help="默认开：只有超级管理员能调用 image_generate（按调用者角色判断）。",
            ),
            _F(
                "save_to_character",
                "出图写回角色",
                "tristate",
                "image_generation",
                "services",
                help="把生成的图存进当前角色的 images/；默认开。",
            ),
            _F("send_by_default", "生成后自动发送", "bool", "image_generation", "services", False, help="默认关：先只回图片信息，由模型再决定发送。"),
            _F(
                "supports_reference",
                "支持参考图",
                "bool",
                "image_generation",
                "services",
                False,
                help="中转站支持参考图时打开：会把角色图片按 reference_field 透传。",
            ),
            _F("reference_field", "参考图字段名", "str", "image_generation", "services", "image", help="supports_reference 打开时用。"),
            _F(
                "negative_prompt_field",
                "负向提示词字段名",
                "str",
                "image_generation",
                "services",
                "negative_prompt",
                help="负向提示词默认作为独立字段发送时用的字段名。",
            ),
            _F(
                "negative_prompt_mode",
                "负向提示词发送方式",
                "choice",
                "image_generation",
                "services",
                "field",
                (
                    ("", "（默认 field）"),
                    ("field", "field 独立字段"),
                    ("inline", "inline 折进正向"),
                    ("off", "off 丢弃"),
                ),
                help="加权标签模型会把 inline 的「avoid: …」当成要画的东西，所以默认独立字段。",
            ),
            _F("max_concurrent", "生图并发上限", "int", "image_generation", "services", 0, help="0 表示不限制。"),
            _F("queue_size", "排队长度", "int", "image_generation", "services", 0, help="并发满时最多排几个；0 表示不排队。"),
            _F("queue_timeout_seconds", "排队超时（秒）", "int", "image_generation", "services", 0, help="排队的请求最多等多久；0 表示不限时。"),
        ),
        note="Base URL / 密钥 / 模型名在「进阶」页改（那里有「获取可用模型」与模型备注名）。",
        where="「进阶」页的生图区",
    ),
    # -- 生图：提示词 -------------------------------------------------------
    SettingGroup(
        "image_prompt",
        "生图：提示词与优化（image_generate）",
        ("image_generate",),
        (
            _F(
                "preset_prompt",
                "预设提示词",
                "text",
                "image_generation",
                "services",
                help="每张图都会拼在最前面的画风 / 质量约定；最终 prompt = 预设 + 角色预设 + 场景描述。",
            ),
            _F(
                "negative_prompt",
                "负向提示词",
                "text",
                "image_generation",
                "services",
                help="不想出现的内容；发送方式见「生图：基础行为」里的 negative_prompt_mode。",
            ),
            _F("max_prompt_runes", "提示词最长字符", "int", "image_generation", "services", 4000, help="超过会被拒绝或截断。"),
            _F(
                "optimize",
                "提示词优化",
                "choice",
                "image_generation",
                "services",
                "rules",
                (("", "（默认 rules）"), ("off", "off 关闭"), ("rules", "rules 规则库")),
                help="rules 用内置的 GPT Image Prompts 规则库补镜头 / 光线 / 画风锚点。",
            ),
            _F(
                "optimize_term_mode",
                "优化锚点写法",
                "choice",
                "image_generation",
                "services",
                "phrase",
                (("", "（默认 phrase）"), ("phrase", "phrase 短语锚点"), ("tag", "tag 单词标签")),
                help="phrase 拼成短语；tag 输出单词标签串。",
            ),
            _F("optimize_max_anchors", "最多补几个锚点", "int", "image_generation", "services", 4, help="镜头 / 光线 / 画风等锚点数量上限。"),
            _F("optimize_max_negatives", "最多补几条负向", "int", "image_generation", "services", 10, help="自动补充的负向提示词条数上限。"),
            _F("optimize_max_added_runes", "最多补多少字符", "int", "image_generation", "services", 400, help="自动补充内容的总字符上限。"),
            _F("optimize_max_tags", "最多几个 tag", "int", "image_generation", "services", 12, help="tag 模式下最多输出几个标签。"),
            _F(
                "optimize_rewrite",
                "LLM 语义改写",
                "choice",
                "image_generation",
                "services",
                "auto",
                (
                    ("", "（默认 auto）"),
                    ("off", "off 关闭"),
                    ("auto", "auto 短/含糊才改写"),
                    ("always", "always 总是改写"),
                ),
                help="用命名模型把短提示词改写成完整描述；auto 只在太短或太含糊时触发。",
            ),
            _F(
                "optimize_rewrite_model",
                "改写用哪个槽位",
                "choice",
                "image_generation",
                "services",
                "naming",
                (
                    ("", "（默认 naming）"),
                    ("naming", "naming 命名模型"),
                    ("compact", "compact 压缩模型"),
                    ("chat", "chat 聊天模型"),
                    ("work", "work 工作模型"),
                ),
                help="改写走哪个模型槽位（省钱的选 naming / compact）。",
            ),
            _F("optimize_rewrite_min_runes", "触发改写的最短长度", "int", "image_generation", "services", 40, help="短于该字符数的提示词才会触发 auto 改写。"),
        ),
    ),
    # -- 画师串 -------------------------------------------------------------
    SettingGroup(
        "artist",
        "画师串与生成参数留档（artist_*）",
        (
            "artist_extract",
            "artist_library_search",
            "artist_library_read",
            "artist_library_save",
            "artist_library_delete",
        ),
        (
            _F(
                "enabled",
                "启用画师串库",
                "tristate",
                "image_generation.artist_library",
                "services",
                help="默认由 root 是否配置决定；显式关掉后 artist_library_* 全部提示未启用。",
            ),
            _F(
                "root",
                "画师串库文件",
                "path",
                "image_generation.artist_library",
                "services",
                help="相对 services.toml 所在目录，例如 artist_library.json；留空 = 不启用可写串库。",
            ),
            _F(
                "metadata_store",
                "生成参数留档文件（metadata_store）",
                "path",
                "image_generation",
                "services",
                help=(
                    "就是 image_generation.metadata_store。填一个 JSON 文件名（相对 services.toml "
                    "所在目录）即开启；留空即关闭。入库图片超过 [media] 限制会被转成 JPEG，"
                    "PNG/EXIF 参数就在那一步丢失；开启后会在转码前先另存一份参数，artist_extract "
                    "之后仍能读回提示词。只对开启之后新入库的图有效：平台（QQ 等）转存过的图、"
                    "截图与二次编辑本来就没有参数，开了也救不回来。"
                ),
            ),
            _F(
                "artist_default_preset",
                "默认画师串",
                "str",
                "image_generation",
                "services",
                help="出图没显式指定画师串时使用的组合 id。",
            ),
            _F(
                "prompt_dialect",
                "提示词方言",
                "choice",
                "image_generation",
                "services",
                None,
                (
                    ("", "（自动推断）"),
                    ("natural", "natural 自然语言"),
                    ("sd_weighted", "sd_weighted 加权标签"),
                    ("nai", "nai"),
                    ("tags", "tags"),
                ),
                help="决定画师串怎么渲染；留空按 model_family 推断。",
            ),
            _F(
                "model_family",
                "模型家族",
                "str",
                "image_generation",
                "services",
                help="例如 gpt_image / flux / sdxl；只在 prompt_dialect 留空时用来推断方言。",
            ),
        ),
        note=(
            "artist_extract 读图片参数不需要画师串库，只有组合的保存 / 检索需要 root；"
            "想救回转码前的参数必须开 metadata_store。"
        ),
    ),
    # -- 图片反推提示词 -----------------------------------------------------
    SettingGroup(
        "image_to_prompt",
        "图片反推提示词（image_to_prompt）",
        ("image_to_prompt",),
        (
            _F(
                "enabled",
                "启用反推",
                "tristate",
                "image_to_prompt",
                "services",
                help=(
                    "显式关掉即强制停用；留空时配好 provider + model 就算启用。"
                    "选「开」必须同时填 provider + model（否则后台服务重启会失败）。"
                ),
            ),
            _F("provider", "视觉 provider", "str", "image_to_prompt", "services", help="复用 [providers.*] 里的一个 provider。"),
            _F("model", "视觉 model", "str", "image_to_prompt", "services", help="必须是该 provider 支持的看图模型（要能接受图片输入）。"),
            _F("max_tokens", "返回提示词上限", "int", "image_to_prompt", "services", 400, help="限制返回给聊天模型的 token 数。"),
            _F("temperature", "温度", "float", "image_to_prompt", "services", 0.2, help="0 表示不覆盖上游自己的温度。"),
            _F("max_edge", "上传前长边上限", "int", "image_to_prompt", "services", 1536, help="0 表示保留原图尺寸。"),
            _F("max_image_bytes", "图片字节上限", "int", "image_to_prompt", "services", 12582912, help="12 MiB = 12582912。"),
            _F("timeout_seconds", "请求超时（秒）", "int", "image_to_prompt", "services", 90, help="单次反推请求的超时。"),
        ),
        note=(
            "没配 provider + model 时 image_to_prompt 工具不会注册（群里看不到它，"
            "模型只能用 artist_extract 读图片元数据）。"
        ),
        validate=_require_vision_backend("image_to_prompt", "图片反推提示词（image_to_prompt）"),
    ),
)
