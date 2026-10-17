"""工具 schema 与元信息的声明式构造器。

对应 Go：``internal/tool/builder.go``。

命名说明：Go 的 ``Builder`` 方法名与其私有字段同名（``Risk`` / ``risk``），
Python 里这两者会撞名，因此字段一律私有化（``_risk``），对外只暴露 Go 同名
方法的 snake_case 形式（``risk(...)``），调用姿势与 Go 一致：
``Builder("x").description("d").risk(RiskHigh).build_info()``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.tool.tool import (
    Info,
    RiskLow,
    SourceBuiltin,
    normalize_names,
    normalize_risk,
    normalize_tags,
)


@dataclass
class ParamConfig:
    """对应 Go ``paramConfig``。"""

    required: bool = False
    items: dict[str, Any] | None = None
    enum: list[str] = field(default_factory=list)


# ParamOption 是一次参数配置回调。对应 Go ``ParamOption``。
ParamOption = Callable[[ParamConfig], None]


def required() -> ParamOption:
    """对应 Go ``Required``。"""

    def option(cfg: ParamConfig) -> None:
        cfg.required = True

    return option


def items(typ: str) -> ParamOption:
    """对应 Go ``Items``。"""

    def option(cfg: ParamConfig) -> None:
        cfg.items = {"type": typ}

    return option


def enum(*values: str) -> ParamOption:
    """对应 Go ``Enum``。"""

    def option(cfg: ParamConfig) -> None:
        cfg.enum = list(values)

    return option


class Builder:
    """工具声明构造器。对应 Go ``Builder``。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self._description = ""
        self._source: str = SourceBuiltin
        self._risk: str = RiskLow
        self._superadmin_only = False
        self._hidden = False
        self._owner_scoped = False
        self._foreground_only = False
        self._vision_required = False
        self._tags: list[str] = []
        self._depends_on: list[str] = []
        self._properties: dict[str, Any] = {}
        self._required: list[str] = []

    # -- 元信息 -------------------------------------------------------------
    def description(self, description: str) -> "Builder":
        """对应 Go ``Builder.Description``。"""
        self._description = description
        return self

    def source(self, source: str) -> "Builder":
        """对应 Go ``Builder.Source``。"""
        self._source = source
        return self

    def risk(self, risk: str) -> "Builder":
        """对应 Go ``Builder.Risk``。"""
        self._risk = risk
        return self

    def hidden(self) -> "Builder":
        """对应 Go ``Builder.Hidden``。"""
        self._hidden = True
        return self

    def tags(self, *tags: str) -> "Builder":
        """对应 Go ``Builder.Tags``。"""
        self._tags.extend(tags)
        return self

    def superadmin_only(self) -> "Builder":
        """对应 Go ``Builder.SuperadminOnly``。"""
        self._superadmin_only = True
        return self

    def owner_scoped(self) -> "Builder":
        """对应 Go ``Builder.OwnerScoped``。"""
        self._owner_scoped = True
        return self

    def foreground_only(self) -> "Builder":
        """对应 Go ``Builder.ForegroundOnly``。"""
        self._foreground_only = True
        return self

    def vision_required(self) -> "Builder":
        """对应 Go ``Builder.VisionRequired``：模型不支持图片时隐藏该工具。"""
        self._vision_required = True
        return self

    def depends_on(self, *names: str) -> "Builder":
        """对应 Go ``Builder.DependsOn``。"""
        self._depends_on.extend(normalize_names(names))
        return self

    # -- 参数 ---------------------------------------------------------------
    def string(self, name: str, description: str, *opts: ParamOption) -> "Builder":
        """对应 Go ``Builder.String``。"""
        return self._param(name, "string", description, opts)

    def integer(self, name: str, description: str, *opts: ParamOption) -> "Builder":
        """对应 Go ``Builder.Integer``。"""
        return self._param(name, "integer", description, opts)

    def boolean(self, name: str, description: str, *opts: ParamOption) -> "Builder":
        """对应 Go ``Builder.Boolean``。"""
        return self._param(name, "boolean", description, opts)

    def object(self, name: str, description: str, *opts: ParamOption) -> "Builder":
        """对应 Go ``Builder.Object``：额外开放 ``additionalProperties``。"""
        self._param(name, "object", description, opts)
        prop = self._properties.get(name)
        if isinstance(prop, dict):
            prop["additionalProperties"] = True
        return self

    def string_array(self, name: str, description: str, *opts: ParamOption) -> "Builder":
        """对应 Go ``Builder.StringArray``。"""
        return self._param(name, "array", description, (*opts, items("string")))

    def object_array(
        self,
        name: str,
        description: str,
        properties: dict[str, Any],
        required_names: Sequence[str] | None = None,
        *opts: ParamOption,
    ) -> "Builder":
        """对应 Go ``Builder.ObjectArray``。"""
        cfg = _merge_options(opts)
        items_schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required_names:
            items_schema["required"] = list(required_names)
        self._properties[name] = {
            "type": "array",
            "description": description,
            "items": items_schema,
        }
        if cfg.required:
            self._required = _append_unique(self._required, name)
        return self

    # -- 产出 ---------------------------------------------------------------
    def build_info(self) -> Info:
        """对应 Go ``Builder.BuildInfo``。"""
        return Info(
            name=self._name,
            description=self._description,
            source=self._source,
            risk=normalize_risk(self._risk, RiskLow),
            superadmin_only=self._superadmin_only,
            hidden=self._hidden,
            owner_scoped=self._owner_scoped,
            foreground_only=self._foreground_only,
            vision_required=self._vision_required,
            tags=normalize_tags(self._tags),
            depends_on=normalize_names(self._depends_on),
        )

    def build_schema(self) -> ToolSchema:
        """对应 Go ``Builder.BuildSchema``。"""
        parameters: dict[str, Any] = {"type": "object", "properties": self._properties}
        if self._required:
            parameters["required"] = list(self._required)
        return ToolSchema(
            type="function",
            function=ToolFunctionSchema(
                name=self._name,
                description=self._description,
                parameters=parameters,
            ),
        )

    # -- 内部 ---------------------------------------------------------------
    def _param(
        self, name: str, typ: str, description: str, opts: Sequence[ParamOption]
    ) -> "Builder":
        cfg = _merge_options(opts)
        prop: dict[str, Any] = {"type": typ, "description": description}
        if cfg.items is not None:
            prop["items"] = cfg.items
        if cfg.enum:
            prop["enum"] = list(cfg.enum)
        self._properties[name] = prop
        if cfg.required:
            self._required = _append_unique(self._required, name)
        return self


def _merge_options(opts: Sequence[ParamOption]) -> ParamConfig:
    cfg = ParamConfig()
    for opt in opts or ():
        if opt is not None:
            opt(cfg)
    return cfg


def _append_unique(values: list[str], value: str) -> list[str]:
    if value in values:
        return values
    return [*values, value]


def new_builder(name: str) -> Builder:
    """对应 Go ``NewBuilder``。"""
    return Builder(name)


__all__ = [name for name in dir() if not name.startswith("_")]
