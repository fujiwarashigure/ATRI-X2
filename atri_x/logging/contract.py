"""日志来源/结果/运行来源最小契约。对应 Go：``internal/logging/contract.go``。

本模块是 fork 版"来源标识（``module``）+ 操作结果（``result``）+ 运行来源
（``run_source``）"三个字段取值的唯一出处：

1. 每条运行日志和审计日志都要写明它来自哪个模块（``module``），且只允许用登记过的名字
   （``/log --hook``、``/audit --hook`` 就是按 ``module=hook`` 过滤的）。
2. 拒绝、跳过、失败、成功、取消是 ``result`` 字段的稳定取值，不再靠自由文本措辞；
   读取方**不得**由 ``result`` 反推日志等级，反之亦然（正常取消是 ``canceled`` + INFO，
   策略拒绝是 ``rejected`` + WARN）。
3. 工具调用的运行来源写在 ``run_source`` 字段，取值在调用现场固定，不从会话元数据回推；
   缺字段的历史记录不补造。

消息正文、文本长度上限与 JSONL 落盘属于完整契约的范围，本轮不实施（与 Go 一致）。
"""

from __future__ import annotations

# --- 来源模块标识（写入 module 字段） --------------------------------------

ModuleApp = "app"
ModuleAgent = "agent"
ModuleSession = "session"
ModuleHook = "hook"
ModulePlatform = "platform"
ModuleDelivery = "delivery"
ModuleCron = "cron"
ModuleAtrinis = "atrinis"
ModuleModel = "model"
ModuleStorage = "storage"
ModuleMedia = "media"
ModuleMaintenance = "maintenance"
ModuleTool = "tool"

# 顺序固定（Go 的 LogModules 同序），便于文档与测试比对。
LOG_MODULES: tuple[str, ...] = (
    ModuleApp,
    ModuleAgent,
    ModuleSession,
    ModuleHook,
    ModulePlatform,
    ModuleDelivery,
    ModuleCron,
    ModuleAtrinis,
    ModuleModel,
    ModuleStorage,
    ModuleMedia,
    ModuleMaintenance,
    ModuleTool,
)

# --- 操作结果（写入 result 字段） ------------------------------------------

ResultSucceeded = "succeeded"
ResultFailed = "failed"
ResultCanceled = "canceled"
ResultRejected = "rejected"
ResultSkipped = "skipped"

LOG_RESULTS: tuple[str, ...] = (
    ResultSucceeded,
    ResultFailed,
    ResultCanceled,
    ResultRejected,
    ResultSkipped,
)

# --- 工具调用运行来源（写入 run_source 字段） ------------------------------

RunSourceForeground = "foreground"
RunSourceCron = "cron"
RunSourceAtrinis = "atrinis"

#: 承载运行来源的字段名（写入与 ``/audit --run_source`` 共用）。
FieldRunSource = "run_source"

RUN_SOURCES: tuple[str, ...] = (
    RunSourceForeground,
    RunSourceCron,
    RunSourceAtrinis,
)


def log_modules() -> list[str]:
    """返回全部已登记的来源标识（顺序固定）。对应 Go ``LogModules()``。"""
    return list(LOG_MODULES)


def valid_log_module(module: str) -> bool:
    """报告来源标识是否已登记。对应 Go ``ValidLogModule(module string) bool``。

    空字符串不是合法来源：来源必须写明自己是谁，不能依赖读取方猜测。
    """
    return isinstance(module, str) and module.strip() in LOG_MODULES


def valid_log_result(result: str) -> bool:
    """报告操作结果取值是否合法。对应 Go ``ValidLogResult(result string) bool``。"""
    return isinstance(result, str) and result.strip() in LOG_RESULTS


def valid_run_source(source: str) -> bool:
    """报告运行来源取值是否合法。对应 Go ``ValidRunSource(source string) bool``。"""
    return isinstance(source, str) and source.strip() in RUN_SOURCES


__all__ = [
    "FieldRunSource",
    "LOG_MODULES",
    "LOG_RESULTS",
    "ModuleAgent",
    "ModuleApp",
    "ModuleAtrinis",
    "ModuleCron",
    "ModuleDelivery",
    "ModuleHook",
    "ModuleMaintenance",
    "ModuleMedia",
    "ModuleModel",
    "ModulePlatform",
    "ModuleSession",
    "ModuleStorage",
    "ModuleTool",
    "RUN_SOURCES",
    "ResultCanceled",
    "ResultFailed",
    "ResultRejected",
    "ResultSkipped",
    "ResultSucceeded",
    "RunSourceAtrinis",
    "RunSourceCron",
    "RunSourceForeground",
    "log_modules",
    "valid_log_module",
    "valid_log_result",
    "valid_run_source",
]
