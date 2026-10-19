"""图像后端探测：当前生效的是 Pillow 加速后端还是纯标准库后端。

**``import PIL`` 只发生在本模块**（:func:`pillow_module`），``image.py`` 与打包脚本
都调这里 —— 因此"Pillow 到底能不能用"全仓只有一个判定，不会有第二处
``try/except ImportError`` 给出不同答案。运行时依赖仍是纯标准库：Pillow 不可用
（未安装、安装损坏、版本不兼容）时静默回落纯 Python 后端。

对外接口：

* :func:`pillow_module` —— ``PIL.Image`` 模块或 ``None``；
* :func:`pillow_version` —— Pillow 版本号（不可用为 ``""``）；
* :func:`image_backend` —— **生效中**的后端：``"pillow"`` / ``"stdlib"``；
* :func:`image_backend_info` —— 上面两项 + 是否装上 + 不可用原因，供
  ``atri_x.app.doctor`` / ``/diagnostics`` 与 ``packaging/build.py`` 共用；
* :func:`reset_probe` —— 清缓存（测试模拟"没装 Pillow"用）。

部署侧核验命令（打包产物同样可用，不需要网络）::

    atri-x.exe -m atri_x.media.imagecodec.backend_probe --json

输出形如 ``{"backend": "pillow", "version": "12.3.0", ...}``；``backend=stdlib``
说明产物里没有可用的 Pillow，只能走纯 Python 编解码（功能完整、速度较慢）。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence

__all__ = [
    "image_backend",
    "image_backend_info",
    "main",
    "pillow_module",
    "pillow_version",
    "reset_probe",
]

#: 探测缓存：``_PROBED`` 置位后不再重复 import（Pillow 装不上时重复 import 很贵）。
_PIL_MODULE: Any = None
_PIL_VERSION = ""
_PIL_REASON = ""
_PROBED = False


def _probe() -> Any:
    """执行一次 ``import PIL``；任何异常都当成"不可用"（含损坏安装）。"""
    global _PIL_MODULE, _PIL_VERSION, _PIL_REASON, _PROBED
    if _PROBED:
        return _PIL_MODULE
    _PROBED = True
    try:
        import PIL  # type: ignore[import-not-found]
        from PIL import Image as pil_image  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - 可选依赖：任何失败都必须降级而不是崩
        _PIL_MODULE = None
        _PIL_VERSION = ""
        _PIL_REASON = "%s: %s" % (type(exc).__name__, exc)
        return None
    _PIL_MODULE = pil_image
    _PIL_VERSION = str(getattr(PIL, "__version__", "") or "")
    _PIL_REASON = ""
    return _PIL_MODULE


def pillow_module() -> Any:
    """``PIL.Image`` 模块；不可用时返回 ``None``（只探测一次）。"""
    return _probe()


def pillow_version() -> str:
    """Pillow 版本号；不可用（或取不到版本）时返回空串。"""
    _probe()
    return _PIL_VERSION


def reset_probe() -> None:
    """清空探测缓存，下次调用重新探测（测试与热更新场景用）。"""
    global _PIL_MODULE, _PIL_VERSION, _PIL_REASON, _PROBED
    _PIL_MODULE = None
    _PIL_VERSION = ""
    _PIL_REASON = ""
    _PROBED = False


def image_backend() -> str:
    """**生效中**的后端：``"pillow"`` 或 ``"stdlib"``。

    以 :func:`atri_x.media.imagecodec.image.backend` 为准：它尊重环境变量
    ``ATRI_X_IMAGECODEC_BACKEND``（``pure`` 会强制纯后端）与 ``set_backend()``，
    所以"装了 Pillow"不等于"在用 Pillow"。这里只把它的 ``"pure"`` 词表翻译成
    对外的 ``"stdlib"``。
    """
    from . import image as _image  # 延迟导入：image 顶层会导入本模块，避免成环

    return "pillow" if _image.backend() == "pillow" else "stdlib"


def image_backend_info() -> dict[str, Any]:
    """诊断用快照：``backend`` / ``version`` / ``pillow_installed`` / ``reason``。

    ``backend`` 是**生效中**的后端（``pillow`` / ``stdlib``）；``version`` 是本机
    装着的 Pillow 版本（没装为空串）——两者一起看才能区分"没装 Pillow"与
    "装了但被 ``ATRI_X_IMAGECODEC_BACKEND=pure`` 强制关掉"。
    """
    pil = _probe()
    return {
        "backend": image_backend(),
        "version": _PIL_VERSION,
        "pillow_installed": pil is not None,
        "reason": _PIL_REASON,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m atri_x.media.imagecodec.backend_probe [--json]``：打印后端信息。"""
    args = list(sys.argv[1:] if argv is None else argv)
    info = image_backend_info()
    if "--json" in args:
        line = json.dumps(info, ensure_ascii=True, sort_keys=True)
    else:
        line = "image_backend: %s %s" % (info["backend"], info["version"] or "-")
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - 模块入口
    raise SystemExit(main())
