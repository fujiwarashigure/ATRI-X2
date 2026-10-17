"""``atri_x/tool/discover.py`` 的单元测试。

对应 Go 测试：``internal/tool/discover_tag_test.go``（以及 ``registry_test.go``
里覆盖 ``DiscoverDetails`` 的部分）。
"""

from __future__ import annotations

import asyncio
import json
import unittest

from atri_x.errors import InvalidError
from atri_x.security import Actor, RoleUser, default_policy, with_actor, with_policy
from atri_x.tool.builder import Builder
from atri_x.tool.detail import DetailBlock
from atri_x.tool.discover import (
    DISCOVER_TOOL_NAME,
    DiscoverArgs,
    DiscoverTool,
    discovery_content,
    discovery_security,
    expand_discovery_tag_names,
    marshal_json_no_escape,
    new_discover_tool,
    with_discover_filter,
)
from atri_x.tool.tool import (
    METADATA_ACTIVATE_TOOLS,
    CallRequest,
    Info,
    Registry,
    Result,
    Role,
)


def _run(coro):
    return asyncio.run(coro)


def _call(arguments: dict | None = None) -> CallRequest:
    return CallRequest(
        id="1",
        name=DISCOVER_TOOL_NAME,
        arguments=json.dumps(arguments) if arguments is not None else "",
    )


class PlainTool:
    """最普通的工具：详情走 schema。"""

    def __init__(self, name: str, description: str, tags: tuple[str, ...] = ()) -> None:
        self._name = name
        self._description = description
        self._tags = list(tags)

    def name(self) -> str:
        return self._name

    def info(self) -> Info:
        return Builder(self._name).description(self._description).tags(*self._tags).build_info()

    def schema(self):
        return (
            Builder(self._name)
            .description(self._description)
            .string("x", "参数")
            .build_schema()
        )

    async def call(self, ctx, req) -> Result:  # pragma: no cover - 不被 discover 调用
        return Result(content="ok")


class DetailTool(PlainTool):
    """提供详情文本并声明激活工具。"""

    def detail(self) -> str:
        return "详情：" + self._name

    def activate_tools(self) -> list[str]:
        return ["extra"]


class OverrideTool(PlainTool):
    """自定义发现文案且 override=True。"""

    def discovery_content(self) -> tuple[str, bool]:
        return "自定义块", True


class HiddenTagTool(PlainTool):
    """隐藏工具：只能按 tag 发现。"""

    def info(self) -> Info:
        return (
            Builder(self._name)
            .description(self._description)
            .tags(*self._tags)
            .hidden()
            .build_info()
        )


class DiscoverArgsTest(unittest.TestCase):
    def test_empty_arguments(self) -> None:
        self.assertEqual(DiscoverArgs.parse(""), DiscoverArgs())

    def test_parses_name_and_names(self) -> None:
        args = DiscoverArgs.parse('{"name":"a","names":["b",1,"c"]}')
        self.assertEqual(args.name, "a")
        self.assertEqual(args.names, ["b", "c"])

    def test_rejects_invalid_json_and_shapes(self) -> None:
        for raw in ("{", "[]", '{"names":1}'):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidError):
                    DiscoverArgs.parse(raw)


class DiscoverToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = Registry()
        self.registry.register(PlainTool("alpha", "甲"))
        self.registry.register(PlainTool("beta", "乙", ("chat",)))
        self.registry.register(DetailTool("gamma", "丙"))
        self.registry.register(OverrideTool("delta", "丁"))
        self.registry.register(HiddenTagTool("hidden_one", "隐", ("chat",)))
        self.tool = new_discover_tool(self.registry)

    def test_name_and_schema(self) -> None:
        self.assertEqual(self.tool.name(), "discover_tool")
        schema = self.tool.schema()
        self.assertEqual(schema.function.name, "discover_tool")
        properties = schema.function.parameters["properties"]
        self.assertIn("name", properties)
        self.assertIn("names", properties)
        self.assertEqual(self.tool.info().risk, "safe")

    def test_list_skips_hidden_tools(self) -> None:
        result = _run(self.tool.call(None, _call()))
        names = [item["info"]["name"] for item in json.loads(result.data)["tools"]]
        self.assertIn("alpha", names)
        self.assertNotIn("hidden_one", names)

    def test_list_text_renders_name_and_description(self) -> None:
        # Go 的列举分支不带 schema，所以走 `name：description` 而不是"已发现工具"行。
        result = _run(self.tool.call(None, _call()))
        self.assertIn("alpha：甲", result.content)
        self.assertNotIn("已发现工具", result.content)

    def test_detail_tool_activates_tools(self) -> None:
        result = _run(self.tool.call(None, _call({"name": "gamma"})))
        self.assertEqual(result.metadata[METADATA_ACTIVATE_TOOLS], ["extra"])
        self.assertIn("详情：gamma", result.content)

    def test_schema_tools_use_found_tools_line(self) -> None:
        result = _run(self.tool.call(None, _call({"names": ["alpha"]})))
        self.assertIn("已发现工具：alpha", result.content)

    def test_tag_expands_to_matching_tools(self) -> None:
        # hidden 工具能被 tag 展开成名字，但 root 详情查询会按 Go 的规则拒掉它
        # （addDiscoveryDetail：`root && info.Hidden` → "not found or not allowed"）。
        result = _run(self.tool.call(None, _call({"name": "chat"})))
        payload = json.loads(result.data)
        names = [item["info"]["name"] for item in payload["tools"]]
        self.assertEqual(names, ["beta"])
        self.assertEqual([item["name"] for item in payload["errors"]], ["hidden_one"])

    def test_override_discovery_content(self) -> None:
        result = _run(self.tool.call(None, _call({"name": "delta"})))
        self.assertIn("自定义块", result.content)
        self.assertNotIn("已发现工具", result.content)

    def test_single_unknown_name_raises(self) -> None:
        with self.assertRaises(InvalidError) as ctx:
            _run(self.tool.call(None, _call({"name": "nope"})))
        self.assertIn("nope", str(ctx.exception))

    def test_unknown_names_report_errors(self) -> None:
        result = _run(self.tool.call(None, _call({"names": ["alpha", "nope"]})))
        payload = json.loads(result.data)
        self.assertEqual([item["info"]["name"] for item in payload["tools"]], ["alpha"])
        self.assertEqual(payload["errors"][0]["name"], "nope")
        self.assertIn("未发现工具", result.content)

    def test_before_discover_hook_runs(self) -> None:
        seen: list[int] = []

        async def hook(ctx) -> None:
            seen.append(1)

        tool = new_discover_tool(self.registry, hook)
        _run(tool.call(None, _call()))
        self.assertEqual(seen, [1])

    def test_filter_hides_tools(self) -> None:
        ctx = with_discover_filter(None, lambda _ctx, info: info.name != "alpha")
        result = _run(self.tool.call(ctx, _call()))
        names = [item["info"]["name"] for item in json.loads(result.data)["tools"]]
        self.assertNotIn("alpha", names)


class ExpandTagNamesTest(unittest.TestCase):
    def test_keeps_unknown_names(self) -> None:
        registry = Registry()
        registry.register(PlainTool("alpha", "甲", ("chat",)))
        out = expand_discovery_tag_names(registry, ["alpha", "chat", "nope"], lambda _t: True)
        self.assertEqual(out, ["alpha", "nope"])

    def test_empty_input(self) -> None:
        self.assertEqual(expand_discovery_tag_names(Registry(), [], lambda _t: True), [])


class DiscoveryContentTest(unittest.TestCase):
    def test_none_result_is_empty(self) -> None:
        self.assertEqual(_run(discovery_content(None, Registry(), None)), "")

    def test_errors_without_name_are_skipped(self) -> None:
        from atri_x.tool.detail import DiscoveryError, DiscoveryResult

        registry = Registry()
        result = DiscoveryResult(errors=[DiscoveryError(name="", reason="x")])
        self.assertEqual(_run(discovery_content(None, registry, result)), "")


class DiscoverySecurityTest(unittest.TestCase):
    def test_defaults(self) -> None:
        actor, policy = discovery_security(None)
        self.assertEqual(actor.role, RoleUser)
        self.assertIsNotNone(policy)

    def test_uses_context_values(self) -> None:
        actor = Actor(id="u1", role="user")
        policy = default_policy()
        ctx = with_policy(with_actor(None, actor), policy)
        got_actor, got_policy = discovery_security(ctx)
        self.assertEqual(got_actor.id, "u1")
        self.assertIs(got_policy, policy)


class MarshalTest(unittest.TestCase):
    def test_no_html_escaping(self) -> None:
        self.assertEqual(
            marshal_json_no_escape({"a": "<b>&</b>", "n": None}),
            '{"a":"<b>&</b>","n":null}',
        )

    def test_detail_block_is_not_serialized(self) -> None:
        from atri_x.tool.detail import DiscoveredTool, PublicInfo

        tool = DiscoveredTool(
            info=PublicInfo(name="a", description="d"),
            detail_block=DetailBlock(content="x"),
        )
        self.assertNotIn("x", marshal_json_no_escape(tool.to_json()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
