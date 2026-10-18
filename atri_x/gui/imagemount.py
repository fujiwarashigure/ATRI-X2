"""本地生图服务的「挂载」：把端口映射 / SSL 链接翻译成 OpenAI 兼容 Base URL。

背景：ATRI-X 的生图客户端只认 OpenAI 兼容端点（``POST {base_url}/images/generations``，
见 :mod:`atri_x.imagegen.client`）。自己部署生图时，服务通常不在本机：

- **端口映射**：本机（或局域网）把远端 ComfyUI / 生图服务的端口转出来，
  典型是 ``http://127.0.0.1:8188``（SSH 隧道、frp、docker ``-p`` 都算这一种）；
- **SSL 链接**：只有 HTTPS 域名能给出来的时候（AUTODL 的「自定义服务」就是典型），
  形如 ``https://xxxxxxxx-8188.gpuhub.com`` —— 这是 AUTODL 画图镜像**唯一**安全的
  访问方式，不能直连容器端口。

两种挂载方式最后都落到同一个 ``base_url`` 上（默认再补 ``/v1`` 前缀），所以
``services.toml`` 的 schema 不变。:func:`probe` 只做只读探测，用来回答
「这个地址通不通、后面是 OpenAI 兼容服务还是 ComfyUI 原生接口」——ComfyUI 原生
接口没有 ``/images/generations``，需要先加一层 OpenAI 兼容转接（或换成
OpenAI 兼容的本地生图服务）。

本模块只依赖标准库；网络请求放在界面层的后台线程里跑（见
:func:`atri_x.gui.widgets.run_async`）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from atri_x.gui.modelcatalog import parse_models

__all__ = [
    "DEFAULT_COMFY_PORT",
    "DEFAULT_HOST",
    "DEFAULT_PREFIX",
    "KIND_AUTH",
    "KIND_COMFY",
    "KIND_OPENAI",
    "KIND_UNKNOWN",
    "MOUNT_CHOICES",
    "MOUNT_EXTERNAL",
    "MOUNT_PORT",
    "MOUNT_SSL",
    "ImageMountError",
    "MountSpec",
    "ProbeResult",
    "build_base_url",
    "candidate_roots",
    "describe",
    "normalize_prefix",
    "probe",
]

#: 外部 API（用户自己填完整 Base URL，保持原有行为）。
MOUNT_EXTERNAL = "external"
#: 本地服务 · 端口映射（``http://<主机>:<端口>``）。
MOUNT_PORT = "port"
#: 本地服务 · SSL 链接（AUTODL 自定义服务 / 反向代理）。
MOUNT_SSL = "ssl"

#: 界面下拉项：(值, 显示名)。
MOUNT_CHOICES = (
    (MOUNT_EXTERNAL, "外部 API（OpenAI 兼容）"),
    (MOUNT_PORT, "本地服务 · 端口映射"),
    (MOUNT_SSL, "本地服务 · SSL 链接（AUTODL 等）"),
)

#: 端口映射的默认主机与端口（ComfyUI 默认 8188）。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_COMFY_PORT = 8188
#: OpenAI 兼容路径前缀（ComfyUI-OpenAI 转接一般也挂在这个前缀下）。
DEFAULT_PREFIX = "/v1"

#: 探测结果类型。
KIND_OPENAI = "openai"
KIND_COMFY = "comfyui"
KIND_AUTH = "auth"
KIND_UNKNOWN = "unknown"

#: 探测响应体上限（只用来识别服务类型，不需要大响应）。
MAX_PROBE_BYTES = 2 << 20
#: 探测超时（秒）。
PROBE_TIMEOUT = 8.0


class ImageMountError(Exception):
    """挂载参数不合法（消息直接展示给用户）。"""


@dataclass(frozen=True)
class MountSpec:
    """一次挂载选择的参数（界面上的几个输入框）。

    ``mode == MOUNT_EXTERNAL`` 时用 ``url``；``MOUNT_PORT`` 用 ``host``/``port``；
    ``MOUNT_SSL`` 用 ``url``。三种模式都会带上 ``prefix``。
    """

    mode: str = MOUNT_EXTERNAL
    host: str = DEFAULT_HOST
    port: int = DEFAULT_COMFY_PORT
    url: str = ""
    prefix: str = DEFAULT_PREFIX

    @property
    def is_local(self) -> bool:
        """是否属于「本地生图服务」这一类挂载方式。"""
        return self.mode in (MOUNT_PORT, MOUNT_SSL)

    def describe(self) -> str:
        """一句话描述当前挂载（状态行用）。"""
        if self.mode == MOUNT_PORT:
            return f"端口映射 {self.host}:{self.port}{normalize_prefix(self.prefix)}"
        if self.mode == MOUNT_SSL:
            return f"SSL 链接 {self.url}{normalize_prefix(self.prefix)}"
        return "外部 API"


@dataclass(frozen=True)
class ProbeResult:
    """一次探测的结果（只读，不写配置）。"""

    ok: bool = False
    kind: str = KIND_UNKNOWN
    detail: str = ""
    models: tuple[str, ...] = ()
    base_url: str = ""
    checked: tuple[str, ...] = field(default_factory=tuple)

    @property
    def openai_compatible(self) -> bool:
        return self.kind in (KIND_OPENAI, KIND_AUTH)


# ---------------------------------------------------------------------------
# 地址
# ---------------------------------------------------------------------------
def normalize_prefix(prefix: str) -> str:
    """把前缀规整成 ``/v1`` 这种形态；空串表示不加前缀。"""
    text = (prefix or "").strip()
    if not text:
        return ""
    if text in ("/", "./"):
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/")


def build_base_url(spec: MountSpec) -> str:
    """按挂载方式拼出 Base URL；参数不合法时抛 :class:`ImageMountError`。

    - 端口映射：``http://127.0.0.1:8188/v1``（主机可填局域网 IP 或域名）；
    - SSL 链接：``https://xxx.gpuhub.com/v1``（已经带 ``/v1`` 时不重复追加）。
    """
    prefix = normalize_prefix(spec.prefix)
    if spec.mode == MOUNT_PORT:
        host = (spec.host or "").strip() or DEFAULT_HOST
        if "://" in host:
            raise ImageMountError("端口映射的「主机」只填 IP 或域名（不要带 http://）")
        if "/" in host:
            raise ImageMountError("端口映射的「主机」不能带路径；路径请填到「接口前缀」")
        port = int(spec.port or DEFAULT_COMFY_PORT)
        if not (1 <= port <= 65535):
            raise ImageMountError(f"端口超出范围：{port}（1-65535）")
        return f"http://{host}:{port}{prefix}"
    if spec.mode == MOUNT_SSL:
        raw = (spec.url or "").strip()
        if not raw:
            raise ImageMountError("SSL 链接不能为空（例如 https://xxxx-8188.gpuhub.com）")
        parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
        if parsed.scheme not in ("http", "https"):
            raise ImageMountError("SSL 链接只支持 http:// 或 https://")
        if not parsed.netloc:
            raise ImageMountError(f"SSL 链接缺少域名：{raw}")
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")
        if path:
            base += path
        if prefix and not base.endswith(prefix):
            base += prefix
        return base
    if spec.mode == MOUNT_EXTERNAL:
        raw = (spec.url or "").strip().rstrip("/")
        if not raw:
            raise ImageMountError("外部 API 需要先填 Base URL（例如 https://relay.example.com/v1）")
        parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ImageMountError(f"Base URL 不合法：{raw}")
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if prefix and not base.endswith(prefix):
            base += prefix
        return base
    raise ImageMountError(f"未知的挂载方式：{spec.mode!r}")


def candidate_roots(base_url: str) -> list[str]:
    """由 Base URL 推出「服务根地址」候选（去掉结尾的 ``/v1`` 或 ``/models``）。"""
    base = (base_url or "").strip().rstrip("/")
    roots: list[str] = []
    if not base:
        return roots
    trimmed = base
    for suffix in ("/models", "/images/generations"):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
    roots.append(trimmed)
    if trimmed.endswith("/v1"):
        root = trimmed[: -len("/v1")]
        if root and root not in roots:
            roots.append(root)
    return roots


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
def probe(
    base_url: str,
    api_key: str = "",
    *,
    timeout: float = PROBE_TIMEOUT,
) -> ProbeResult:
    """只读探测一个 Base URL 后面是什么服务，**不抛异常**（失败写进 ``detail``）。

    顺序：先按 OpenAI 兼容找 ``/models``（有模型清单就是它），再看
    ``/system_stats`` / ``/queue`` 是不是 ComfyUI 原生接口。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ProbeResult(detail="先填一个地址再检测", base_url="")
    checked: list[str] = []
    errors: list[str] = []
    models_urls = [base + "/models"]
    for root in candidate_roots(base):
        candidate = root + "/models"
        if candidate not in models_urls:
            models_urls.append(candidate)
    for url in models_urls[:3]:
        checked.append(url)
        status, data, error = _get_json(url, api_key, timeout)
        if error:
            errors.append(error)
            continue
        if status in (401, 403):
            return ProbeResult(
                ok=True,
                kind=KIND_AUTH,
                detail=f"服务可达，但需要鉴权（HTTP {status}）：检查密钥后再试",
                base_url=base,
                checked=tuple(checked),
            )
        if status == 200 and data is not None:
            models = parse_models(data)
            if models or _looks_like_openai_models(data):
                return ProbeResult(
                    ok=True,
                    kind=KIND_OPENAI,
                    detail=f"OpenAI 兼容生图服务（{len(models)} 个模型）",
                    models=tuple(models),
                    base_url=base,
                    checked=tuple(checked),
                )
        if status == 404:
            errors.append(f"{url} → HTTP 404")
        else:
            errors.append(f"{url} → HTTP {status}")

    for root in candidate_roots(base):
        for suffix in ("/system_stats", "/queue"):
            url = root + suffix
            checked.append(url)
            status, data, error = _get_json(url, api_key, timeout)
            if error:
                errors.append(error)
                continue
            if status == 200 and isinstance(data, dict) and _looks_like_comfy(data, suffix):
                return ProbeResult(
                    ok=True,
                    kind=KIND_COMFY,
                    detail=(
                        "检测到 ComfyUI 原生接口（可访问）；但它没有 "
                        "/images/generations —— ATRI-X 需要 OpenAI 兼容转接，"
                        "或改用 OpenAI 兼容的本地生图服务"
                    ),
                    base_url=base,
                    checked=tuple(checked),
                )
    detail = "没有检测到生图服务。"
    if errors:
        detail += " 已尝试：" + "；".join(errors[-4:])
    return ProbeResult(ok=False, kind=KIND_UNKNOWN, detail=detail, base_url=base, checked=tuple(checked))


