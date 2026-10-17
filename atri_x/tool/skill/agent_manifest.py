"""AgentSkill 清单（``ATRIX_SKILL.toml``）的读取、校验与 schema 投影。

对应 Go：``internal/tool/skill/agent_manifest.go``。

解析用标准库 :mod:`tomllib`（只读），与 Go 的 ``pelletier/go-toml/v2`` 行为
对齐到本文件用到的子集：表、数组、多行字面量字符串。

校验要点（逐条对齐 Go）：

* 只允许 8 个字段，出现别的键就报错并列出（排序后逗号分隔）；
* 「可调用」判定 = 有 ``command``、有 ``parameters`` 或有 ``args``；
* 可调用时 ``risk`` 必填、``command`` 非空且每段非空、``parameters`` 必须是
  合法 JSON 对象 schema 且 ``type == "object"``、``properties`` 非空、
  ``args`` 非空且每个映射名都在 ``properties`` 里、``required`` 里的每个名字
  都必须有 args 映射；
* 不可调用时只做 ``timeout_seconds >= 0`` 检查。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.tool.skill.parser import parse_risk
from atri_x.tool.tool import RiskSafe

__all__ = [
    "AGENT_SKILL_CONFIG_FILE",
    "AgentSkillManifest",
    "AgentSkillManifestFile",
    "agent_skill_config_path",
    "agent_skill_manifest_callable",
    "copy_string_map",
    "load_agent_skill_manifest",
    "normalize_manifest_tags",
    "parse_agent_skill_manifest",
    "project_media_schema",
    "schema_required",
]

#: 清单文件名。对应 Go ``AgentSkillConfigFile``。
AGENT_SKILL_CONFIG_FILE = "ATRIX_SKILL.toml"

#: 允许出现的顶层键（Go 侧是同一个字面量 map）。
_ALLOWED_FIELDS = frozenset(
    {
        "risk",
        "superadmin_only",
        "tags",
        "command",
        "timeout_seconds",
        "expose_root",
        "parameters",
        "args",
    }
)


@dataclass
class AgentSkillManifest:
    """校验通过的清单。对应 Go ``AgentSkillManifest``。"""

    risk: str = RiskSafe
    superadmin_only: bool = False
    tags: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 0
    expose_root: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)
    args: dict[str, str] = field(default_factory=dict)
    callable: bool = False

    def schema(self, name: str, description: str) -> ToolSchema:
        """投影成 LLM 工具 schema。对应 Go ``AgentSkillManifest.Schema``。"""
        parameters = project_media_schema(self.parameters)
        if not isinstance(parameters, dict):
            parameters = {}
        return ToolSchema(
            type="function",
            function=ToolFunctionSchema(
                name=name, description=description, parameters=parameters
            ),
        )


@dataclass
class AgentSkillManifestFile:
    """清单文件的原始（未校验）形态。对应 Go ``agentSkillManifestFile``。"""

    risk: str = ""
    superadmin_only: bool = False
    tags: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    timeout_seconds: int = 0
    expose_root: bool = False
    parameters: str = ""
    args: dict[str, str] = field(default_factory=dict)


def agent_skill_config_path(root: str) -> str:
    """清单文件在 skill 目录里的路径。对应 Go ``AgentSkillConfigPath``。"""
    return os.path.join(root, AGENT_SKILL_CONFIG_FILE)


def load_agent_skill_manifest(root: str) -> tuple[AgentSkillManifest, bool]:
    """读取并解析清单。对应 Go ``LoadAgentSkillManifest``。

    返回 ``(清单, 文件是否存在)``；文件不存在时返回默认清单与 ``False``，
    解析失败抛异常（与 Go 的 ``(manifest, found, err)`` 一致）。
    """
    path = agent_skill_config_path(root)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return AgentSkillManifest(), False
    except OSError as exc:
        raise OSError(f"read {AGENT_SKILL_CONFIG_FILE}: {exc}") from exc
    return parse_agent_skill_manifest(data), True


def parse_agent_skill_manifest(data: bytes | str) -> AgentSkillManifest:
    """解析并校验清单文本。对应 Go ``ParseAgentSkillManifest``。"""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"parse {AGENT_SKILL_CONFIG_FILE}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"parse {AGENT_SKILL_CONFIG_FILE}: expected a table")
    unknown = sorted(key for key in raw if key not in _ALLOWED_FIELDS)
    if unknown:
        raise ValueError(
            f"unknown {AGENT_SKILL_CONFIG_FILE} field(s): " + ", ".join(unknown)
        )
    return validate_agent_skill_manifest(_to_file(raw))


def _to_file(raw: dict[str, Any]) -> AgentSkillManifestFile:
    """把 tomllib 的结果规整成清单文件结构（类型不符时给出可读错误）。"""
    file = AgentSkillManifestFile()
    try:
        file.risk = _as_text(raw.get("risk"))
        file.superadmin_only = _as_bool(raw.get("superadmin_only"))
        file.tags = _as_text_list(raw.get("tags"))
        file.command = _as_text_list(raw.get("command"))
        file.timeout_seconds = _as_int(raw.get("timeout_seconds"))
        file.expose_root = _as_bool(raw.get("expose_root"))
        file.parameters = _as_text(raw.get("parameters"))
        file.args = _as_text_map(raw.get("args"))
    except ValueError as exc:
        raise ValueError(f"parse {AGENT_SKILL_CONFIG_FILE}: {exc}") from exc
    return file


def validate_agent_skill_manifest(file: AgentSkillManifestFile) -> AgentSkillManifest:
    """校验清单文件结构。对应 Go ``validateAgentSkillManifest``。"""
    callable_ = agent_skill_manifest_callable(file)
    risk = RiskSafe
    if (file.risk or "").strip() != "":
        risk = parse_risk(file.risk)
    elif callable_:
        raise ValueError("risk is required")
    if not callable_:
        if file.timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        return AgentSkillManifest(
            risk=risk,
            superadmin_only=file.superadmin_only,
            tags=normalize_manifest_tags(file.tags),
            timeout_seconds=file.timeout_seconds,
            expose_root=file.expose_root,
        )
    if not file.command:
        raise ValueError("command is required")
    for index, part in enumerate(file.command):
        if (part or "").strip() == "":
            raise ValueError(f"command[{index}] is empty")
    if (file.parameters or "").strip() == "":
        raise ValueError("parameters is required")
    try:
        parameters = json.loads(file.parameters)
    except json.JSONDecodeError as exc:
        raise ValueError(f"parameters must be JSON object schema: {exc}") from exc
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be JSON object schema")
    if parameters.get("type") != "object":
        raise ValueError("parameters.type must be object")
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("parameters.properties is required")
    if not file.args:
        raise ValueError("[args] is required")
    for raw_name, flag in file.args.items():
        name = (raw_name or "").strip()
        if name == "":
            raise ValueError("args contains empty parameter name")
        if (flag or "").strip() == "":
            raise ValueError(f"args.{name} is empty")
        if name not in properties:
            raise ValueError(f"args.{name} is not defined in parameters.properties")
    for name in schema_required(parameters):
        if name not in file.args:
            raise ValueError(f"required parameter {name} has no args mapping")
    if file.timeout_seconds < 0:
        raise ValueError("timeout_seconds must be >= 0")
    return AgentSkillManifest(
        risk=risk,
        superadmin_only=file.superadmin_only,
        tags=normalize_manifest_tags(file.tags),
        command=list(file.command),
        timeout_seconds=file.timeout_seconds,
        expose_root=file.expose_root,
        parameters=parameters,
        args=copy_string_map(file.args),
        callable=True,
    )


def agent_skill_manifest_callable(file: AgentSkillManifestFile) -> bool:
    """对应 Go ``agentSkillManifestCallable``。"""
    return (
        len(file.command) > 0
        or (file.parameters or "").strip() != ""
        or len(file.args) > 0
    )


def schema_required(parameters: dict[str, Any] | None) -> list[str]:
    """取 JSON schema 的 ``required`` 名字列表。对应 Go ``schemaRequired``。"""
    if not isinstance(parameters, dict):
        return []
    value = parameters.get("required")
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item != "":
            out.append(item)
    return out


def copy_string_map(values: dict[str, str] | None) -> dict[str, str]:
    """对应 Go ``copyStringMap``。"""
    return {key: value for key, value in (values or {}).items()}


def normalize_manifest_tags(tags: list[str] | None) -> list[str]:
    """trim + 去重 + 保序。对应 Go ``normalizeManifestTags``。"""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        tag = (tag or "").strip()
        if tag == "" or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def project_media_schema(value: Any) -> Any:
    """把自定义 ``type: "media"`` 投影成字符串 + 媒体 ID 正则。

    对应 Go ``projectMediaSchema``。递归处理 ``array.items`` 与 ``object.properties``
    （浅拷贝，不改动原 schema）。
    """
    if not isinstance(value, dict):
        return value
    projected = dict(value)
    kind = value.get("type")
    if kind == "media":
        projected["type"] = "string"
        projected["pattern"] = "^media:[0-9a-f]{64}$"
    elif kind == "array":
        if "items" in value:
            projected["items"] = project_media_schema(value["items"])
    elif kind == "object":
        properties = value.get("properties")
        if isinstance(properties, dict):
            projected["properties"] = {
                name: project_media_schema(property_)
                for name, property_ in properties.items()
            }
    return projected


# ---------------------------------------------------------------------------
# TOML 值的类型规整（tomllib 会给出原生类型，这里只做防御性转换）
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError("expected a string value")


def _as_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError("expected a boolean value")


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("expected an integer value")
    if isinstance(value, int):
        return value
    raise ValueError("expected an integer value")


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected an array of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("expected an array of strings")
        out.append(item)
    return out


def _as_text_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected a table of strings")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise ValueError("expected a table of strings")
        out[str(key)] = item
    return out
