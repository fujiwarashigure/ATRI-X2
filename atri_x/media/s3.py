"""S3 兼容对象存储后端。

对应 Go：``internal/media/s3.go``。

Go 走 ``aws-sdk-go-v2``（``s3.Client`` / ``s3.PresignClient``）；本仓走
:mod:`atri_x.net.s3` 的纯标准库 SigV4 实现（``S3Client``），语义对齐：

* 对象键：``media/<64 位十六进制>``（Go ``objectKey``）。
* ``Put`` 返回对象键并从 ``Manager`` 侧写进 ``media.object_key``。
* ``Open`` 优先用记录里的 ``object_key``，缺失时回落到按 ID 推导的键。
* ``Remove`` 与 Pre``signGet`` 同样。

**按需初始化**（``devdocs/architecture.md``）：本模块的构造失败只应让实际远端操作
失败，不能阻止应用启动 —— 因此 :func:`new_s3_backend` 只在 ``Manager`` 第一次需要
远端后端时被调用（``remote_factory``），凭据缺失时抛
:class:`atri_x.media.types.MediaError`，由 ``Manager`` 记警告并让该次操作失败。

与 Go 的差异：Go 的 ``S3Backend`` 接收 aws-sdk 的 ``CredentialsProvider`` 并自行
加载凭据链；本仓没有 SDK，凭据来源顺序见 :func:`resolve_credentials`。
``FileDeliveryConfig.s3_public_base_url`` 在 Go 侧也不被媒体后端使用，本仓同样
只在配置里保留字段。
"""

from __future__ import annotations

import io
import os
from typing import Any, BinaryIO, Mapping

from atri_x.media.config import FileDeliveryConfig
from atri_x.media.types import IDPrefix, MediaError
from atri_x.net.s3 import S3Client, S3Config

__all__ = ["S3Backend", "new_s3_backend", "object_key", "resolve_credentials", "s3_config"]


def object_key(id: str) -> str:
    """对应 Go ``objectKey``。"""
    return "media/" + (id[len(IDPrefix):] if id.startswith(IDPrefix) else id)


def resolve_credentials(
    cfg: FileDeliveryConfig, credentials: Any = None
) -> tuple[str, str, str]:
    """解析 S3 凭据，返回 ``(access_key, secret_key, session_token)``。

    顺序与 Go 的 provider 链精神一致，但形态收敛成三种：

    1. ``credentials`` 是 Mapping / 带 ``access_key`` 属性的对象；
    2. ``credentials`` 是带 ``retrieve()`` 的 provider（对应 aws-sdk 的
       ``CredentialsProvider.Retrieve``），其结果再按第 1 条读取；
    3. 都没有时读 ``cfg.s3_access_key`` / ``cfg.s3_secret_key``，仍为空则读
       ``cfg.s3_access_key_env`` / ``cfg.s3_secret_key_env`` 指向的环境变量。
    """
    access = secret = token = ""
    if credentials is not None:
        source = credentials
        retriever = getattr(credentials, "retrieve", None)
        if callable(retriever):
            source = retriever()
            if hasattr(source, "__await__"):  # pragma: no cover - 同步入口不接受协程
                raise MediaError("s3 credentials provider must be synchronous")
        if isinstance(source, Mapping):
            access = str(source.get("access_key") or source.get("access_key_id") or "")
            secret = str(source.get("secret_key") or source.get("secret_access_key") or "")
            token = str(source.get("session_token") or "")
        else:
            access = str(
                getattr(source, "access_key", "") or getattr(source, "access_key_id", "") or ""
            )
            secret = str(
                getattr(source, "secret_key", "")
                or getattr(source, "secret_access_key", "")
                or ""
            )
            token = str(getattr(source, "session_token", "") or "")
    if not access:
        access = cfg.s3_access_key or _env(cfg.s3_access_key_env)
    if not secret:
        secret = cfg.s3_secret_key or _env(cfg.s3_secret_key_env)
    return access.strip(), secret.strip(), token.strip()


def _env(name: str) -> str:
    return os.environ.get(name, "") if name else ""


def s3_config(cfg: FileDeliveryConfig, credentials: Any = None) -> S3Config:
    """把 ``[file_delivery]`` 段映射成 :class:`atri_x.net.s3.S3Config`。

    显式配置了 ``s3_endpoint`` 时用 path-style（Go 的
    ``options.UsePathStyle = cfg.S3Endpoint != ""``），否则用 AWS 默认的
    virtual-hosted 形态。
    """
    access, secret, token = resolve_credentials(cfg, credentials)
    return S3Config(
        endpoint=cfg.s3_endpoint,
        region=cfg.s3_region or "auto",
        bucket=cfg.s3_bucket,
        access_key=access,
        secret_key=secret,
        session_token=token,
        path_style=bool(cfg.s3_endpoint) and cfg.s3_path_style,
    )


class S3Backend:
    """S3 兼容后端。对应 Go ``S3Backend``。"""

    def __init__(self, config: S3Config, client: S3Client | None = None) -> None:
        self.config = config
        self.bucket = config.bucket
        self.client = client if client is not None else S3Client(config)

    # -- Backend ------------------------------------------------------------
    async def put(
        self, ctx: Any, id: str, data: bytes, size: int, content_type: str
    ) -> str:
        """上传对象，返回对象键。对应 Go ``S3Backend.Put``。"""
        key = object_key(id)
        try:
            await self.client.put_object(key, bytes(data), content_type or "application/octet-stream")
        except Exception as exc:  # noqa: BLE001 - 统一成媒体域错误并保留 cause
            raise MediaError(f"put media object: {exc}") from exc
        return key

    async def open(self, ctx: Any, media: Any) -> BinaryIO:
        """下载对象为内存流。对应 Go ``S3Backend.Open``。"""
        key = getattr(media, "object_key", "") or object_key(media.id)
        try:
            body = await self.client.get_object(key)
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"get media object: {exc}") from exc
        return io.BytesIO(body)

    async def remove(self, ctx: Any, media: Any) -> None:
        """删除对象。对应 Go ``S3Backend.Remove``。"""
        key = getattr(media, "object_key", "") or object_key(media.id)
        try:
            await self.client.delete_object(key)
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"delete media object: {exc}") from exc

    async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
        """生成预签名下载 URL。对应 Go ``S3Backend.PresignGet``。"""
        key = getattr(media, "object_key", "") or object_key(media.id)
        seconds = int(expiry) if expiry and expiry > 0 else 3600
        try:
            return self.client.presign_get(key, seconds)
        except Exception as exc:  # noqa: BLE001
            raise MediaError(f"presign media object: {exc}") from exc


def new_s3_backend(cfg: FileDeliveryConfig, credentials: Any = None) -> S3Backend:
    """构造 S3 后端。对应 Go ``NewS3Backend``。

    凭据不可用时抛 :class:`atri_x.media.types.MediaError`；调用方（``Manager``）
    必须把它当成**该次远端操作**的失败，而不是启动失败。
    """
    access, secret, _token = resolve_credentials(cfg, credentials)
    if not access or not secret:
        raise MediaError("s3 credentials are not configured")
    return S3Backend(s3_config(cfg, credentials))
