<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# 术语表（自动生成 + 人工补）

三套命名的对照：中文功能名（人） ↔ Python 域（代码） ↔ 上游 Go 包（对照）。查**具体模块**用 `devdocs/generated/module-map.md`；查**按任务入口**用 `docs/dev/find-it.md`。

| 中文功能名 | Python 域 | 上游 Go 包 | 模块数 | 测试覆盖 | 配置段 |
|---|---|---|---:|---:|---|
| Agent 对话编排 | `atri_x/agent/` | `internal/agent` | 147 | 88/147 | `[agent]`、`[agent.dsh]` |
| 工具与工具运行时 | `atri_x/tool/` | `internal/tool/builtin` | 82 | 60/82 | `state.toml` 的 `tool_allowlist`、`[tools]` |
| 平台适配器 | `atri_x/platform/` | `internal/platform/qq-onebot` | 69 | 50/69 | `[platform.*]` |
| 桌面端（Tk + 托盘） | `atri_x/gui/` | `—` | 72 | 49/72 | `state.toml` 与各服务的 GUI 段 |
| 存储与 SQLite | `atri_x/storage/` | `internal/storage/sqlite` | 19 | 10/19 | `[storage]` |
| 配置与运行时状态 | `atri_x/config/` | `internal/config` | 10 | 9/10 | `[app]` / `[security]` / `[connect_notice]` 等 |
| Hook 与插件 | `atri_x/hook/` | `internal/hook/rules` | 39 | 21/39 | `hooks.toml` |
| LLM 适配与协议 | `atri_x/llm/` | `internal/llm/openai` | 15 | 13/15 | `[llm]` / `[models]` |
| 装配层 | `atri_x/app/` | `internal/app` | 34 | 29/34 | `[app]` 与各服务装配段 |
| 媒体与图片处理 | `atri_x/media/` | `internal/media` | 27 | 11/27 | `[image_generation]` / `[media]` |

## 常用别名

| 别名 / 旧名 | 指什么 |
|---|---|
| Atriwisp / Atrinis / Atriena | 三条内置角色能力线，见 `docs/atrinis.md` |
| 协议端 / proto | Omni-Bot 协议后端（NapCat / SnowLuma），见 `devdocs/qq-backends-matrix.md` |
| 群友人设 | 按「平台 + 群 + 用户」注入的成员档案，见 `docs/character-library.md` |
| 研究引擎 / research_pack | 随包分发的纯标准库研究流水线，见 `docs/research.md` |
| DSH | 深度路由内核（`runtime/dsh-kernel`），见 `devdocs/dsh-acp.md` |
