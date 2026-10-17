"""消息段 helper。

对应 Go：``internal/llm/segment.go``。

Go 侧 ``segment.go`` 与 ``llm.go`` 同属 ``package llm``；Python 侧把「类型」
放在 :mod:`atri_x.llm.llm`、「消息段操作」放在本模块，此处统一再导出，
使 :mod:`atri_x.llm` 的对外表面等价于 Go 的 ``package llm``。
"""

from __future__ import annotations

from atri_x.llm.llm import (  # noqa: F401
    MessageSegment,
    SegmentFile,
    SegmentImage,
    SegmentText,
    SegmentType,
    append_segment_text,
    append_system_segment_text,
    append_latest_user_segment_text,
    clone_messages,
    file_segments,
    has_image_segment,
    image_reference_text,
    image_segments,
    latest_user_segment_content_text,
    latest_user_segment_text_only,
    latest_user_segments,
    messages_have_image_segment,
    prepend_latest_user_segment_text,
    prepend_segment_text,
    replace_latest_user_segment_text,
    replace_segment_text,
    segments_content_text,
    segments_text_only,
    set_latest_user_segments,
    set_segment_text,
    text_segments,
)

__all__ = [name for name in dir() if not name.startswith("_")]
