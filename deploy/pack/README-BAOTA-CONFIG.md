# 宝塔面板 + Nginx 反代配置

ATRI-X 自身只监听宿主机回环，公网访问统一交给宝塔的 Nginx。

## 1. 端口分工

| 容器端口 | 用途 | 是否进反代 |
|---|---|---|
| 32172 | CLI 远程 WebSocket | 是 → `wss://<域名>/cli/v1/ws` |
| 32170 | Atrinis 事件 HTTP 入口 | 仅启用 Atrinis 时 → `https://<域名>/atriena/` |
| 32171 | 独立健康接口 | **否**。只给宿主机 watchdog / 监控 / `health_probe.py` |

**不要把 32171 放进反代文件。** 它不鉴权（`/live` `/ready` 始终公开），
暴露到公网等于免费告诉别人你的服务状态。

## 2. 前置条件

- `app.toml` 的 `[platform.cli.server]`：`enabled=true`、`listen="0.0.0.0:32172"`；
- 启用 Atrinis 时 `atrinis.toml`：`enabled=true`、`[http] addr="0.0.0.0:32170"`；
- compose 的端口映射只绑 `127.0.0.1`（`deploy/docker-compose.yml` 默认如此）。

⚠️ `listen` / `addr` 写成 `127.0.0.1:xxxxx` 会让 Nginx **连不上**
（容器内的回环不是宿主机的回环）。必须是 `0.0.0.0:xxxxx`，
再由 compose 把宿主机侧限制在 `127.0.0.1`。

## 3. 用法一：宝塔「反向代理」界面

网站 → 你的域名 → 设置 → 反向代理 → 添加反向代理：

| 代理目录 | 目标 URL |
|---|---|
| `/cli/v1/ws` | `http://127.0.0.1:32172` |
| `/atriena/` | `http://127.0.0.1:32170`（仅启用 Atrinis 时） |

WebSocket 需要在配置文件里补上行（界面可能不生成）：

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 3600s;
proxy_buffering off;
```

## 4. 用法二：直接贴配置

把 `deploy/nginx-atri-x.conf` 的内容贴进
`/www/server/panel/vhost/nginx/<域名>.conf` 的 `server {}` 内：

```bash
nginx -t && systemctl reload nginx
```

## 5. 健康检查的坑（重要）

`location = /atri-x/healthz` 那一段代理的是 **Atrinis** 的 `/healthz`。
ATRI-X 默认**不启用** Atrinis，所以：

- 未启用 Atrinis 时该地址会失败，但 ATRI-X 进程、模型 API、OneBot / Telegram、
  CLI 仍可能完全正常；
- **不要**用它判断服务异常，更不要拿它做宝塔的监控探针。

要监控请用宿主机的独立健康接口（不进反代）：

```bash
python3 /opt/atri-x/deploy/health_probe.py            # /live：进程是否还响应
python3 /opt/atri-x/deploy/health_probe.py --ready    # /ready：调度与数据目录就绪
```

## 6. 防火墙 / 安全组

- 安全组只开 22 / 443（需要 80 做 ACME 验证时临时开）；
- **不要**在宝塔「安全」里放行 32170 / 32171 / 32172；
- `curl http://<公网IP>:32171/live` 应当连接超时——能通就说明映射错了。

## 7. 大文件上传

反代 Atrinis 的段落里已经给了 `client_max_body_size 32m`。若你的素材更大，
同时还要调大应用侧的接收上限，否则会被应用先拒掉。

## 8. 排错

```bash
# 容器侧是否真的在监听
docker compose exec atri-x python -m atri_x doctor
# 宿主机侧是否能连
python3 deploy/health_probe.py --json
# 反代链路
tail -n 100 /www/wwwlogs/<域名>.error.log
```

| 现象 | 多半原因 |
|---|---|
| `502 Bad Gateway` | 应用没在监听，或 `listen` 写成了 `127.0.0.1` |
| WebSocket 连上就断 | 反代缺 `Upgrade` / `Connection` 头，或 `proxy_read_timeout` 太短 |
| 404（其他接口正常） | 反代路径写错：CLI 是 `/cli/v1/ws`，不是 `/ws` |
| 401 / 403 | token 不匹配（CLI 用 `ATRIX_CLI_LOCAL_TOKEN`，Atrinis 用 `ATRINIS_HOME_TOKEN`） |
