"""审查回归：门禁失败语义；不导入被检查的真实应用模块。"""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class GuiSelftestCleanupTests(unittest.TestCase):
    def test_withdraw_failure_also_destroys_created_root(self):
        from unittest.mock import Mock
        from atri_x.gui import selftest
        root = SimpleNamespace(withdraw=Mock(side_effect=selftest.tk.TclError("synthetic")), destroy=Mock())
        with patch.object(selftest.tk, "Tk", return_value=root), patch.object(selftest, "safe_print"):
            self.assertEqual(selftest.run_selftest(), 1)
        root.destroy.assert_called_once()

    def test_construction_failure_destroys_created_root(self):
        from unittest.mock import Mock
        from atri_x.gui import selftest
        root = SimpleNamespace(withdraw=Mock(), destroy=Mock())
        with patch.object(selftest.tk, "Tk", return_value=root), patch.object(
                selftest, "GuiApp", side_effect=RuntimeError("synthetic")), patch.object(selftest, "safe_print"):
            self.assertEqual(selftest.run_selftest(), 1)
        root.destroy.assert_called_once()


class ExternalEventWireTypeTests(unittest.TestCase):
    def test_timeout_rejects_values_outside_signed_int64(self):
        from atri_x.atriena.types import tool_declaration_from_json
        for value in (2**63, -(2**63)-1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tool_declaration_from_json({"timeout_seconds": value})
        self.assertEqual(tool_declaration_from_json({"timeout_seconds": 30}).timeout_seconds, 30)

    def test_atriwisp_tags_are_strings_without_coercion(self):
        from atri_x.atriena.types import Atriwisp
        for item in (123, True, {}, [], None):
            with self.subTest(item=item), self.assertRaises(ValueError):
                Atriwisp.from_json({"tags": [item]})
        self.assertIsNone(Atriwisp.from_json({}).tags)
        self.assertEqual(Atriwisp.from_json({"tags": []}).tags, [])
        self.assertEqual(Atriwisp.from_json({"tags": ["safe"]}).tags, ["safe"])

    def test_tool_names_and_timeouts_reject_wrong_json_types(self):
        from atri_x.atriena.types import Request, tool_declaration_from_json
        for item in (123, True, {}, [], None):
            with self.subTest(item=item), self.assertRaises(ValueError):
                Request.from_json({"tool_list_names": [item]})
        with self.assertRaises(ValueError):
            tool_declaration_from_json({"timeout_seconds": True})
        self.assertEqual(Request.from_json({"tool_list_names": ["web_search"]}).tool_list_names, ["web_search"])


class PlatformInt64BoundaryTests(unittest.TestCase):
    def test_invalid_numeric_values_return_none_without_conversion_errors(self):
        from atri_x.atriena.calls import to_int64
        for value in (float("nan"), float("inf"), -float("inf"), 2**63, -(2**63)-1,
                      str(2**63), "1_000", "１２", 1.5, True):
            with self.subTest(value=value):
                self.assertIsNone(to_int64(value))
        for value in (0, 2**63-1, -(2**63), "42", "+42", 42.0):
            self.assertEqual(to_int64(value), int(value))


class PlatformCallCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_cancellation_propagates_and_stops_later_calls(self):
        import asyncio
        from unittest.mock import AsyncMock
        from atri_x.atriena.calls import execute_calls
        with patch("atri_x.atriena.calls.execute_call", new=AsyncMock(side_effect=asyncio.CancelledError())) as execute:
            with self.assertRaises(asyncio.CancelledError):
                await execute_calls(None, object(), [object(), object()])
            self.assertEqual(execute.await_count, 1)

    async def test_ordinary_failure_retains_batch_error_contract(self):
        from unittest.mock import AsyncMock
        from atri_x.atriena.calls import execute_calls, CallExecutionError
        with patch("atri_x.atriena.calls.execute_call", new=AsyncMock(side_effect=ValueError("synthetic"))):
            with self.assertRaises(CallExecutionError) as caught:
                await execute_calls(None, object(), [object()])
            self.assertEqual(len(caught.exception.results), 1)


class ResidentClearConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_alias_requires_confirmation_before_any_delete(self):
        from tests.agent_commands_fakes import Harness
        harness = Harness()
        self.addCleanup(harness.store.close)
        result = await harness.router.dispatch(harness.ctx, "/forget resident clear")
        self.assertIn("需要确认", result.content)
        self.assertEqual(harness.resident_memory.normal_cleared, 0)
        self.assertEqual(harness.resident_memory.core_writes, [])


class ReactionZeroWeightDisplayTests(unittest.TestCase):
    def test_selecting_zero_weight_does_not_show_default_one(self):
        from unittest.mock import Mock
        from atri_x.gui.reactionpage import ReactionPanel
        panel = SimpleNamespace(tree=SimpleNamespace(selection=lambda: ["0"]), items=[{"id":"fake", "weight":0}])
        for name in ("emotion", "name", "weight", "enabled", "path"):
            setattr(panel, "item_" + name + "_var", SimpleNamespace(set=Mock()))
        ReactionPanel._on_select(panel)
        panel.item_weight_var.set.assert_called_once_with("0")


class TkTagPickerFilterTests(unittest.TestCase):
    def test_real_listbox_preserves_selected_hidden_entries(self):
        import tkinter as tk
        from atri_x.gui.taglibedit import TagPickerDialog
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        self.addCleanup(root.destroy)
        picker = TagPickerDialog.__new__(TagPickerDialog)
        picker._selected = ["handwritten"]
        picker._all = [SimpleNamespace(name=name, group="", aliases=[], description="") for name in ("alpha", "beta")]
        picker._names = ["alpha", "beta"]
        picker.search_var = tk.StringVar(master=root)
        picker.listbox = tk.Listbox(root, selectmode=tk.EXTENDED)
        picker._window = tk.Toplevel(root)
        picker._window.withdraw()
        picker._refill()
        picker.listbox.selection_set(1)
        picker.search_var.set("alpha")
        picker._refill()
        picker.listbox.selection_set(0)
        picker._confirm()
        self.assertEqual(picker.result, ["handwritten", "beta", "alpha"])


class TagPickerFilterStateTests(unittest.TestCase):
    def test_confirm_preserves_hidden_selected_tags_and_visible_changes(self):
        from unittest.mock import Mock
        from atri_x.gui.taglibedit import TagPickerDialog
        picker = SimpleNamespace(_selected=["hidden", "remove", "handwritten"],
            _visible=["remove", "new"], _names=["hidden", "remove", "new"],
            listbox=SimpleNamespace(curselection=lambda: (1,)), _window=SimpleNamespace(destroy=Mock()))
        picker._capture_selection = lambda: TagPickerDialog._capture_selection(picker)
        TagPickerDialog._confirm(picker)
        self.assertEqual(picker.result, ["hidden", "handwritten", "new"])


class TagPickerReplacementTests(unittest.TestCase):
    def test_deselected_registered_tag_is_not_reintroduced(self):
        from unittest.mock import Mock
        from atri_x.gui.taglibedit import apply_tag_picker
        variable = SimpleNamespace(get=lambda: "old, handwritten", set=Mock())
        tab = SimpleNamespace(paths=SimpleNamespace(config_dir="synthetic"), frame=None, journal=Mock())
        with patch("atri_x.gui.taglibedit.choose_tags", return_value=["new", "handwritten"]):
            apply_tag_picker(tab, variable, "synthetic")
        variable.set.assert_called_once_with("new, handwritten")


class YamlStringScalarTests(unittest.TestCase):
    def test_implicit_type_names_are_quoted_and_control_characters_rejected(self):
        from atri_x.gui.dshcatalog import _yaml_scalar
        from atri_x.gui.configedit import ConfigEditError
        for value in ("123", "true", "null", "1e3", "yes", "normal", "a'b"):
            rendered = _yaml_scalar(value)
            self.assertTrue(rendered.startswith("'") and rendered.endswith("'"))
            self.assertEqual(rendered[1:-1].replace("''", "'"), value)
        for value in ("a\nb", "a\rb", "a\x00b"):
            with self.subTest(value=value), self.assertRaises(ConfigEditError):
                _yaml_scalar(value)


class ProviderRenameSafetyTests(unittest.TestCase):
    def test_rename_is_rejected_before_deleting_or_writing_configuration(self):
        from atri_x.gui import configmodel
        from atri_x.gui.configedit import ConfigEditError
        view = configmodel.ProviderView(name="new")
        with patch.object(configmodel, "_providers_path", return_value="synthetic"), patch.object(
                configmodel, "remove_section") as remove, patch.object(configmodel, "patch_section") as write, patch.object(
                configmodel, "_verify_provider_saved"):
            with self.assertRaises(ConfigEditError):
                configmodel.save_provider(SimpleNamespace(), view, original="old")
            remove.assert_not_called()
            write.assert_not_called()


class ContextWindowFiniteTests(unittest.TestCase):
    def test_nonfinite_window_inputs_raise_config_error(self):
        from atri_x.gui.configmodel import parse_context_window
        from atri_x.gui.configedit import ConfigEditError
        for value in ("nan", "inf", "-inf", "1e999", "1e308m"):
            with self.subTest(value=value), self.assertRaises(ConfigEditError):
                parse_context_window(value)
        self.assertEqual(parse_context_window("512k"), 512000)


class ProviderLiteralKeyTests(unittest.TestCase):
    def test_provider_header_uses_literal_dotted_name(self):
        import tomllib
        from atri_x.gui import configmodel
        with patch.object(configmodel, "read_toml", return_value={}), patch.object(configmodel, "patch_section") as write:
            configmodel._save_extra_headers("synthetic", "a.b", {"X-Test": "fake"})
        header = write.call_args.args[1]
        parsed = tomllib.loads("[" + header + "]\nvalue=1\n")
        self.assertIn("a.b", parsed["providers"])


class ExceptionRatchetScopeTests(unittest.TestCase):
    def test_nested_function_raise_does_not_exempt_swallowing_handler(self):
        import ast
        path = Path(__file__).resolve().parents[1] / "scripts" / "check_except.py"
        spec = importlib.util.spec_from_file_location("audit_except_gate", path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        tree = ast.parse('try:\n pass\nexcept BaseException:\n def unused():\n  raise RuntimeError()\n pass\n')
        self.assertFalse(gate._reraises(tree.body[0].handlers[0]))
        direct = ast.parse('try:\n pass\nexcept BaseException:\n raise\n')
        self.assertTrue(gate._reraises(direct.body[0].handlers[0]))


class WorkspacePathTypeTests(unittest.TestCase):
    def test_path_does_not_coerce_structured_values_into_directory_names(self):
        from atri_x.tool.builtin.workspace import parse_workspace_args
        from atri_x.errors import InvalidError
        for raw in ('{"path":{}}', '{"path":[]}', '{"path":123}', '{"path":true}'):
            with self.subTest(raw=raw), self.assertRaises(InvalidError):
                parse_workspace_args(raw)
        self.assertEqual(parse_workspace_args('{"path":"relative"}').path, "relative")


class AstUnicodeLineBoundaryTests(unittest.TestCase):
    def test_unicode_separator_inside_go_string_does_not_shift_newline_index(self):
        from atri_x.tool.builtin.file_tools_ast import find_go_ast_matches
        source = 'package main\nvar s = "a\u2028b"; var 名 = target\n'
        match = find_go_ast_matches("synthetic.go", source, "target")[0]
        line = source.split("\n")[1]
        self.assertEqual(match.line, 2)
        self.assertEqual(match.column, len(line[:line.index("target")].encode("utf-8")) + 1)


class WorkspaceSandboxFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_lookup_failure_never_reaches_host_workspace(self):
        from atri_x.tool.builtin.workspace import WorkspaceTool
        from atri_x.tool.tool import CallRequest
        with patch("atri_x.sandbox.runtime.service_or_none", side_effect=RuntimeError("synthetic")), patch(
                "atri_x.tool.builtin.workspace.workspace_store_from_context") as host:
            with self.assertRaises(RuntimeError):
                await WorkspaceTool().call(None, CallRequest(arguments="{}"))
            host.assert_not_called()


class ToolSandboxLookupTests(unittest.TestCase):
    def test_shell_and_file_workspace_lookup_fail_closed(self):
        from atri_x.tool import workspace
        from atri_x.tool.builtin import shell
        for module in (workspace, shell):
            with self.subTest(module=module.__name__), patch(
                    "atri_x.sandbox.runtime.service_or_none", side_effect=RuntimeError("synthetic")):
                with self.assertRaises(RuntimeError):
                    module._sandbox_service()
        with patch("atri_x.sandbox.runtime.service_or_none", return_value=None):
            self.assertIsNone(workspace._sandbox_service())
            self.assertIsNone(shell._sandbox_service())


class WorkspaceStorageCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_storage_cancellation_is_not_wrapped(self):
        import asyncio
        from unittest.mock import AsyncMock
        from atri_x.tool.builtin.workspace import WorkspaceTool
        from atri_x.tool.tool import CallRequest
        store = SimpleNamespace(clear_workspace_dir=AsyncMock(side_effect=asyncio.CancelledError()))
        with patch("atri_x.tool.builtin.workspace._sandbox_service", return_value=None), patch(
                "atri_x.tool.builtin.workspace.workspace_store_from_context", return_value=(store, True)), patch(
                "atri_x.tool.builtin.workspace.default_workspace_dir", return_value="synthetic"):
            with self.assertRaises(asyncio.CancelledError):
                await WorkspaceTool().call(None, CallRequest(arguments='{"reset":true}'))


class DiscoveryProviderContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_context_and_async_noarg_returns_are_supported(self):
        from atri_x.tool.detail import load_discovery_content
        sync = SimpleNamespace(discovery_content=lambda ctx=None: ("sync", False))
        async def noarg():
            return "async", True
        self.assertEqual(await load_discovery_content(None, sync), ("sync", False, True))
        self.assertEqual(await load_discovery_content(None, SimpleNamespace(discovery_content=noarg)), ("async", True, True))

    async def test_optional_keyword_only_parameter_is_not_called_positionally(self):
        from atri_x.tool.detail import load_discovery_content
        def content(*, option=False):
            return "keyword", option
        self.assertEqual(await load_discovery_content(None, SimpleNamespace(discovery_content=content)), ("keyword", False, True))


class DiscoveryCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_detail_cancellation_propagates(self):
        import asyncio
        from unittest.mock import AsyncMock
        from atri_x.tool.detail import add_discovery_detail
        from atri_x.tool.tool import Info
        tool = SimpleNamespace(info=lambda: Info(name="synthetic"),
                               load_detail=AsyncMock(side_effect=asyncio.CancelledError()))
        registry = SimpleNamespace(get=lambda name: (tool, True))
        seen, visiting = {}, {}
        with self.assertRaises(asyncio.CancelledError):
            await add_discovery_detail(registry, None, "synthetic", True, lambda tool: True, seen, visiting, [], [])
        self.assertFalse(seen)
        self.assertFalse(visiting)


class StructureLongestMetricTests(unittest.TestCase):
    def test_longest_is_measured_even_without_long_files(self):
        from atri_x.devtools.astindex import RepoIndex, ModuleInfo, collect_metrics
        index = RepoIndex(root=Path("synthetic"), modules=(
            ModuleInfo(name="atri_x.fake", path="atri_x/fake.py", lines=123),))
        measured = collect_metrics(index)
        self.assertEqual(measured["longest"], ("atri_x/fake.py", 123))
        self.assertEqual(measured["long_files"], 0)


class DeployFakeEnvironmentTests(unittest.TestCase):
    def test_real_execution_escape_flag_is_overridden_in_cli_tests(self):
        from tests.test_deploy_harness_cli import TestCliEndToEnd
        fake = SimpleNamespace(write_capture=lambda rules: Path("synthetic-capture"),
                               base_rules=lambda: [], tmp=Path("synthetic"))
        with patch.dict("os.environ", {"ATRIX_DEPLOY_FAKE_ALLOW_REAL": "1"}), patch(
                "tests.test_deploy_harness_cli.subprocess.run", return_value=object()) as run:
            TestCliEndToEnd.run_cli(fake, "smoke.py", rules=[])
        self.assertEqual(run.call_args.kwargs["env"].get("ATRIX_DEPLOY_FAKE_ALLOW_REAL"), "0")


class ListApiExitTests(unittest.TestCase):
    def test_missing_requested_api_fails_even_with_grep(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "list_api.py"
        spec = importlib.util.spec_from_file_location("audit_list_api", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.importlib, "import_module", return_value=SimpleNamespace(present=1)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(["fake", "missing"]), 1)
            self.assertEqual(module.main(["fake", "missing", "--grep", "present"]), 1)
            self.assertEqual(module.main(["fake", "present"]), 0)


class ResearchEngineTests(unittest.TestCase):
    """随包研究引擎：筛空空集不得被放大、DOI 建议开关必须生效。"""

    @classmethod
    def setUpClass(cls):
        import sys
        cls.engine = str(Path(__file__).resolve().parents[1] / "research_pack" / "engine")
        if cls.engine not in sys.path:
            sys.path.insert(0, cls.engine)

    @classmethod
    def tearDownClass(cls):
        import sys
        for name in list(sys.modules):
            if name == "atrix_research" or name.startswith("atrix_research."):
                del sys.modules[name]
        try:
            sys.path.remove(cls.engine)
        except ValueError:
            pass

    def _library(self):
        return {"works": [{"title": "Alpha", "doi": "10.1000/a"}, {"title": "Beta"}]}

    def _fake_store(self, value):
        return SimpleNamespace(read=lambda *args, **kwargs: value)

    def test_finished_screening_with_empty_include_set_yields_no_works(self):
        from atrix_research.runtime import included_works, works
        library = self._library()
        self.assertEqual(len(works(library)), 2)
        self.assertEqual(included_works(self._fake_store({"included_keys": []}), library), [])
        self.assertEqual(len(included_works(self._fake_store(None), library)), 2)
        one = works(library)[0].key
        kept = included_works(self._fake_store({"included_keys": [one]}), library)
        self.assertEqual([work.key for work in kept], [one])

    def test_missing_doi_suggestion_can_be_disabled(self):
        from unittest.mock import patch
        from atrix_research import verify
        from atrix_research.models import Work
        work = Work(title="Alpha")
        with patch.object(verify.crossref, "search", side_effect=AssertionError("network")) as search:
            off = verify.verify_library(object(), [work], suggest_missing_doi=False)
        search.assert_not_called()
        self.assertEqual(off["items"][0]["status"], "NO_DOI")
        with patch.object(verify.crossref, "search", return_value=[]) as search:
            verify.verify_library(object(), [work], suggest_missing_doi=True)
        self.assertEqual(search.call_count, 1)

