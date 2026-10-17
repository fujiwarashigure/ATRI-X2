# 阿里云 / 腾讯云轻量服务器部署（Python 版）

适用于阿里云轻量应用服务器（Lighthouse）、腾讯云轻量应用服务器，
以及任何装了 Docker 的 Linux VPS。

## 1. 最短路径

```bash
# 1) 拿包（本机生成或从发布页下载）
python deploy/pack/prepare_offline.py

# 2) 上传解压
scp deploy/dist/atri-x-0.9.0-offline-amd64.tar.gz root@<服务器>:/root/
ssh root@<服务器>
tar -xzf atri-x-0.9.0-offline-amd64.tar.gz && cd offline-amd64

# 3) 填环境变量
cp .env.example .env && chmod 600 .env
vim .env

# 4) 一条命令部署
python3 deploy.py --load
```

没有预构建镜像时用 `python3 deploy.py`（会 `docker compose build`）。

## 2. 安全组 / 防火墙

**只开 SSH（22）和 HTTPS（443）**。ATRI-X 自己的端口全部只监听到宿主机回环：

| 端口 | 是否对公网开放 |
|---|---|
| 32171（健康接口） | **不要**开。宿主机 watchdog / 监控用 |
| 32172（CLI WebSocket） | **不要**直接开。走 Nginx 反代 `wss://域名/cli/v1/ws` |
| 32170（Atrinis 事件） | 仅在启用 Atrinis 时，走 Nginx 反代 `/atriena/` |

## 3. 域名 + TLS

宝塔面板：网站 → 添加站点 → 申请 Let's Encrypt 证书 → 把
`nginx-atri-x.conf` 的内容贴进该站点的 `server {}`（或按注释用「反向代理」界面），
然后 `nginx -t && systemctl reload nginx`。

验证：

```bash
curl -sS https://<域名>/cli/v1/ws       # 预期 400/426（WebSocket 升级失败），不是 404
python3 health_probe.py                 # 宿主机回环探活
```

## 4. 系统服务化

不要同时用「宝塔 Docker 管理器」和 systemd 托管同一套 Compose，二选一。

systemd 方式：

```bash
mkdir -p /opt/atri-x && cp -a /root/offline-amd64/. /opt/atri-x/
cp /opt/atri-x/atri-x-compose.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now atri-x-compose
```

看门狗：

```bash
cp /opt/atri-x/atri-x-watchdog.service /opt/atri-x/atri-x-watchdog.timer /etc/systemd/system/
cp /opt/atri-x/watchdog.env.example /opt/atri-x/deploy/watchdog.env
systemctl daemon-reload && systemctl enable --now atri-x-watchdog.timer
```

## 5. 备份

```bash
cd /opt/atri-x
python3 backup.py                       # 一致性备份到 backups/
```

放进 crontab（每天 04:17）：

```cron
17 4 * * * cd /opt/atri-x && /usr/bin/python3 backup.py >> /var/log/atri-x-backup.log 2>&1
```

备份目录会保留最近 14 份（`KEEP` 覆盖）。建议再把 `backups/` 同步到对象存储
（`rsync` / `ossutil`）。

## 6. 升级与回滚

```bash
cd /opt/atri-x
python3 upgrade.py            # 快照 → 构建 → 预检 → 重建 → 验收
python3 rollback.py           # 出问题时回滚（会先用旧镜像验证数据快照）
```

升级前请确认 `deploy/VERSION` 与要升到的版本一致，或用
`ATRIX_VERSION=<版本> ATRIX_ALLOW_VERSION_MISMATCH=1` 显式放行。

## 7. 常见问题

**Q：`docker compose build` 卡在拉基础镜像。**
A：国内网络可以在 `.env` 里设
`PY_BASE_IMAGE=registry.cn-hangzhou.aliyuncs.com/library/python:3.14-slim`
（腾讯云换成 `ccr.ccs.tencentyun.com` 的对应路径）。

**Q：容器反复重启。**
A：先看日志 `cd /opt/atri-x && docker compose logs --tail=200`，
再看 `python3 health_probe.py --json`。最常见原因是 `.env` 里的 API Key 没填，
或数据目录不可写（`chown -R 10001:10001 data`）。

**Q：`/tasks` `/metrics` 返回 404。**
A：这两个接口在没设 `ATRIX_OPS_TOKEN` 时**默认不注册**，属预期行为。

**Q：内存不够（2 核 4G 要跑宝塔 + ATRI-X）。**
A：compose 已经限制 `mem_limit: 1536m` / `pids_limit: 512`。若还紧张，
先关掉不用的平台适配（OneBot / Telegram 只留一个）与生图。