def describe(result: ProbeResult) -> str:
    """把探测结果渲染成一行状态（界面状态行用）。"""
    if not result.ok:
        return "检测失败：" + (result.detail or "未知原因")
    if result.kind == KIND_COMFY:
        return "检测到 ComfyUI 原生接口：需要 OpenAI 兼容转接后才能给 ATRI-X 调用"
    return result.detail or "检测通过"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _looks_like_openai_models(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    return any(key in data for key in ("data", "models", "object"))


def _looks_like_comfy(data: dict, suffix: str) -> bool:
    if suffix == "/system_stats":
        return "system" in data or "devices" in data
    return "queue_remaining" in data or "queue_running" in data


def _get_json(
    url: str, api_key: str, timeout: float
) -> tuple[int, object | None, str]:
    """GET 一个 JSON 地址；返回 ``(状态码, 解析结果, 错误文本)``。

    网络层失败时状态码为 0、错误文本写明原因；HTTP 非 2xx 也返回状态码与响应体
    解析结果（能解析就解析），让调用方自己决定怎么提示。
    """
    headers = {"Accept": "application/json", "User-Agent": "ATRI-X-GUI"}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 用户配置的地址
            status = int(getattr(response, "status", 200) or 200)
            body = response.read(MAX_PROBE_BYTES)
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            body = exc.read(MAX_PROBE_BYTES)
        except Exception:  # noqa: BLE001 - 读不到响应体就当空
            body = b""
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - 关不掉不影响判断
            pass
    except urllib.error.URLError as exc:
        return 0, None, f"连不上 {url}：{getattr(exc, 'reason', exc)}"
    except TimeoutError:
        return 0, None, f"{url} 超时（{timeout:.0f} 秒）"
    except OSError as exc:
        return 0, None, f"请求 {url} 失败：{exc}"
    parsed: object | None = None
    if body:
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            parsed = None
    return status, parsed, ""
