"""Skill 工具包：AgentSkill / Go skill 的发现、运行与创建。

对应 Go：``internal/tool/skill/``（2555 行）。模块划分与 Go 文件一一对应，
``process_unix.go`` + ``process_windows.go`` 合并为 :mod:`atri_x.tool.skill.process`
（按 :data:`atri_x.tool.skill.process.IS_WINDOWS` 分支）。

职责地图：

============================  ======================================
模块                          职责（Go 文件）
============================  ======================================
:mod:`catalog`                ``catalog.go``：种类、记录、目录
:mod:`parser`                 ``parser.go``：SKILL.md 解析、风险解析
:mod:`agent_manifest`         ``agent_manifest.go``：ATRIX_SKILL.toml
:mod:`descriptor`             ``descriptor.go``：文档型 skill 工具
:mod:`command_tool`           ``command_tool.go``：可调用 AgentSkill
:mod:`media`                  ``media.go``：媒体交接
:mod:`runner`                 ``runner.go``：``go_skill_run``
:mod:`process`                ``process_{unix,windows}.go``：子进程控制
:mod:`scanner`                ``scanner.go``：文件系统扫描
:mod:`manager`                ``manager.go``：装载/重载/清单写入
:mod:`agent_skill_tool`       ``agent_skill_tool.go``：``agent_skill``
:mod:`go_source`              ``go_source.go``：gofmt/校验/编译
:mod:`creator`                ``creator.go``：``create_atrix_skill``
:mod:`modify_atrix_skill`     ``modify_atrix_skill.go``：读/改原生 skill
:mod:`finalize_atrix_skill`   ``finalize_atrix_skill.go``：收口校验
:mod:`atriyph_warnings`       ``atriyph_warnings.go``：诊断拼接
============================  ======================================

用法::

    from atri_x.tool.skill import Manager, new_manager

    manager = new_manager(root, registry)
    await manager.ensure_loaded(token)
    record, ok = manager.catalog.get("my_skill")
"""

from __future__ import annotations

from .agent_manifest import (
    AGENT_SKILL_CONFIG_FILE,
    AgentSkillManifest,
    agent_skill_config_path,
    load_agent_skill_manifest,
    parse_agent_skill_manifest,
    project_media_schema,
    schema_required,
)
from .agent_skill_tool import AgentSkillTool, new_agent_skill_tool
from .atriyph_warnings import append_atriyph_warnings
from .catalog import Catalog, Kind, KindAgent, KindGo, Record, new_catalog, source_for_kind
from .command_tool import CommandTool, command_arguments, new_command_tool
from .creator import (
    CREATE_ATRIX_SKILL_NAME,
    CreateAtrixSkillTool,
    new_create_atrix_skill_tool,
    validate_skill_name,
)
from .descriptor import (
    AgentSkillManagerName,
    Descriptor,
    GoRunnerName,
    load_record_detail,
    new_descriptor,
)
from .finalize_atrix_skill import (
    FINALIZE_ATRIX_SKILL_NAME,
    FinalizeAtrixSkillTool,
    new_finalize_atrix_skill_tool,
)
from .go_source import (
    GO_BINARY_ENV,
    GoBuildResult,
    build_go_skill,
    format_go_source,
    run_go_build,
    validate_go_main_source,
)
from .manager import Manager, new_manager
from .media import (
    prepare_command_media,
    prepare_go_media,
    result_from_stdout,
    result_from_stdout_with_media,
    schema_contains_media,
)
from .modify_atrix_skill import (
    MODIFY_ATRIX_SKILL_NAME,
    READ_ATRIX_SKILL_NAME,
    ModifyAtrixSkillTool,
    ReadAtrixSkillTool,
    atrix_skill_target_path,
    new_modify_atrix_skill_tool,
    new_read_atrix_skill_tool,
)
from .parser import Definition, parse_risk, parse_skill_markdown
from .process import CommandOutput, IS_WINDOWS, run_command, truncate_output
from .runner import (
    GoRunner,
    GoRunnerArgs,
    new_go_runner,
    parse_go_runner_args,
    run_media_command,
    safe_relative_path,
)
from .scanner import (
    FilesystemScanner,
    atriyph_risk,
    default_root,
    find_go_binary,
    new_filesystem_scanner,
)

__all__ = [
    "AGENT_SKILL_CONFIG_FILE",
    "AgentSkillManagerName",
    "AgentSkillManifest",
    "AgentSkillTool",
    "CREATE_ATRIX_SKILL_NAME",
    "Catalog",
    "CommandOutput",
    "CommandTool",
    "CreateAtrixSkillTool",
    "Definition",
    "Descriptor",
    "FINALIZE_ATRIX_SKILL_NAME",
    "FilesystemScanner",
    "FinalizeAtrixSkillTool",
    "GO_BINARY_ENV",
    "GoBuildResult",
    "GoRunner",
    "GoRunnerArgs",
    "GoRunnerName",
    "IS_WINDOWS",
    "Kind",
    "KindAgent",
    "KindGo",
    "MODIFY_ATRIX_SKILL_NAME",
    "Manager",
    "ModifyAtrixSkillTool",
    "READ_ATRIX_SKILL_NAME",
    "ReadAtrixSkillTool",
    "Record",
    "agent_skill_config_path",
    "append_atriyph_warnings",
    "atriyph_risk",
    "atrix_skill_target_path",
    "build_go_skill",
    "command_arguments",
    "default_root",
    "find_go_binary",
    "format_go_source",
    "load_agent_skill_manifest",
    "load_record_detail",
    "new_agent_skill_tool",
    "new_catalog",
    "new_command_tool",
    "new_create_atrix_skill_tool",
    "new_descriptor",
    "new_filesystem_scanner",
    "new_finalize_atrix_skill_tool",
    "new_go_runner",
    "new_manager",
    "new_modify_atrix_skill_tool",
    "new_read_atrix_skill_tool",
    "parse_agent_skill_manifest",
    "parse_go_runner_args",
    "parse_risk",
    "parse_skill_markdown",
    "prepare_command_media",
    "prepare_go_media",
    "project_media_schema",
    "result_from_stdout",
    "result_from_stdout_with_media",
    "run_command",
    "run_go_build",
    "run_media_command",
    "safe_relative_path",
    "schema_contains_media",
    "schema_required",
    "source_for_kind",
    "truncate_output",
    "validate_go_main_source",
    "validate_skill_name",
]
