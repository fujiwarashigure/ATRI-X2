# ATRI-X 文档

这里是面向用户的 ATRI-X（Python 重写版）文档。开发计划、任务拆分和内部设计资料已移到 [`../devdocs/`](../devdocs/)；包级 Go → Python 映射见 [`PORTING.md`](../PORTING.md) §4。

## 推荐阅读顺序

1. [快速开始](getting-started.md)：从配置 API Key 到启动 CLI，完成第一次对话。
2. [配置说明](configuration.md)：了解配置文件、路径规则、Provider、运行数据和插件目录。
3. [命令速查](commands.md)：查看常用 slash 命令和会话管理方式。
4. [核心概念](concepts.md)：理解 Chat / Work 模式、工具发现、Session、Hook、Cron、Skill 和安全策略。
5. [角色素材库](character-library.md)：用文件夹管理角色文本与图片，支持索引检索和 `@char:<id>` 临时启用。
6. [生图服务](image-generation.md)：对接中转站 GPT Image 2.5，自动拼接预设提示词并出图。
7. [图片反推提示词](configuration.md#图片反推提示词-image_to_prompt)：用 `image_to_prompt` 内置工具把参考图交给视觉模型，反推绘图提示词。
8. [视觉兜底 vision](configuration.md#视觉兜底-vision)：纯文本主模型拒绝图片时，先用视觉模型把图片转写成描述再重试。
9. [识图能力与两段式描述](vision.md)：图片精度（`detail`）、每轮带图先描述的两段式、SauceNAO 以图搜图兜底、隐私开关与失败原因。
10. [内容安全（道德限制 + 敏感信息过滤）](safety.md)：词表拦截、敏感信息脱敏、审计日志与图片/文件出口策略。
11. [定时报告](reports.md)：每天两次推送生图量、Token、费用、磁盘和内存；另有每日**会话总结**（`[maintenance.daily_summary]`）。
12. [Hook](hooks.md)：规则 Hook 配置、action 类型、segments 多段输出、exec 脚本和表情提取示例。
13. [Atrinis 监听枢纽](atrinis.md)：了解 Atrinis、Atriwisp、Atriena 和外部事件接入。
14. [Atrinis 配置与使用](atrinis-usage.md)：启用 Atrinis、配置 Atriwisp，并用 Atriena 投递事件。
15. [Atriyph 任务记法](atriyph.md)：用结构化记法描述 LLM Cron 与原生 Skill。
16. [前端 API](frontend-api.md)：WebSocket 协议、消息类型和补全接口，用于编写自定义前端。
17. [部署与运维](../deploy/README.md)：云服务器 / Linux 容器部署、健康接口、watchdog、备份恢复、升级回滚，以及 [Windows 本地容器部署](../deploy/windows/README.md)。
18. [Windows 桌面端与原生应用](windows-app.md)：把 ATRI-X 打成免安装 Python/Docker 的 exe（窗口版 + 控制台版），带托盘常驻/开机自启/安装卸载/会话列表，可选内置 DSH 内核并启用**深度路由**，还能出 Inno Setup 安装包，并接上 QQ OneBot 协议端当机器人；「在线」页可看后台服务/协议端/QQ 平台三段状态并一键上下线。
19. [SnowLuma 使用与配置教程](snowluma.md)：**可选的备用 QQ 协议端**（默认协议端是随包内置的 NapCat）——安装、首次启动、WebUI、进程注入、OneBot 正向 WS 配置、`ATRIX_SNOWLUMA_DIR` 探测、ATRI-X 桌面端切换与排障。独立成篇，可单独转发。
20. [文档移植状态](../devdocs/porting-status.md)：哪些内容是从 Go 上游改写而来、哪些命令行接口需要逐条核对。

## 文档维护约定

- `README` 只保留项目介绍、快速入口和最小启动路径。
- `docs/` 放用户使用文档，避免混入开发任务流水账。
- `devdocs/` 放开发计划、任务拆分、架构和接口。
- 新增用户可见功能时，优先更新对应专题文档，而不是把细节全部塞进 README。
- `CHANGELOG.md` 为中文源文件，执行补丁式写作（描述从旧状态怎样变化到新状态），以版本（tag）为节点，平时改动写进 `## [未发布]`，发版时改为 `## [vX.Y.Z] - YYYY-MM-DD` 并新建空的「未发布」小节。本仓库**不保留**英文镜像（上游的 `docs.en/`、`README.en.md`、`CHANGELOG.en.md` 是自动翻译产物，不随移植复制）。

## 当前状态

ATRI-X 仍在开发中，配置、命令和扩展接口可能调整。文档优先覆盖当前稳定且常用的使用路径；实验性能力会尽量标注。
