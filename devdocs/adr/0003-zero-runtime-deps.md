# ADR 0003：运行时零第三方**代码**依赖

- 状态：接受（与上游 Go 的"单静态二进制"精神一致）

## 决定

`pyproject.toml` 的必需依赖只有 `tzdata`（**纯数据包**，IANA 时区库，不含可执行代码）。
Pillow / reportlab / pypdf 都是**可选**加速或导出后端，缺了必须降级，不能崩。

## 为什么

1. **可移植**：目标机不需要预装依赖；Windows 便携版解压即用，源码树 `python -m atri_x` 直接跑。
2. **可审计**：整仓代码可读，安全边界（Shell 解析、脱敏、权限）不依赖黑盒。
3. **许可干净**：避免 copyleft 传染——这也是 `atri_x/shellparse/` 选择 clean-room 自研
   AST 解析、而不是引入 GPLv3 的 `bashlex` 的原因。
4. **冻结产物小**：PyInstaller 只收 `atri_x` 与标准库。

## 代价（真实成本，别美化）

- **Windows 上很多标准库缺口要自己补**：WebSocket（RFC 6455）、TOML 写、S3 SigV4 签名、
  cron 表达式解析、图像编解码、shell AST —— 都在 `atri_x/net/`、`atri_x/media/`、
  `atri_x/shellparse/`、`atri_x/shellparse` 一带。
- **资源采样要手写 `ctypes`**：1.1.4 新增的 `atri_x/sysinfo.py` 用 kernel32 的
  `K32GetProcessMemoryInfo` / `GetProcessTimes` / `GetSystemTimes` / `GlobalMemoryStatusEx`
  拿进程与整机资源，而不是 `psutil`。踩过的坑（句柄 `restype=c_void_p`、结构体 `cb` 初始化、
  `FILETIME` 100ns 单位、kernel 时间含 idle）写在模块注释里。
- **性能上限**低于 C 扩展版本（例如纯 Python 图像编解码、TOML 写）。

## 边界

- 允许**可选**加速后端，但必须 `try/except ImportError` 降级，且在结果里说明降级
  （例如研究助理缺 PDF 库时导出 md/html/docx 而不是假装成功）。
- 不允许为了省事引入"就一个功能"的第三方包——先问：标准库能不能做？自研面有多大？许可是否干净？
- `atri_x/devtools/`（开发期工具）同样只用标准库：生成索引不该要求开发者装东西。

## 相关

- 依赖声明：`pyproject.toml`
- 第三方组件清单与许可：[`LEGAL.md`](../../LEGAL.md)
- 资源采样实现：`atri_x/sysinfo.py`
