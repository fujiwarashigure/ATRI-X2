"""部署套件共享辅助库（统一门面）：``import _lib`` 拿到下面全部能力。

对应上游：``deploy/`` 下 7 个 shell 脚本里各自重复实现的那一堆 bash 函数
（``log`` / ``warn`` / ``die`` / ``detect_compose`` / ``compose`` /
``wait_for_container_ready`` / ``host_path`` / ``read_env_value`` /
``sha256sum`` / ``REDACT_SED`` / ``verify_redaction`` …）。

上游把同一段逻辑复制了 7 份（``backup.sh``、``restore-verify.sh``、
``rollback.sh``、``upgrade.sh``、``init-host.sh``、``build-push.sh``、
``atri-x-watchdog.sh``），改一处要同步七处。Python 版抽成一个包，
无论谁被 ``python deploy/xxx.py`` 直接执行还是被 unittest import，行为一致。

**原 ``_lib.py`` 是单文件 900+ 行**，超过 ``PORTING.md`` §2 的 600 行上限，
因此按职责拆成四个模块，本文件只做 re-export（门面）：

| 模块 | 职责 |
|---|---|
| :mod:`_console` | ``log`` / ``warn`` / ``die`` / ``console``（控制台与 ANSI） |
| :mod:`_envutil` | 时间、环境变量、文本/JSON、状态小文件 |
| :mod:`_proc` | 子进程、``docker compose`` 前端探测、``host_path`` |
| :mod:`_netutil` | HTTP 探针（curl 等价物）与 TCP 兜底 |
| :mod:`_libfs` | 文件遍历、sha256、清单校验、tar 安全解包、脱敏 |

使用方式（两种都支持）::

    # 被 unittest 导入
    from deploy import _lib
    # 被 `python deploy/backup.py` 直接执行（脚本自己把 deploy/ 放进 sys.path）
    import _lib

拆分是纯机械的：所有名字在拆分前后完全一致，``__all__`` 是并集。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接执行 `python deploy/backup.py` 时，deploy/ 已经在 sys.path[0]；
# 被 `from deploy import _lib` 导入时不一定，这里补上，保证下面四个
# 兄弟模块两种场景都能 import。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _console import ANSI, console, die, log, safe_write, warn  # noqa: E402
from _envutil import (  # noqa: E402
    decode_env_value,
    env_flag,
    env_int,
    env_value,
    list_env_sources,
    now_epoch,
    now_iso,
    now_local,
    now_stamp,
    now_utc,
    parse_env_file,
    parse_env_lines,
    pretty_json,
    read_env_file,
    read_int_file,
    read_text,
    tail_lines,
    wc_lines,
    write_env_file,
    write_int_file,
    write_text,
)
from _libfs import (  # noqa: E402
    REDACTED,
    REAL_REDACT,
    ArchivedPathError,
    SafeExtractError,
    collect_files,
    detect_credential_patterns,
    detect_known_secrets,
    extract_tar,
    file_sha256,
    iter_files,
    known_secrets_from_env,
    real_redact,
    redact_dir,
    safe_extract_tar,
    sensitive_env_name,
    verify_manifest,
)
from _netutil import (  # noqa: E402
    http_get,
    http_ok,
    http_post_json,
    http_status,
    post_json,
    probe_health,
    tcp_open,
)
from _proc import (  # noqa: E402
    compose_argv,
    compose_command,
    has_command,
    host_path,
    record_external,
    run_capture,
    run_inherit,
    run_lines,
)

#: ``deploy/`` 目录（本文件所在目录）。
DEPLOY_DIR = _HERE
#: 仓库根目录（``deploy/`` 的上一层）。
REPO_ROOT = _HERE.parent

__all__ = [
    "ANSI",
    "ArchivedPathError",
    "DEPLOY_DIR",
    "REAL_REDACT",
    "REDACTED",
    "REPO_ROOT",
    "SafeExtractError",
    "collect_files",
    "compose_argv",
    "compose_command",
    "console",
    "decode_env_value",
    "detect_credential_patterns",
    "detect_known_secrets",
    "die",
    "env_flag",
    "env_int",
    "env_value",
    "extract_tar",
    "file_sha256",
    "has_command",
    "host_path",
    "http_get",
    "http_ok",
    "http_post_json",
    "http_status",
    "iter_files",
    "known_secrets_from_env",
    "list_env_sources",
    "log",
    "now_epoch",
    "now_iso",
    "now_local",
    "now_stamp",
    "now_utc",
    "parse_env_file",
    "parse_env_lines",
    "post_json",
    "pretty_json",
    "probe_health",
    "read_env_file",
    "read_int_file",
    "read_text",
    "real_redact",
    "record_external",
    "redact_dir",
    "run_capture",
    "run_inherit",
    "run_lines",
    "safe_extract_tar",
    "safe_write",
    "sensitive_env_name",
    "tail_lines",
    "tcp_open",
    "verify_manifest",
    "warn",
    "wc_lines",
    "write_env_file",
    "write_int_file",
    "write_text",
]
