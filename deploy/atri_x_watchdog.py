"""ATRI-X 外部 watchdog：只依据独立 /live 接口判断进程是否还能响应，
连续失败达到阈值后收集诊断信息，并按冷却时间和次数上限受控重启容器。

对应上游：``deploy/atri-x-watchdog.sh``（362 行 bash）。

推荐由 systemd timer 每分钟执行一次（``atri-x-watchdog.timer``）；
不要和宝塔 Docker 管理器同时托管同一套 Compose。

手动暂停：``touch <state-dir>/paused``

与上游的行为等价点（逐条对齐，改动见文件末尾「有意偏差」）：
- 三层判定：/live 失败计数 → 达阈值 → 冷却检查 → 窗口内次数上限；
- /live 正常但 /ready 持续失败时**只告警不重启**（避免重启风暴）；
- 诊断包含 summary + /live + /ready + /healthz + /tasks + /metrics + docker 信息；
- 诊断包先脱敏、再复检；复检不过就写 ``REDACTION-FAILED`` 标记、chmod 700、发 webhook，
  并以非零退出，绝不把“可能带凭据的包”当成成功产物；
- ``--redact-dir <dir>`` 供运维对已有诊断目录单独脱敏 + 复检；
- ``WATCHDOG_DRY_RUN=1`` 只诊断不重启。

有意偏差（Python 版）：
1. **脱敏实现**：上游用 6 条 ``sed -E`` 正则就地改写。Python 版复用
   ``atri_x.redact.secrets``（``PORTING.md`` §3 的唯一出处），保证与“应用自己
   写日志时的脱敏”规则一致；只额外补一条“引号内取值”修复，因为项目规则的
   那条分支会吃掉收尾引号，导致重复脱敏不幂等（详见 ``deploy/_libfs.py``）。
2. **upstream 用 ``find | xargs rm -rf`` 清理旧诊断目录**；Python 版用标准库
   遍历 + ``shutil.rmtree``，同样只保留最新 30 份，但不依赖 awk/sort/xargs。
3. **单实例锁**：systemd timer 每分钟触发而一次运行可能超过一分钟（诊断 + 重启
   等待）。上游没有互斥，两个实例会互相覆盖状态文件并重复重启。Python 版用
   仓库外的状态目录里一个 O_EXCL 锁文件串行化，超时才抢占。
4. **HTTP 探针**用 ``urllib``（标准库）而不是 ``curl``：slim 镜像/精简运维机
   上未必有 curl。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

KEEP_DIAGNOSTICS = 30
LOG_TAIL_LINES = 500
DEFAULT_COMPOSE_DIR = _DEPLOY


class Watchdog:
    """一次 watchdog 运行的全部状态（配置 + 状态目录 + 探测结果）。"""

    def __init__(self, compose_dir: Path | None = None) -> None:
        self.compose_dir = Path(_lib.env_value("WATCHDOG_COMPOSE_DIR", str(compose_dir or DEFAULT_COMPOSE_DIR)))
        self.state_dir = Path(
            _lib.env_value("WATCHDOG_STATE_DIR", str(self.compose_dir / "watchdog-state"))
        )
        self.diag_dir = Path(
            _lib.env_value("WATCHDOG_DIAG_DIR", str(self.compose_dir / "diagnostics"))
        )
        self.health_url = _lib.env_value("WATCHDOG_HEALTH_URL", "http://127.0.0.1:32171/live")
        self.ready_url = _lib.env_value("WATCHDOG_READY_URL", "http://127.0.0.1:32171/ready")
        self.healthz_url = _lib.env_value("WATCHDOG_HEALTHZ_URL", "http://127.0.0.1:32171/healthz")
        self.service = _lib.env_value("WATCHDOG_SERVICE", "atri-x")
        self.failure_threshold = _lib.env_int("WATCHDOG_FAILURE_THRESHOLD", 3)
        self.cooldown_seconds = _lib.env_int("WATCHDOG_COOLDOWN_SECONDS", 300)
        self.max_restarts = _lib.env_int("WATCHDOG_MAX_RESTARTS", 3)
        self.window_seconds = _lib.env_int("WATCHDOG_WINDOW_SECONDS", 21600)
        self.dry_run = _lib.env_flag("WATCHDOG_DRY_RUN", False)
        self.webhook_url = _lib.env_value("WATCHDOG_WEBHOOK_URL")
        self.ops_token = _lib.env_value("WATCHDOG_OPS_TOKEN")
        self.restart_reason_file = Path(
            _lib.env_value(
                "WATCHDOG_RESTART_REASON_FILE",
                str(self.compose_dir / "data" / "run" / "atri-x" / "last_restart_reason"),
            )
        )
        self.ready_alert_threshold = _lib.env_int("WATCHDOG_READY_ALERT_THRESHOLD", 3)
        self.ready_alert_cooldown = _lib.env_int("WATCHDOG_READY_ALERT_COOLDOWN_SECONDS", 3600)
        self.compose_file = Path(
            _lib.env_value("ATRIX_COMPOSE_FILE", str(self.compose_dir / "docker-compose.yml"))
        )
        self.timeout = float(_lib.env_int("WATCHDOG_HTTP_TIMEOUT", 5))
        self._compose: list[str] | None = None

    # ---------- 状态文件 ----------
    def state_path(self, name: str) -> Path:
        return self.state_dir / name

    def read_int(self, name: str, default: int = 0) -> int:
        return _lib.read_int_file(self.state_path(name), default)

    def write_int(self, name: str, value: int) -> None:
        _lib.write_int_file(self.state_path(name), value)

    def append_line(self, name: str, line: str) -> None:
        """追加一行到状态文件（日志类状态用，允许失败但不静默改语义）。"""
        path = self.state_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            _lib.warn(f"写入状态文件失败：{path}：{exc}")

    # ---------- 通知 ----------
    def send_webhook(self, event: str, text: str) -> None:
        """发一条 webhook；未配置时静默跳过，发送失败只 warn 不中断。"""
        if not self.webhook_url:
            return
        payload = {
            "source": "atri-x-watchdog",
            "event": event,
            "service": self.service,
            "time": _lib.now_iso(),
            "message": text,
        }
        status, body = _lib.post_json(self.webhook_url, payload, timeout=self.timeout)
        if not (200 <= status < 300):
            _lib.warn(f"webhook delivery failed for event {event}（status={status} {body[:120]}）")

    # ---------- 探测 ----------
    def live_ok(self) -> bool:
        return _lib.http_ok(self.health_url, timeout=self.timeout)

    def ready_ok(self) -> bool:
        return _lib.http_ok(self.ready_url, timeout=self.timeout)

    def alert_if_not_ready(self) -> None:
        """/live 正常但 /ready 持续失败：只告警、不重启。

        这种“进程活着但初始化或调度有问题”的情况直接重启容易演变成重启风暴，
        交给人判断更稳。没有 webhook 时这一层不产生任何副作用（与上游一致）。
        """
        if not self.webhook_url:
            return
        if self.ready_ok():
            if self.read_int("ready_failures", 0) > 0:
                _lib.log("readiness recovered")
            self.write_int("ready_failures", 0)
            return
        fails = self.read_int("ready_failures", 0) + 1
        self.write_int("ready_failures", fails)
        if fails < self.ready_alert_threshold:
            return
        now = _lib.now_epoch()
        last = self.read_int("last_ready_alert_epoch", 0)
        if last > 0 and (now - last) < self.ready_alert_cooldown:
            return
        self.write_int("last_ready_alert_epoch", now)
        self.append_line(
            "ready_alerts.log",
            f"{_lib.now_iso()} not_ready: {fails} consecutive failures of {self.ready_url}",
        )
        self.send_webhook(
            "not_ready",
            f"{self.service} is live but not ready ({fails} consecutive failures of {self.ready_url})",
        )
        _lib.warn(f"readiness alert sent after {fails} failed checks of {self.ready_url}")

    # ---------- 诊断 ----------
    def capture_diagnostics(self) -> tuple[int, Path]:
        """收集诊断包并脱敏，返回 ``(退出码, 目录)``；退出码 0 = 脱敏复检通过。"""
        stamp = _lib.now_stamp()
        directory = self.diag_dir / stamp
        directory.mkdir(parents=True, exist_ok=True)
        _lib.log(f"capture diagnostics -> {directory}")

        reason = _lib.read_text(self.restart_reason_file).strip()
        if not reason:
            reasons = _lib.read_text(self.state_path("restart_reasons.log")).strip().splitlines()
            reason = reasons[-1] if reasons else ""
        summary = "\n".join(
            [
                f"time={_lib.now_iso()}",
                f"service={self.service}",
                f"health_url={self.health_url}",
                f"ready_url={self.ready_url}",
                f"healthz_url={self.healthz_url}",
                f"consecutive_failures={self.read_int('consecutive_failures', 0)}",
                f"last_restart_at={self.read_int('last_restart_epoch', 0)}",
                f"last_restart_reason={reason}",
                "",
            ]
        )
        _lib.write_text(directory / "summary.txt", summary)

        token = self.ops_token
        for name, url in (
            ("live.json", self.health_url),
            ("ready.json", self.ready_url),
            ("healthz.json", self.healthz_url),
        ):
            _, body = _lib.http_get(url, timeout=self.timeout, token=token)
            _lib.write_text(directory / name, body)
        base = self.healthz_url[: -len("/healthz")] if self.healthz_url.endswith("/healthz") else self.healthz_url
        for name, url in (("tasks.json", f"{base}/tasks"), ("metrics.json", f"{base}/metrics")):
            _, body = _lib.http_get(url, timeout=self.timeout, token=token)
            _lib.write_text(directory / name, body)

        self._capture_docker(directory)

        redaction_failed = False
        ok, failures = _lib.redact_dir(
            directory, extra_secrets=_lib.known_secrets_from_env()
        )
        for failure in failures:
            _lib.warn(f"诊断包脱敏出错：{failure}")
        if not ok:
            redaction_failed = True

        # 复检三层：独立模式检测 + 已知凭据值检测（幂等性由 redact_dir 的收敛保证）。
        pattern_hits = _lib.detect_credential_patterns(directory)
        known_hits = _lib.detect_known_secrets(directory, _lib.known_secrets_from_env())
        for hit in (*pattern_hits, *known_hits):
            _lib.warn(hit)
        if pattern_hits or known_hits:
            redaction_failed = True

        if redaction_failed:
            _lib.warn(f"诊断包可能仍包含凭据，请勿外发：{directory}")
            marker = directory / "REDACTION-FAILED"
            try:
                _lib.write_text(marker, f"redaction_failed_at={_lib.now_iso()}\n")
                _lib.write_text(
                    directory / "summary.txt",
                    _lib.read_text(directory / "summary.txt")
                    + f"redaction_failed_at={_lib.now_iso()}\n",
                )
                os.chmod(directory, 0o700)
            except OSError as exc:  # pragma: no cover - 权限受限时尽力而为
                _lib.warn(f"无法写入脱敏失败标记：{exc}")
            self.send_webhook("diagnostics_unredacted", f"diagnostics redaction failed: {directory}")

        _lib.write_text(directory / "disk.txt", self._df_output())
        if (self.compose_dir / "data").is_dir():
            _lib.write_text(directory / "data-size.txt", self._dir_size(self.compose_dir / "data"))
        self.prune_diagnostics()
        return (1 if redaction_failed else 0), directory

    def _capture_docker(self, directory: Path) -> None:
        """抓 docker 状态。**只抓 State/Ports**，不抓 Config.Env。

        ``docker inspect`` 的完整输出会把容器环境变量里的 API Key / token
        写进诊断包；上游因此只取 ``.State`` 与 ``.NetworkSettings.Ports``。
        """
        if not _lib.has_command("docker"):
            return
        commands = (
            (["docker", "ps", "-a", "--filter", f"name=^/{self.service}$"], "docker-ps.txt"),
            (["docker", "inspect", "--format", "{{json .State}}", self.service], "docker-state.json"),
            (
                ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", self.service],
                "docker-ports.json",
            ),
            (["docker", "logs", "--tail", str(LOG_TAIL_LINES), self.service], "docker-logs.txt"),
            (["docker", "stats", "--no-stream", self.service], "docker-stats.txt"),
        )
        for argv, name in commands:
            result = _lib.run_capture(argv, timeout=30.0)
            _lib.write_text(directory / name, (result.stdout or "") + (result.stderr or ""))

    def _df_output(self) -> str:
        """``df -h`` 的等价物（POSIX 上没有就写空文件而不是失败）。"""
        if os.name == "nt":
            return f"windows: no df -h; compose_dir={self.compose_dir}\n"
        result = _lib.run_capture(["df", "-h"], timeout=15.0)
        return (result.stdout or "") + (result.stderr or "")

    def _dir_size(self, directory: Path) -> str:
        """``du -sh`` 的等价物：累加文件大小（跨平台，不需要 du）。"""
        total = 0
        for path in _lib.iter_files(directory):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return f"{total / 1048576:.1f}M\t{directory}\n"

    def prune_diagnostics(self) -> None:
        """只保留最新 :data:`KEEP_DIAGNOSTICS` 份诊断目录。"""
        if not self.diag_dir.is_dir():
            return
        entries: list[tuple[float, Path]] = []
        for child in self.diag_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                entries.append((child.stat().st_mtime, child))
            except OSError:
                continue
        entries.sort(key=lambda item: item[0], reverse=True)
        for _, stale in entries[KEEP_DIAGNOSTICS:]:
            shutil.rmtree(stale, ignore_errors=True)

    # ---------- 重启 ----------
    def compose(self) -> list[str] | None:
        """缓存 Compose 前端探测结果（探测本身会起子进程）。"""
        if self._compose is None:
            self._compose = _lib.compose_command() or []
        return self._compose or None

    def restarts_in_window(self, now: int) -> int:
        """裁剪并统计窗口内的重启次数（对应上游 restart_history + awk + wc -l）。"""
        history = self.state_path("restart_history")
        cutoff = now - self.window_seconds
        kept: list[str] = []
        for line in _lib.read_text(history).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                stamp = int(line.split()[0])
            except (ValueError, IndexError):
                continue
            if stamp >= cutoff:
                kept.append(str(stamp))
        if kept:
            _lib.write_text(history, "\n".join(kept) + "\n")
        elif history.exists():
            _lib.write_text(history, "")
        return len(kept)

    def pause(self, reason_line: str) -> None:
        """写入 paused 标记与重启原因，暂停自动重启。"""
        try:
            _lib.write_text(self.state_path("paused"), f"{_lib.now_iso()}\n")
        except OSError as exc:
            _lib.warn(f"无法写入 paused 标记：{exc}")
        self.append_line("restart_reasons.log", reason_line)
        try:
            self.restart_reason_file.parent.mkdir(parents=True, exist_ok=True)
            _lib.write_text(self.restart_reason_file, reason_line + "\n")
        except OSError as exc:
            _lib.warn(f"无法写入重启原因文件 {self.restart_reason_file}：{exc}")


def redact_dir_command(target: str) -> int:
    """``--redact-dir <目录>``：对已有诊断目录脱敏 + 复检（供运维手动执行）。"""
    directory = Path(target)
    if not target or not directory.is_dir():
        sys.stderr.write(f"用法：{Path(sys.argv[0]).name} --redact-dir <目录>\n")
        return 2
    rc = 0
    ok, failures = _lib.redact_dir(directory, extra_secrets=_lib.known_secrets_from_env())
    for failure in failures:
        _lib.warn(failure)
    if not ok:
        rc = 1
    hits = _lib.detect_credential_patterns(directory) + _lib.detect_known_secrets(
        directory, _lib.known_secrets_from_env()
    )
    for hit in hits:
        _lib.warn(hit)
    if hits:
        rc = 1
    if rc == 0:
        _lib.log(f"诊断目录脱敏通过：{directory}")
    else:
        _lib.warn(f"诊断目录脱敏未通过：{directory}")
        try:
            _lib.write_text(directory / "REDACTION-FAILED", f"redaction_failed_at={_lib.now_iso()}\n")
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - 权限受限时尽力而为
            pass
    return rc


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="atri_x_watchdog.py",
        description="ATRI-X 外部 watchdog：/live 失败达阈值后受控重启容器",
    )
    parser.add_argument("--redact-dir", default="", metavar="目录", help="只对已有诊断目录做脱敏 + 复检")
    parser.add_argument("--compose-dir", default="", help="Compose 项目目录（默认 deploy/）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：一次 watchdog 运行。返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    if args.redact_dir:
        return redact_dir_command(args.redact_dir)

    dog = Watchdog(Path(args.compose_dir) if args.compose_dir else None)
    dog.state_dir.mkdir(parents=True, exist_ok=True)
    dog.diag_dir.mkdir(parents=True, exist_ok=True)

    if dog.state_path("paused").exists():
        _lib.log(f"watchdog paused by operator; remove {dog.state_path('paused')} to resume")
        return 0

    fails = dog.read_int("consecutive_failures", 0)
    if dog.live_ok():
        if fails > 0:
            _lib.log(f"health recovered after {fails} consecutive failure(s)")
        dog.write_int("consecutive_failures", 0)
        dog.alert_if_not_ready()
        return 0

    fails += 1
    dog.write_int("consecutive_failures", fails)
    _lib.warn(f"live check failed ({fails}/{dog.failure_threshold})")
    if fails < dog.failure_threshold:
        return 0

    now = _lib.now_epoch()
    last_restart = dog.read_int("last_restart_epoch", 0)
    if last_restart > 0 and (now - last_restart) < dog.cooldown_seconds:
        _lib.warn(
            f"restart cooldown active ({now - last_restart}s < {dog.cooldown_seconds}s); not restarting"
        )
        return 0

    if dog.restarts_in_window(now) >= dog.max_restarts:
        paused_line = (
            f"{_lib.now_iso()} paused: max restarts ({dog.max_restarts}) reached in window"
        )
        _lib.warn(f"max restarts ({dog.max_restarts}) reached in window; pausing auto-restart")
        dog.pause(paused_line)
        rc, _ = dog.capture_diagnostics()
        if rc != 0:
            _lib.warn("诊断收集/脱敏未通过，已写入 REDACTION-FAILED 标记")
        dog.send_webhook("paused", "max restarts reached; manual intervention required")
        return 1

    diag_rc, diag_dir = dog.capture_diagnostics()

    if dog.dry_run:
        _lib.log(f"DRY_RUN=1: would restart {dog.service}; diagnostics={diag_dir}")
        if diag_rc != 0:
            _lib.warn(f"DRY_RUN=1 但诊断脱敏未通过；诊断目录已标记，请勿外发：{diag_dir}")
        return diag_rc

    compose = dog.compose()
    if not compose or not dog.compose_file.is_file():
        _lib.warn(f"docker compose not available; cannot restart {dog.service}")
        dog.send_webhook("restart_failed", "docker compose not available")
        return 1

    _lib.log(f"restarting {dog.service} after {fails} failed live checks")
    reason_line = f"{_lib.now_iso()} restart: {fails} consecutive failures of {dog.health_url}"
    dog.append_line("restart_reasons.log", reason_line)
    try:
        dog.restart_reason_file.parent.mkdir(parents=True, exist_ok=True)
        _lib.write_text(dog.restart_reason_file, reason_line + "\n")
    except OSError:
        pass

    argv_up = _lib.compose_argv(
        compose, dog.compose_file, ["up", "-d", "--force-recreate", dog.service]
    )
    result = _lib.run_capture(argv_up, cwd=dog.compose_dir, timeout=180.0)
    if result.returncode != 0:
        _lib.warn(f"restart command failed for {dog.service}: {(result.stderr or '').strip()}")
        dog.send_webhook("restart_failed", "docker compose up failed")
        return 1

    dog.write_int("last_restart_epoch", now)
    dog.append_line("restart_history", str(now))
    dog.write_int("consecutive_failures", 0)
    dog.send_webhook("restarted", f"restarted {dog.service}; diagnostics={diag_dir}")
    _lib.log("restart command completed")
    if diag_rc != 0:
        _lib.warn(f"容器已重启，但诊断包脱敏未通过；诊断目录已标记，请勿外发：{diag_dir}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
