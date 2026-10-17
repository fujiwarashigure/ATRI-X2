"""网络层。对应 Go 的标准库 ``net/http`` 与 ``github.com/coder/websocket``。

Python 标准库不提供统一的异步 HTTP 客户端与 WebSocket 实现，这里补齐：

- :mod:`atri_x.net.httpclient` —— HTTP/1.1 客户端：缓冲请求、SSE 流式读取、
  multipart 上传、代理、TLS 校验开关、重试与 RetryNotifier 回调。
- :mod:`atri_x.net.websocket` —— RFC 6455 客户端与服务端（OneBot / QQ 官方
 网关 / 远程 CLI 用）。
- :mod:`atri_x.net.s3` —— SigV4 签名的最小 S3 客户端。
- :mod:`atri_x.net.tomlw` —— TOML 写入（``tomllib`` 只读）。
- :mod:`atri_x.net.cronspec` —— cron 表达式解析。
"""

from __future__ import annotations
