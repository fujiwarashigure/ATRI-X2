"""进程 Hook 继承的环境。

对应 Go：``internal/hook/process.go``。
"""

from __future__ import annotations

from atri_x.processenv.environment import Environment, new as new_environment


#: 进程 Hook 继承的环境。对应 Go 的 ``type ProcessEnvironment = processenv.Environment``。
ProcessEnvironment = Environment


def new_process_environment(base: list[str], values: dict[str, str]) -> Environment:
    """把配置值合并进 ATRI-X 进程环境。

    对应 Go ``NewProcessEnvironment``。语义：进程环境的值优先，但配置里的
    ``PATH`` 条目是**追加**到后面的。

    Go 里 ``processenv.New`` 是**包级函数**（``internal/processenv/environment.go:23``），
    不是 ``Environment`` 的方法；写成 ``Environment.new(base)`` 会在真实启动时抛
    ``AttributeError: type object 'Environment' has no attribute 'new'``
    —— 单元测试用假环境实现时看不见。
    """
    return new_environment(base).fill(values)


__all__ = ["ProcessEnvironment", "new_process_environment"]
