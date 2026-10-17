"""稳定性测试（长跑）：起容器、定时采样、按硬指标判定。

对应上游：无（上游没有容器长跑测试）。它是 :file:`deploy/smoke.py` 的补充：
冒烟测的是「装得起来、答案对不对」，稳定性测的是「跑得住」——
有没有内存持续增长、有没有健康检查抖动、有没有反复重启、有没有未捕获异常。

用法::

    python deploy/stability.py                       # 默认跑 300 秒，每 5 秒采样一次
    python deploy/stability.py --duration 1800 --interval 5 --keep
    python deploy/stability.py --image atri-x:0.9.0 --json out/stability.json
    python deploy/stability.py --allow-missing-health   # 早期镜像没有 /healthz 时的退化模式

判定标准（**边界值算通过**，都可用参数覆盖）
--------------------------------------------
=========================  ==============  ==========================================
指标                        默认阈值        参数
=========================  ==============  ==========================================
健康检查失败率              ``<= 1%``       ``--max-failure-rate 0.01``
内存增长比（后 1/3 ÷ 前 1/3） ``<= 1.5``     ``--max-rss-growth 1.5``
容器重启次数                ``== 0``        仅 ``--max-restarts 0``
未捕获异常日志              ``== 0``        ``docker logs`` 里不出现
                                            ``Traceback (most recent call last)``
容器仍在运行                ``True``        （结束时核对 ``State.Running``）
=========================  ==============  ==========================================

* **失败率**：一次采样里只要有任一「必须通过」的端点不是 2xx（含连接拒绝 / 超时），
  这一次采样就算失败；``失败次数 / 采样次数 <= 阈值`` 才算通过。恰好 1% 通过。
* **内存增长**：把成功采到的 RSS 序列按 ``len // 3`` 切成前段与后段，
  ``后段均值 / 前段均值 <= 1.5`` 才算通过（恰好 1.5 通过）。样本少于 3 个时无法判定，
  按**不通过**处理并在 ``summary`` 里说明。
* **``--allow-missing-health``**：早期镜像可能没有 ``/healthz``（例如没设
  ``ATRIX_OPS_TOKEN``），此时退化成「只看存活」：``/live`` 才算失败判据，
  ``/ready`` / ``/healthz`` 只记录不判定。默认（严格模式）三个端点都要求 2xx。

报告（键名固定，CI 直接读）
--------------------------
::

    {"ok": bool, "duration_seconds": float, "samples": int, "failures": int,
     "restart_count": int, "rss_first_third_mean": float|null,
     "rss_last_third_mean": float|null, "rss_growth_ratio": float|null,
     "log_tracebacks": int, "summary": str}

附加键（不改变上面这些的含义）：``steps``（前置步骤，键名同冒烟测试）、
``failure_detail``（每次失败的时间戳 / 端点 / 状态 / 错误）、
``sample_detail``（每次采样的时间戳 / 三个端点延迟 / CPU% / RSS）、
``thresholds``（本次生效的阈值）、``image`` / ``container`` / ``docker_version``。

同样地：Docker 不可用时**不崩**，第一步 FAIL、其余 skipped、退出码 1。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402
import _smokelib as _sl  # noqa: E402
from _docker import DockerCLI  # noqa: E402

DEFAULT_DURATION = 300.0
DEFAULT_INTERVAL = 5.0
DEFAULT_HTTP_TIMEOUT = 3.0
DEFAULT_STARTUP_TIMEOUT = 180.0
DEFAULT_MAX_FAILURE_RATE = 0.01
DEFAULT_MAX_RSS_GROWTH = 1.5
DEFAULT_MAX_RESTARTS = 0
#: 结束时抓取的日志行数（扫 Traceback 用）。
LOG_TAIL_LINES = 400
DEFAULT_FIXTURE_DIR = _DEPLOY / "fixtures" / "smoke-config"
DEFAULT_REPORT_DIR = _DEPLOY / "reports"
#: 写最小配置用的一次性容器名。与冒烟测试不同名，两者可以同时跑。
SEED_CONTAINER = "atri-x-stability-seed"
TRACEBACK_MARKER = "Traceback (most recent call last)"

#: 采样端点（与 deploy/README.md 第 3 节一致）。
ENDPOINTS = ("/live", "/ready", "/healthz")
#: 严格模式下必须 2xx 的端点；``--allow-missing-health`` 时只剩 /live。
STRICT_ENDPOINTS = ENDPOINTS
LIVENESS_ONLY_ENDPOINTS = ("/live",)

PIPELINE = (
    "docker_available",
    "image_ready",
    "seed_config",
    "container_start",
    "healthy",
    "sample",
    "restart_check",
    "log_check",
)


class StabilityTest:
    """一次长跑稳定性测试。``docker`` / ``sleep`` 可注入，便于无 Docker 单测。"""

    def __init__(
        self,
        *,
        image: str = "",
        tag: str = "",
        duration: float = DEFAULT_DURATION,
        interval: float = DEFAULT_INTERVAL,
        keep: bool = False,
        json_path: str = "",
        write_json: bool = True,
        fixture_dir: str = "",
        volume: str = "atri_x_stability_data",
        container: str = _sl.CONTAINER_NAME_STABILITY,
        docker: DockerCLI | None = None,
        dockerfile: str = "deploy/Dockerfile",
        context: str = "",
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        health_interval: float = 2.0,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        max_failure_rate: float = DEFAULT_MAX_FAILURE_RATE,
        max_rss_growth: float = DEFAULT_MAX_RSS_GROWTH,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        allow_missing_health: bool = False,
        ops_token: str = "stability-test-token",
        probe=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.docker = docker if docker is not None else DockerCLI()
        self.image = image
        self.tag = tag
        self.duration = duration
        self.interval = interval
        self.keep = keep
        self.json_path = json_path
        self.write_json = write_json
        self.fixture_dir = Path(fixture_dir) if fixture_dir else DEFAULT_FIXTURE_DIR
        self.volume = volume
        self.container = container
        self.dockerfile = dockerfile
        self.context = context or str(_REPO)
        self.startup_timeout = startup_timeout
        self.health_interval = health_interval
        self.http_timeout = http_timeout
        self.max_failure_rate = max_failure_rate
        self.max_rss_growth = max_rss_growth
        self.max_restarts = max_restarts
        self.allow_missing_health = allow_missing_health
        self.ops_token = ops_token
        #: 探针可注入：默认用 :func:`_lib.http_get`（返回 ``(状态码, 正文)``）。
        self.probe = probe if probe is not None else self._http_probe
        self.sleep = sleep
        self.clock = clock

        self.steps = _sl.StepLog()
        self.report: dict[str, object] = {}
        self.docker_version = ""
        self.built = False
        self.started = False
        self.base_url = ""
        # 收尾核对用的字段：必须先给默认值，否则前置失败时 _verdicts() 会 AttributeError。
        self.samples = 0
        self.failures = 0
        self.restart_count = 0
        self.running = False
        self.log_tracebacks = 0
        self.rss_values: list[float] = []
        self.sample_detail: list[dict[str, object]] = []
        self.failure_detail: list[dict[str, object]] = []

    # ------------------------------------------------------------------
    # 流程
    # ------------------------------------------------------------------
    def run(self) -> int:
        """跑完整流程并返回进程退出码（0 = 全绿）。"""
        try:
            self._pipeline()
        finally:
            self._cleanup()
        return self._finish()

    def _pipeline(self) -> None:
        """前置 → 等 healthy → 采样长跑 → 收尾核对。"""
        ok, version = _sl.check_docker(self.docker, self.steps)
        self.docker_version = version
        if not ok:
            self._skip_rest("Docker 不可用")
            return
        prep = _sl.prepare_image(
            self.docker,
            self.steps,
            image=self.image,
            tag=self.tag,
            version=_lib.read_text(_DEPLOY / "VERSION").strip() or "dev",
            dockerfile=self.dockerfile,
            context=self.context,
            tag_suffix="-stability",
        )
        self.tag = prep.tag or self.tag
        self.built = prep.built
        if not prep.ok:
            self._skip_rest("镜像不可用")
            return
        if not _sl.seed_volume(
            self.docker,
            self.steps,
            image=self._image_ref(),
            volume=self.volume,
            fixture_dir=self.fixture_dir,
            container_name=SEED_CONTAINER,
        ):
            self._skip_rest("最小配置未能写入数据卷")
            return
        spec = _sl.ContainerSpec(
            name=self.container,
            image=self._image_ref(),
            volume=self.volume,
            env={"ATRIX_OPS_TOKEN": self.ops_token},
            auto_remove=not self.keep,
        )
        if not _sl.start_container(self.docker, self.steps, spec):
            self.started = False
            self._skip_rest("容器未能启动")
            return
        self.started = True

        if not self._wait_healthy():
            self._skip_rest("容器未能 healthy，长跑没有意义")
            return
        self._sample_loop()
        self._check_restarts()
        self._check_logs()

    def _skip_rest(self, reason: str) -> None:
        """把尚未记录的步骤标成 skipped。"""
        done = {step.name for step in self.steps.steps}
        for name in PIPELINE:
            if name not in done:
                self.steps.skipped(name, reason)

    def _finish(self) -> int:
        """写报告、打印趋势与摘要、返回退出码。"""
        summary, verdicts = self._verdicts()
        self.report.update(
            {
                "ok": all(verdicts.values()),
                "summary": summary,
                "thresholds": {
                    "max_failure_rate": self.max_failure_rate,
                    "max_rss_growth": self.max_rss_growth,
                    "max_restarts": self.max_restarts,
                    "allow_missing_health": self.allow_missing_health,
                },
            }
        )
        written = ""
        if self.write_json and self.json_path:
            _sl.write_report(self.json_path, self.report)
            written = str(self.json_path)
        print("")
        self._print_trend()
        _lib.log(f"稳定性测试摘要（容器 {self.container}）")
        _sl.print_steps(self.steps.steps)
        if self._long_run_skipped():
            _lib.warn("长跑未执行（见上面的 skipped 步骤），四项判定不适用；退出码仍为失败")
        else:
            for name, passed in verdicts.items():
                line = f"  [{' OK ' if passed else 'FAIL'}] 判定：{name}"
                if passed:
                    _lib.safe_write(sys.stdout, line)
                else:
                    _lib.warn(line)
            _lib.safe_write(sys.stdout, "  " + summary)
        conclusion = "通过" if self.report["ok"] else "失败"
        _lib.log(f"结论：{conclusion}" + (f"；报告：{written}" if written else ""))
        return 0 if self.report["ok"] else 1

    def _long_run_skipped(self) -> bool:
        """长跑阶段是否被跳过（前置失败时判定没有意义）。"""
        return any(step.name == "sample" and step.skipped for step in self.steps.steps)

    # ------------------------------------------------------------------
    # 前置与健康
    # ------------------------------------------------------------------
    def _wait_healthy(self) -> bool:
        self.steps.start("healthy")
        outcome = _sl.wait_healthy(
            self.docker,
            self.container,
            timeout=self.startup_timeout,
            interval=self.health_interval,
            sleep=self.sleep,
        )
        step = _sl.apply_health_outcome(
            self.steps, "healthy", outcome, container=self.container, keep=self.keep
        )
        if not step.ok:
            _lib.warn("启动期 healthy 判定失败：" + step.detail.splitlines()[0])
            return False
        self.base_url = self._resolve_base_url()
        self._wait_ready()
        return True

    def _wait_ready(self) -> None:
        """等应用**真正 ready** 再开始采样（启动瞬态不该算进稳定性失败率）。

        为什么需要：``healthy`` 的三层探测与 Dockerfile 的 ``HEALTHCHECK`` 逐字一致，
        最后一层只是 TCP 连通；而冷启动的数据卷要首次初始化（建库、建目录、目录可写
        检查），``/ready`` 可能在容器 start 之后 8~14 秒仍是 503。那是**启动瞬态**，
        不是"稳定性失败"——但采样若从那一刻开始，1 次瞬态就能把 50 次采样的失败率顶到
        2%（阈值 ≤1%），把结论判红。真实事故：2026-10-17 20:44 那次 1/50 的失败全部
        来自第 1 次采样的 ``/ready`` 503（同一时刻 ``/live`` 是 200）。

        这里复用 ``--startup-timeout`` 作为预算轮询 ``/ready``；预算耗尽时只告警不阻断
        —— 让采样如实记录（报告里能看到逐次状态），避免把"真的起不来"也藏起来。
        """
        if not self.base_url or self.duration <= 0:
            # 端口解析失败：交给采样阶段报明确原因。
            # 采样窗口为 0（干跑 / 单测）时不存在"启动瞬态污染采样"的问题，直接跳过，
            # 免得对着假端口做真实 HTTP 轮询（那是分钟级的等待）。
            return
        deadline = self.clock() + max(self.startup_timeout, 0.0)
        # 真实运行时按 health_interval 轮询；单测里 interval 可能是 0、sleep 是 no-op，
        # 所以再用「预算 / 最小轮询间隔」封顶次数，避免零间隔 + 假时钟下空转。
        interval = max(float(self.health_interval), 0.05)
        attempts = max(1, int(max(self.startup_timeout, 0.0) / interval) + 1)
        attempts = min(attempts, 200)
        status = 0
        for _attempt in range(attempts):
            status, _elapsed = self._probe_endpoint("/ready")
            if 200 <= status < 300:
                return
            if self.clock() >= deadline:
                break
            self.sleep(self.health_interval)
        _lib.warn(
            "容器进程已 healthy，但 /ready 在启动预算内未就绪"
            f"（最后一次状态 {status}）；采样仍会开始，请把启动瞬态与稳定性问题分开看"
        )

    def _resolve_base_url(self) -> str:
        """把容器内 32171 的宿主机映射端口解析成 ``http://127.0.0.1:<port>``。

        与 compose 一样发布到宿主机回环（``-p 127.0.0.1::32171``，由 Docker 随机
        分配），因此本机同时跑着生产实例也不冲突。解析不到端口时返回空串，
        采样会全部失败并给出明确原因。
        """
        port = self.docker.published_port(self.container, _sl.HEALTH_PORT)
        return f"http://127.0.0.1:{port}" if port else ""

    # ------------------------------------------------------------------
    # 采样
    # ------------------------------------------------------------------
    def _sample_loop(self) -> None:
        """按 ``--interval`` 采样 ``--duration`` 秒，记录失败与时间戳。"""
        self.steps.start("sample")
        required = (
            LIVENESS_ONLY_ENDPOINTS if self.allow_missing_health else STRICT_ENDPOINTS
        )
        self.sample_detail = []
        self.failure_detail = []
        self.rss_values = []
        failures = 0
        started = self.clock()
        deadline = started + max(self.duration, 0.0)
        index = 0
        while True:
            index += 1
            stamp = _lib.now_iso()
            sample: dict[str, object] = {"index": index, "timestamp": stamp}
            for path in ENDPOINTS:
                if not self.base_url:
                    sample[f"{path.strip('/')}_status"] = 0
                    sample[f"{path.strip('/')}_ms"] = None
                    self._record_failure(
                        stamp,
                        path,
                        0,
                        "未解析到 32171 的宿主机端口（-p 127.0.0.1::32171）",
                        fatal=path in required,
                    )
                    continue
                status, elapsed_ms = self._probe_endpoint(path)
                sample[f"{path.strip('/')}_status"] = status
                sample[f"{path.strip('/')}_ms"] = elapsed_ms
                if not 200 <= status < 300:
                    self._record_failure(
                        stamp,
                        path,
                        status,
                        "超时/连接拒绝" if status == 0 else f"HTTP {status}",
                        fatal=path in required,
                    )
            cpu_text, rss = _sl.parse_stats_line(
                self.docker.stats(self.container, _sl.STATS_FORMAT)
            )
            sample["cpu_percent"] = cpu_text
            sample["rss_bytes"] = rss
            if rss is not None:
                self.rss_values.append(rss)
            self.sample_detail.append(sample)
            if self._sample_failed(sample, required):
                failures += 1
            if self.clock() >= deadline:
                break
            self.sleep(self.interval)
        self.samples = len(self.sample_detail)
        self.failures = failures
        detail = (
            f"{self.samples} 次采样，失败 {failures} 次"
            f"（{failures / self.samples:.2%}）"
            if self.samples
            else "未采到任何样本"
        )
        if not self.base_url:
            detail += "；未能解析健康接口的宿主机端口，所有采样必然失败"
        elif failures == self.samples and all(
            item.get("live_status") == 0 for item in self.sample_detail
        ):
            detail += (
                "；宿主机侧全部连不上，但容器内三层探测是通的 —— 通常意味着应用只监听"
                f"容器内回环地址（ATRIX_HEALTH_ADDR 应为 0.0.0.0:{_sl.HEALTH_PORT}）"
            )
        elapsed = self.clock() - started
        self.steps.finish(
            "sample", self.samples > 0 and bool(self.base_url), f"{detail}；耗时 {elapsed:.1f}s"
        )

    def _probe_endpoint(self, path: str) -> tuple[int, float | None]:
        """探一个端点，返回 ``(状态码, 毫秒)``；连不上时状态码 0。"""
        began = self.clock()
        status, _body = self.probe(f"{self.base_url}{path}")
        elapsed = (self.clock() - began) * 1000.0
        return int(status), round(elapsed, 1)

    def _http_probe(self, url: str) -> tuple[int, str]:
        """默认探针：标准库 GET，带 ops token（``/healthz`` 需要）。"""
        return _lib.http_get(url, timeout=self.http_timeout, token=self.ops_token)

    def _record_failure(
        self, stamp: str, path: str, status: int, error: str, *, fatal: bool
    ) -> None:
        self.failure_detail.append(
            {
                "timestamp": stamp,
                "endpoint": path,
                "status": status,
                "error": error,
                "counted": bool(fatal),
            }
        )

    def _sample_failed(self, sample: dict[str, object], required) -> bool:
        """这一次采样是否算失败（只按 ``required`` 里的端点判定）。"""
        for path in required:
            status = sample.get(f"{path.strip('/')}_status")
            if not isinstance(status, int) or not 200 <= status < 300:
                return True
        return False

    # ------------------------------------------------------------------
    # 收尾核对
    # ------------------------------------------------------------------
    def _check_restarts(self) -> None:
        """``restart_check``：``RestartCount`` 与 ``State.Running``。"""
        self.steps.start("restart_check")
        state = _sl.container_state(self.docker, self.container)
        if state is None:
            self.restart_count = -1
            self.running = False
            self.steps.finish(
                "restart_check",
                False,
                "容器已不存在（长跑期间退出；--rm 会把它删掉），用 --keep 可保留现场",
            )
            return
        restarts = int(state.get("RestartCount") or 0)
        self.restart_count = restarts
        self.running = bool(state.get("Running", False))
        ok = self.running and restarts <= self.max_restarts
        detail = (
            f"State.Running={self.running} RestartCount={restarts}"
            f"（上限 {self.max_restarts}）Status={state.get('Status')!r}"
        )
        if not ok:
            detail += "；长跑期间容器重启或退出，见 healthy / sample 步骤与 docker logs"
        self.steps.finish("restart_check", ok, detail)

    def _check_logs(self) -> None:
        """``log_check``：``docker logs`` 里不能出现未捕获异常。"""
        self.steps.start("log_check")
        logs = self.docker.logs(self.container, tail=LOG_TAIL_LINES)
        self.log_tracebacks = logs.count(TRACEBACK_MARKER)
        ok = self.log_tracebacks == 0
        detail = f"日志尾部 {LOG_TAIL_LINES} 行里 Traceback 出现 {self.log_tracebacks} 次"
        if not ok:
            first = logs.find(TRACEBACK_MARKER)
            detail += "\n---- docker logs 摘要 ----\n" + logs[first : first + 1200].rstrip("\n")
        self.steps.finish("log_check", ok, detail)

    # ------------------------------------------------------------------
    # 判定与展示
    # ------------------------------------------------------------------
    def _verdicts(self) -> tuple[str, dict[str, bool]]:
        """按四条硬指标算判定，返回 ``(summary, {判定名: 是否通过})``。"""
        samples = int(self.samples or 0)
        failures = int(self.failures or 0)
        rate = (failures / samples) if samples else None
        first, last = _sl.thirds_means(self.rss_values)
        ratio = (last / first) if (first and last is not None and first > 0) else None

        failure_ok = _sl.failures_at_or_below(failures, samples, self.max_failure_rate)
        growth_ok = ratio is not None and _sl.growth_ratio_ok(ratio, self.max_rss_growth)
        restart_ok = int(self.restart_count) <= self.max_restarts and self.running
        logs_ok = int(self.log_tracebacks) == 0

        self.report.update(
            {
                "duration_seconds": round(float(self.duration), 3),
                "samples": samples,
                "failures": failures,
                "restart_count": int(self.restart_count),
                "rss_first_third_mean": None if first is None else round(first, 1),
                "rss_last_third_mean": None if last is None else round(last, 1),
                "rss_growth_ratio": None if ratio is None else round(ratio, 4),
                "log_tracebacks": int(self.log_tracebacks),
                "failure_detail": self.failure_detail,
                "sample_detail": self.sample_detail,
                "image": self._image_ref(),
                "container": self.container,
                "docker_version": self.docker_version,
            }
        )
        parts = [
            f"健康检查失败率 {failures}/{samples}"
            + ("=n/a" if rate is None else f"={rate:.2%}")
            + f"（阈值 ≤{self.max_failure_rate:.2%}）",
            f"内存增长比 "
            + ("n/a（样本不足）" if ratio is None else f"{ratio:.3f}")
            + f"（阈值 ≤{self.max_rss_growth:g}；前 1/3 均值 "
            + _sl.format_bytes(first)
            + "，后 1/3 均值 "
            + _sl.format_bytes(last)
            + "）",
            f"重启次数 {self.restart_count}（上限 {self.max_restarts}）",
            f"Traceback {self.log_tracebacks} 次（要求 0）",
        ]
        verdicts = {
            "健康检查失败率": failure_ok,
            "内存增长比": growth_ok,
            "重启次数": restart_ok,
            "未捕获异常日志": logs_ok,
        }
        return "; ".join(parts), verdicts

    def _print_trend(self) -> None:
        """打印 ASCII 迷你趋势（每次采样的 RSS 与三个端点延迟）。"""
        samples = self.sample_detail
        if not samples:
            _lib.warn("没有采样数据，跳过趋势图")
            return
        rss_values = [
            item["rss_bytes"] for item in samples if isinstance(item.get("rss_bytes"), float)
        ]
        rss_max = max(rss_values, default=0.0)
        latencies = [
            float(item[key])
            for item in samples
            for key in ("live_ms", "ready_ms", "healthz_ms")
            if isinstance(item.get(key), float)
        ]
        lat_max = max(latencies, default=0.0)
        _lib.safe_write(
            sys.stdout, "  #  时刻                live/ready/healthz(ms)   cpu%     RSS        RSS 趋势"
        )
        for item in samples:
            cells = "/".join(
                "n/a" if item.get(f"{name}_ms") is None else f"{float(item[f'{name}_ms']):.0f}"
                for name in ("live", "ready", "healthz")
            )
            cpu = item.get("cpu_percent")
            rss = item.get("rss_bytes") if isinstance(item.get("rss_bytes"), float) else None
            cpu_text = "n/a" if cpu is None else f"{float(cpu):.2f}%"
            _lib.safe_write(
                sys.stdout,
                f"  {item.get('index'):>3} {str(item.get('timestamp', ''))[-14:]:<18} "
                f"{cells:<24} {cpu_text:>7} {_sl.format_bytes(rss):>9}  "
                f"{_sl.ascii_bar(rss, rss_max)}",
            )
        _lib.safe_write(
            sys.stdout,
            f"  延迟峰值 {lat_max:.0f}ms；RSS 峰值 {_sl.format_bytes(rss_max or None)}"
            "（条形按 RSS 峰值缩放）",
        )

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        if self.keep:
            self.steps.skipped(
                "cleanup",
                f"--keep：保留容器 {self.container}、卷 {self.volume}"
                + (f"、镜像 {self.tag}" if self.built else ""),
            )
            return
        if not self.docker_version:
            self.steps.skipped("cleanup", "Docker 不可用，没有产生任何需要清理的对象")
            return
        self.steps.start("cleanup")
        removed: list[str] = []
        if self.started:
            self.docker.remove_container(self.container)
            removed.append(f"容器 {self.container}")
        if self.built and self.tag:
            self.docker.remove_image(self.tag)
            removed.append(f"镜像 {self.tag}")
        self.docker.remove_volume(self.volume)
        removed.append(f"卷 {self.volume}")
        self.steps.finish("cleanup", True, "已清理：" + "、".join(removed))

    # ------------------------------------------------------------------
    def _image_ref(self) -> str:
        """本次实际使用的镜像引用。"""
        return self.image or self.tag or "atri-x:stability"


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="stability.py",
        description="ATRI-X 容器稳定性测试（长跑采样 → 硬指标判定 → 报告）",
    )
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION, help="长跑秒数，默认 300")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="采样间隔秒数，默认 5")
    parser.add_argument("--image", default="", help="跳过构建，直接测这个已有镜像 tag")
    parser.add_argument("--tag", default="", help="构建出的镜像 tag（默认 atri-x:<VERSION>-stability）")
    parser.add_argument("--keep", action="store_true", help="保留容器 / 镜像 / 卷（同时关掉 --rm）")
    parser.add_argument("--json", dest="json_path", default="", help="JSON 报告路径")
    parser.add_argument("--no-json", action="store_true", help="不写 JSON 报告，只打印摘要")
    parser.add_argument(
        "--allow-missing-health",
        action="store_true",
        help="早期镜像没有 /healthz 时退化：只把 /live 当失败判据",
    )
    parser.add_argument("--max-failure-rate", type=float, default=DEFAULT_MAX_FAILURE_RATE, help="失败率上限，默认 0.01")
    parser.add_argument("--max-rss-growth", type=float, default=DEFAULT_MAX_RSS_GROWTH, help="内存增长比上限，默认 1.5")
    parser.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS, help="允许的容器重启次数，默认 0")
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT, help="等 healthy 的秒数")
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT, help="单次 HTTP 采样超时秒数")
    parser.add_argument("--volume", default="atri_x_stability_data", help="数据卷名")
    parser.add_argument("--container", default=_sl.CONTAINER_NAME_STABILITY, help="容器名")
    parser.add_argument("--fixture-dir", default="", help="最小配置目录，默认 deploy/fixtures/smoke-config")
    parser.add_argument("--docker", dest="docker_bin", default="docker", help="docker 可执行文件名")
    return parser


def default_report_path() -> str:
    """默认报告路径：``deploy/reports/stability-<时间戳>.json``。"""
    return str(DEFAULT_REPORT_DIR / f"stability-{_lib.now_stamp()}.json")


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码（0 通过 / 1 失败 / 2 参数错误）。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    json_path = args.json_path or ("" if args.no_json else default_report_path())
    test = StabilityTest(
        image=args.image,
        tag=args.tag,
        duration=args.duration,
        interval=args.interval,
        keep=args.keep,
        json_path=json_path,
        write_json=not args.no_json,
        fixture_dir=args.fixture_dir,
        volume=args.volume,
        container=args.container,
        startup_timeout=args.startup_timeout,
        http_timeout=args.http_timeout,
        max_failure_rate=args.max_failure_rate,
        max_rss_growth=args.max_rss_growth,
        max_restarts=args.max_restarts,
        allow_missing_health=args.allow_missing_health,
        docker=DockerCLI(args.docker_bin),
    )
    return test.run()


if __name__ == "__main__":
    raise SystemExit(main())
