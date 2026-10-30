<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/media/` — 媒体与图片处理

## 这个域负责什么

图片编解码、媒体中心（落盘/引用）、表情图库与生图结果的存取。

**不做什么**：不做内容审核策略（在 `safety/`）、不直接发文件（在 `delivery/`）。

**入口**：`atri_x/media/`、`atri_x/reaction/`、`atri_x/artist/`。

**配置段**：`[image_generation]` / `[media]`

## 易错点

- Pillow 是**可选**加速后端：缺它必须降级到纯 Python，不能崩。
- 大图/大文件走投递后端（base64 / 文件），不要塞进模型上下文。
- 临时文件名要可回收；本机 `mkdtemp` 有已知清理问题（见 AGENTS.md）。

## 模块清单（自动生成）

共 **27** 个模块，被测试覆盖 **11** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.media` | 媒体中心（Media Center）。 | 364 | `test_app_tool_runtime_wiring.py`、`test_atriena.py`、`test_builtin_tools3.py`、`test_gui_members.py`、`test_gui_tabs.py`、`test_imagecodec.py`、`test_imagecodec_jpeg_precision.py`、`test_media_image.py`、`test_media_lifecycle.py`、`test_media_manager.py`、`test_media_platform.py`、`test_media_resolver.py`、`test_media_worker.py`、`test_ops_health.py`、`test_tool_media_runtime.py`、`test_toolrun.py`、`test_vision_privacy.py` |
| `atri_x.media.config` | 媒体中心配置。 | 198 | — |
| `atri_x.media.generation` | 导入期的生成参数采集接缝。 | 134 | — |
| `atri_x.media.history` | 跨库历史 owner 对账。 | 48 | — |
| `atri_x.media.image` | 导入期图片压缩与视觉预处理。 | 352 | — |
| `atri_x.media.imagecodec` | 图像编解码包：纯 Python 实现 PNG / baseline JPEG，Pillow 为可选加速后端。 | 59 | `test_gui_members.py`、`test_gui_tabs.py`、`test_imagecodec.py`、`test_imagecodec_jpeg_precision.py`、`test_media_image.py`、`test_media_manager.py`、`test_ops_health.py` |
| `atri_x.media.imagecodec.backend_probe` | 图像后端探测：当前生效的是 Pillow 加速后端还是纯标准库后端。 | 129 | `test_imagecodec_jpeg_precision.py`、`test_ops_health.py` |
| `atri_x.media.imagecodec.image` | 图像编解码公共契约：Image 容器、格式嗅探、后端分发与统一上限。 | 734 | `test_imagecodec_jpeg_precision.py`、`test_ops_health.py` |
| `atri_x.media.imagecodec.jpeg_decode` | 纯 Python baseline JPEG 解码（对应 Go 的 ``image/jpeg`` 解码侧）。 | 768 | `test_imagecodec.py`、`test_imagecodec_jpeg_precision.py` |
| `atri_x.media.imagecodec.jpeg_encode` | 纯 Python baseline JPEG 编码（对应 Go 的 ``image/jpeg.Encode``）。 | 486 | `test_imagecodec_jpeg_precision.py` |
| `atri_x.media.imagecodec.ops` | 图像后处理：白底合成、等比缩放、阈值压缩。 | 208 | — |
| `atri_x.media.imagecodec.png` | 纯 Python PNG 编解码（对应 Go 的 ``image/png``）。 | 433 | `test_imagecodec.py` |
| `atri_x.media.imagecodec.resample` | 纯 Python 重采样：最近邻 / 双线性 / Lanczos3（默认）。 | 184 | — |
| `atri_x.media.lifecycle` | 可重试的后端清理与孤儿宽限期。 | 136 | — |
| `atri_x.media.local` | 本地内容寻址媒体后端。 | 96 | — |
| `atri_x.media.locks` | 按内容 ID 的对象锁。 | 117 | — |
| `atri_x.media.manager` | 媒体中心 Manager：导入、读取、导出、签名与后端选择。 | 728 | `test_toolrun.py` |
| `atri_x.media.mimetype` | 媒体 MIME 判定：内容嗅探与扩展名映射。 | 153 | — |
| `atri_x.media.output` | 元数据读取与发送用临时导出。 | 90 | — |
| `atri_x.media.platform` | 共享平台导入、历史媒体位置关联与本地 ID 查询。 | 161 | `test_media_platform.py` |
| `atri_x.media.reference` | 引用持有与工具参数引用。 | 175 | — |
| `atri_x.media.remote` | 按需初始化的远端（S3）后端包装。 | 49 | — |
| `atri_x.media.resolver` | LLM 媒体解析与传输选择。 | 275 | — |
| `atri_x.media.s3` | S3 兼容对象存储后端。 | 163 | — |
| `atri_x.media.sanitize` | 名称与来源规范化。 | 155 | — |
| `atri_x.media.types` | 媒体中心的核心类型、常量与 ctx 适配。 | 202 | `test_media_platform.py`、`test_toolrun.py` |
| `atri_x.media.worker` | 媒体图片工作进程（隔离压缩的执行器）。 | 565 | `test_media_image.py`、`test_media_worker.py` |

## 对应上游 Go（自动生成）

`internal/media/config.go`、`internal/media/generation.go`、`internal/media/history.go`、`internal/media/image.go`、`internal/media/lifecycle.go`、`internal/media/local.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 796 个 / 超线文件 79 个）。
