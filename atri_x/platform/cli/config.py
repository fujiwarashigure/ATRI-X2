"""CLI 平台适配器的配置解析与装配入口。

对应 Go：``internal/platform/cli/config.go``。

模块职责：把 ``[platform.cli]`` 原始小节解码成 :class:`Config`，并补齐 Go 侧
``defaultConfig`` / ``applyConfigDefaults`` 的默认值。``atri_x/platform/builtin.py``
（对应 Go ``internal/platform/builtin``）会调用
:func:`new_config_from_platform_config` 装配本地适配器与远程服务端。

与 Go 的差异（仅形式）：

* Go 用 ``toml.Unmarshal`` 把嵌套的 ``server`` / ``clients`` 直接灌进**已定型**的
  struct；Python 侧的 :func:`atri_x.platform.config.decode_config` 是按属性名赋值，
  嵌套小节送进来仍是原始 ``Mapping``。因此 :func:`apply_config_defaults` 会先做一次
  **形状归一**（``Mapping`` → :class:`ServerConfig` / ``ClientConfig``），再补默认值。
  这是 Python 缺 struct tag 解码带来的必要差别，不是语义差别。
* ``(Config, error)`` → 返回 :class:`Config`、非法时抛 :class:`atri_x.errors.InvalidError`。
* Go ``Config`` 的 toml tag（``enabled`` / ``default_client`` / ``default_url`` /
  ``server`` / ``clients``，以及 ``ServerConfig`` 的 ``enabled`` / ``listen`` /
  ``tokens``、``ClientConfig`` 的 ``id`` / ``url`` / ``token_env``）在 Python 侧
  直接体现为同名的 snake_case 字段名，:func:`decode_config` 因此能逐键命中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.config import decode_config

#: 默认监听地址。对应 Go ``defaultRemoteAddr``。
DEFAULT_REMOTE_ADDR = "127.0.0.1:32172"

#: 远程 CLI 的 WebSocket 路径。对应 Go ``defaultRemotePath``。
DEFAULT_REMOTE_PATH = "/cli/v1/ws"

#: 默认客户端档案名。对应 Go ``defaultConfig`` 里的字面量 ``"local"``。
DEFAULT_CLIENT = "local"

__all__ = [
    "DEFAULT_CLIENT",
    "DEFAULT_REMOTE_ADDR",
    "DEFAULT_REMOTE_PATH",
    "ClientConfig",
    "Config",
    "ServerConfig",
    "apply_config_defaults",
    "default_config",
    "new_config_from_platform_config",
]


@dataclass
class ServerConfig:
    """远程 CLI 服务端小节（``[platform.cli.server]``）。对应 Go ``ServerConfig``。"""

    enabled: bool = False
    listen: str = ""
    #: 客户端 ID → 存放 token 的环境变量名列表（可多个，取第一个非空且匹配的）。
    tokens: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ClientConfig:
    """远程 CLI 客户端档案（``[platform.cli.clients.<name>]``）。对应 Go ``ClientConfig``。"""

    id: str = ""
    url: str = ""
    token_env: list[str] = field(default_factory=list)


@dataclass
class Config:
    """``[platform.cli]`` 小节。对应 Go ``Config``。"""

    enabled: bool = True
    default_client: str = DEFAULT_CLIENT
    default_url: str = ""
    server: ServerConfig = field(default_factory=ServerConfig)
    clients: dict[str, ClientConfig] = field(default_factory=dict)

    def client(self, name: str) -> tuple[str, ClientConfig]:
        """按档案名取客户端配置。对应 Go ``Config.Client``。

        未给名字时退回 ``default_client``；仍为空且恰好只有一个档案时用那个档案。
        返回 ``(档案名, ClientConfig)``；取不到时抛 :class:`atri_x.errors.InvalidError`
        （消息与 Go 逐条对齐）。
        """
        name = (name or "").strip()
        if name == "":
            name = (self.default_client or "").strip()
        if name == "" and len(self.clients) == 1:
            name = next(iter(self.clients))
        if name == "":
            raise InvalidError("cli client is not configured")
        client = self.clients.get(name)
        if client is None:
            # Go 是 ``%q``（双引号），这里逐字对齐，便于与上游日志交叉检索。
            raise InvalidError(f'cli client "{name}" is not configured')
        if not (client.id or "").strip():
            client.id = name
        if not (client.url or "").strip():
            client.url = self.default_url
        return name, client


def default_config() -> Config:
    """Go ``defaultConfig`` 的等价物。"""
    return Config(
        enabled=True,
        default_client=DEFAULT_CLIENT,
        default_url="ws://" + DEFAULT_REMOTE_ADDR + DEFAULT_REMOTE_PATH,
        server=ServerConfig(
            enabled=False,
            listen=DEFAULT_REMOTE_ADDR,
            tokens={},
        ),
        clients={},
    )


def new_config_from_platform_config(raw: Mapping[str, Any] | None) -> Config:
    """从 ``cfg.Platform["cli"]`` 的原始小节装配 :class:`Config`。

    对应 Go ``NewConfigFromPlatformConfig``。``raw`` 为 ``None`` 或空表时返回全默认配置；
    解码失败时抛 :class:`atri_x.errors.InvalidError`（消息前缀 ``decode cli config: ``，
    与 Go 的 ``fmt.Errorf("decode cli config: %w", err)`` 一致）。
    """
    cfg = default_config()
    try:
        decode_config(raw, cfg)
    except AtriXError as exc:
        raise InvalidError(f"decode cli config: {exc}") from exc
    apply_config_defaults(cfg)
    return cfg


def apply_config_defaults(cfg: Config) -> None:
    """就地归一形状并补齐默认值。对应 Go ``applyConfigDefaults``。"""
    _normalize_shape(cfg)
    if not (cfg.default_client or "").strip():
        cfg.default_client = DEFAULT_CLIENT
    if not (cfg.server.listen or "").strip():
        cfg.server.listen = DEFAULT_REMOTE_ADDR
    if not (cfg.default_url or "").strip():
        cfg.default_url = "ws://" + cfg.server.listen + DEFAULT_REMOTE_PATH
    if cfg.server.tokens is None:
        cfg.server.tokens = {}
    if cfg.clients is None:
        cfg.clients = {}
    for name, client in list(cfg.clients.items()):
        if not (client.id or "").strip():
            client.id = (name or "").strip()
        if not (client.url or "").strip():
            client.url = cfg.default_url
        cfg.clients[name] = client


def _normalize_shape(cfg: Config) -> None:
    """把 ``decode_config`` 落下的原始 ``Mapping`` 归一成本模块的 dataclass。

    Go 侧靠 struct tag 在解码时定型，Python 侧解码只做属性赋值，因此在补默认值之前
    必须先归一，否则 ``cfg.server.listen`` 会直接在 ``dict`` 上取属性而炸。
    """
    if isinstance(cfg.server, Mapping):
        cfg.server = _server_config_from_mapping(cfg.server)
    elif not isinstance(cfg.server, ServerConfig):  # pragma: no cover - 防御性
        cfg.server = ServerConfig()
    if isinstance(cfg.clients, Mapping):
        clients: dict[str, ClientConfig] = {}
        for name, value in cfg.clients.items():
            key = str(name)
            if isinstance(value, ClientConfig):
                clients[key] = value
            elif isinstance(value, Mapping):
                clients[key] = _client_config_from_mapping(value)
            else:  # pragma: no cover - 防御性
                clients[key] = ClientConfig()
        cfg.clients = clients
    elif not isinstance(cfg.clients, dict):  # pragma: no cover - 防御性
        cfg.clients = {}
    if cfg.server.tokens is not None and not isinstance(cfg.server.tokens, Mapping):
        cfg.server.tokens = {}  # pragma: no cover - 防御性
    elif isinstance(cfg.server.tokens, Mapping):
        cfg.server.tokens = {
            str(name): _string_list(value) for name, value in cfg.server.tokens.items()
        }


def _server_config_from_mapping(raw: Mapping[str, Any]) -> ServerConfig:
    out = ServerConfig()
    if "enabled" in raw:
        out.enabled = bool(raw.get("enabled"))
    listen = raw.get("listen")
    if isinstance(listen, str):
        out.listen = listen
    tokens = raw.get("tokens")
    if isinstance(tokens, Mapping):
        out.tokens = {str(name): _string_list(value) for name, value in tokens.items()}
    return out


def _client_config_from_mapping(raw: Mapping[str, Any]) -> ClientConfig:
    out = ClientConfig()
    identifier = raw.get("id")
    if isinstance(identifier, str):
        out.id = identifier
    url = raw.get("url")
    if isinstance(url, str):
        out.url = url
    out.token_env = _string_list(raw.get("token_env"))
    return out


def _string_list(value: Any) -> list[str]:
    """``[]string`` 的宽松归一：字符串当成单元素列表，``None`` 当空列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value if item is not None]
    return []
