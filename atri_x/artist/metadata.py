"""图片容器里的生成参数恢复（只读）：PNG / JPEG / WebP 与 EXIF、XMP。

对应 Go：``internal/artist/metadata.go`` 的**容器解析层**——PNG ``tEXt``/``zTXt``/
``iTXt`` chunk 遍历、JPEG marker 段与 APP1 的 EXIF/XMP、WebP RIFF chunk 遍历、
TIFF/IFD 读取、``UserComment`` 解码，以及对外入口 ``ExtractMetadata``。方言解析层
（A1111 / NovelAI / ComfyUI / InvokeAI）在同包的 ``atri_x/artist/metadata_formats.py``；
两者合起来等于 Go 的单文件 ``metadata.go``。

比 Go 多读一处：PNG 的 ``eXIf`` 块（Go 只读 JPEG/WebP 的 EXIF），见 ``_scan_png``。

这里读到的每个字节都是攻击者可控的第三方数据：声明长度先与剩余缓冲区比对再切片，
解压有硬上限，嵌套与元素数量有闸门，任何问题都变成 ``Metadata.warnings`` 里的中文
降级说明——绝不抛异常、绝不 panic、绝不把读到的文本当指令执行。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zlib

from .metadata_formats import (
    apply_a1111,
    apply_comfy_workflow_fallback,
    apply_comfyui,
    apply_invokeai,
    apply_novelai,
    decode_json_object,
    parse_a1111,
)
from .types import (
    ImageMetadata,
    MaxMetadataTextBytes,
    Metadata,
    MetadataContainerJPEG,
    MetadataContainerPNG,
    MetadataContainerWebP,
    MetadataSourceUnknown,
    MetadataText,
)

# 容器魔数：任何扫描器相信长度字段之前，先匹配这些字节。
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RIFF_MAGIC = b"RIFF"
WEBP_MAGIC = b"WEBP"
EXIF_HEADER = b"Exif\x00\x00"
XMP_JPEG_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_HEADER = b"http://ns.adobe.com/xmp/extension/\x00"

TAG_USER_COMMENT = 0x9286  # EXIF UserComment
TAG_EXIF_IFD = 0x8769  # 指向 Exif 子 IFD 的指针
UNDEFINED_CODE = b"\x00" * 8  # UserComment 的空字符码前缀
XMP_MAX_DEPTH = 64  # XMP 栈深上限（Go 侧同为 64）
XMP_MAX_ELEMENTS = 20000  # 元素数量闸门，对冲 ElementTree 的每元素对象开销
# --- 字节助手 ---

def _split_nul(data: bytes) -> tuple[bytes, bytes, bool]:
    """对应 Go ``metadataSplitNul``：从头部切出一个 NUL 结尾字段。"""
    index = data.find(b"\x00")
    if index >= 0:
        return data[:index], data[index + 1:], True
    return b"", b"", False

def _latin1(raw: bytes) -> str:
    """对应 Go ``metadataLatin1``。"""
    return raw.decode("latin-1")

def _decode_text(raw: bytes) -> str:
    """对应 Go ``metadataDecodeText``：合法 UTF-8 原样保留，否则按 latin-1 解。

    PNG 规范说 ``tEXt``/``zTXt`` 文本是 latin-1，但主流生成器写的是 UTF-8；这样中文提示词
    与真正的 latin-1 字节都不会被弄坏。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return _latin1(raw)

def _utf8_lossy(raw: bytes) -> str:
    """对应 Go ``strings.ToValidUTF8(s, "\\uFFFD")``；非法序列的个数可能不同。"""
    return raw.decode("utf-8", "replace")

def _inflate(raw: bytes, text: MetadataText) -> bytes | None:
    """对应 Go ``metadataInflate``：在硬上限内解压一个 zlib 流。

    ``decompressobj().decompress(raw, 上限 + 1)`` 限长：1 KiB 膨胀到 1 GiB 的块最多只花
    一个受控缓冲。``eof`` 为假说明流被截断，按 Go 的读取失败语义丢弃。"""
    if not raw:
        return None
    object = zlib.decompressobj()
    try:
        decoded = object.decompress(raw, MaxMetadataTextBytes + 1)
    except zlib.error:
        text.warn("压缩文本块无法解压，已跳过")
        return None
    if len(decoded) > MaxMetadataTextBytes:
        text.warn("压缩文本块解压后超过 1 MiB 上限，已丢弃")
        return None
    if not object.eof:
        text.warn("压缩文本块读取失败，已跳过")
        return None
    return decoded

# --- PNG ---

def _scan_png(data: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataScanPNG``：遍历 chunk 列表，长度先与剩余字节比对再切片。"""
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        declared = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        if declared > len(data) - offset - 8:
            text.warn("PNG 数据块声明的长度超出文件范围，已忽略剩余数据")
            return
        body = data[offset + 8:offset + 8 + declared]
        if kind in (b"tEXt", b"zTXt", b"iTXt"):
            _png_text(kind, body, text)
        elif kind == b"eXIf":
            # 有意扩展（Go 的扫描器不读 PNG 的 eXIf 块）：PNG 里的 EXIF 与 JPEG/WebP
            # 同构，带不带 "Exif\0\0" 前缀都能吃，所以没有理由把它扔掉。
            _exif_user_comment(body, text)
        elif kind == b"IEND":
            return
        # 其它块（尤其是 IDAT）按声明长度跳过，其后 4 个 CRC 字节同样跳过。
        offset += 8 + declared + 4

def _png_text(kind: bytes, body: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataPNGText``：解一个文本块（tEXt / zTXt / iTXt）。"""
    if not body:
        text.warn("PNG 文本块为空，已跳过")
        return
    keyword, rest, ok = _split_nul(body)
    if not ok:
        text.warn("PNG 文本块缺少关键字分隔符，已跳过")
        return
    if not keyword:
        text.warn("PNG 文本块的关键字为空，已跳过")
        return
    text.mark()
    if kind == b"tEXt":
        text.add(_decode_text(keyword), _decode_text(rest))
    elif kind == b"zTXt":
        # keyword \0 method zlib-stream：方法占一个字节。有些写入方会多插一个 NUL，
        # 跳过它是安全的——zlib 流不会以 0x00 0x00 开头。
        if not rest or rest[0] != 0:
            text.warn("PNG zTXt 数据块使用了不支持的解压方法，已跳过")
            return
        compressed = rest[1:]
        if len(compressed) >= 2 and compressed[0] == 0 and compressed[1] == 0:
            compressed = compressed[2:]
        decoded = _inflate(compressed, text)
        if decoded is not None:
            text.add(_decode_text(keyword), _decode_text(decoded))
    else:  # iTXt：keyword \0 flag method languageTag \0 translatedKeyword \0 text
        if len(rest) < 2:
            text.warn("PNG iTXt 数据块结构不完整，已跳过")
            return
        flag, method = rest[0], rest[1]
        rest = rest[2:]
        _, rest, ok = _split_nul(rest)
        if ok:
            _, rest, ok = _split_nul(rest)
        if not ok:
            text.warn("PNG iTXt 数据块结构不完整，已跳过")
            return
        if flag == 0:
            text.add(_decode_text(keyword), _utf8_lossy(rest))
        elif flag == 1:
            if method != 0:
                text.warn("PNG iTXt 数据块使用了不支持的解压方法，已跳过")
                return
            decoded = _inflate(rest, text)
            if decoded is not None:
                text.add(_decode_text(keyword), _utf8_lossy(decoded))
        else:
            text.warn("PNG iTXt 数据块的压缩标志非法，已跳过")

# --- JPEG ---

def _scan_jpeg(data: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataScanJPEG``：遍历 marker 段，长度切片前先校验。"""
    offset = 2  # 跳过 SOI
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1  # 不在 marker 边界上：重新同步，而不是盲信当前字节
            continue
        marker = data[offset + 1]
        if marker in (0xFF, 0x00):
            offset += 1
            continue
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xD9:
            return
        length = int.from_bytes(data[offset + 2:offset + 4], "big")
        if length < 2:
            text.warn("JPEG 段声明的长度非法，已忽略剩余数据")
            return
        end = offset + 2 + length
        if end > len(data):
            text.warn("JPEG 段声明的长度超出文件范围，已忽略剩余数据")
            return
        if marker == 0xE1:
            _jpeg_app1(data[offset + 4:end], text)
        if marker == 0xDA:
            return  # Start of scan：后面是熵编码数据，不会再有元数据
        offset = end

def _jpeg_app1(body: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataJPEGAPP1``：读带生成参数的两种 APP1 负载（EXIF 与 XMP）。"""
    if body.startswith(EXIF_HEADER):
        _exif_user_comment(body[len(EXIF_HEADER):], text)
    elif body.startswith(XMP_JPEG_HEADER):
        _xmp_packet(body[len(XMP_JPEG_HEADER):], text)
    elif body.startswith(XMP_EXT_HEADER):
        text.warn("JPEG 扩展 XMP 数据块需要跨段重组，暂不解析")

# --- WebP ---

def _scan_webp(data: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataScanWebP``：遍历 WebP 的 RIFF chunk 列表。"""
    offset = 12
    while offset + 8 <= len(data):
        four_cc = data[offset:offset + 4]
        declared = int.from_bytes(data[offset + 4:offset + 8], "little")
        if declared > len(data) - offset - 8:
            text.warn("WebP 数据块声明的长度超出文件范围，已忽略剩余数据")
            return
        body = data[offset + 8:offset + 8 + declared]
        if four_cc == b"EXIF":
            _exif_user_comment(body, text)
        elif four_cc == b"XMP ":  # 注意 chunk 名的尾随空格
            _xmp_packet(body, text)
        offset += 8 + declared
        if declared % 2 == 1:
            offset += 1  # RIFF chunk 补齐到偶数长度（文件写错时会错位，按长度跳过）

# --- EXIF / TIFF ---

class _TIFFReader:
    """对应 Go ``metadataTIFF``：EXIF 里 TIFF 结构的有界读取器。跟随的每个偏移都来自
    文件本身，因此每一步都做边界检查。"""

    __slots__ = ("data", "order")

    def __init__(self, data: bytes, order: str) -> None:
        self.data = data
        self.order = order  # "little" | "big"，对应 Go 的 binary.ByteOrder

    def u16(self, offset: int) -> tuple[int, bool]:
        """对应 Go ``metadataTIFF.u16``。"""
        if offset < 0 or offset + 2 > len(self.data):
            return 0, False
        return int.from_bytes(self.data[offset:offset + 2], self.order), True

    def u32(self, offset: int) -> tuple[int, bool]:
        """对应 Go ``metadataTIFF.u32``。"""
        if offset < 0 or offset + 4 > len(self.data):
            return 0, False
        return int.from_bytes(self.data[offset:offset + 4], self.order), True

    def find_tag(self, tag: int) -> bytes | None:
        """对应 Go ``metadataTIFF.findTag``：先 IFD0，再它指向的 Exif 子 IFD。"""
        first, ok = self.u32(4)
        if not ok:
            return None
        return self.find_in_ifd(first, tag, 0)

    def find_in_ifd(self, offset: int, tag: int, depth: int) -> bytes | None:
        """对应 Go ``metadataTIFF.findInIFD``；``depth`` 限制子 IFD 指针链。"""
        if depth > 4 or offset < 0 or offset + 2 > len(self.data):
            return None
        count = int.from_bytes(self.data[offset:offset + 2], self.order)
        for index in range(count):
            entry = offset + 2 + index * 12
            if entry + 12 > len(self.data):
                return None
            entry_tag = int.from_bytes(self.data[entry:entry + 2], self.order)
            if entry_tag == tag:
                value = self.entry_value(entry)
                if value is not None:
                    return value
                continue
            if entry_tag != TAG_EXIF_IFD:
                continue
            pointer = self.entry_pointer(entry)
            if pointer is None:
                continue
            value = self.find_in_ifd(pointer, tag, depth + 1)
            if value is not None:
                return value
        return None

    def entry_value(self, entry: int) -> bytes | None:
        """对应 Go ``metadataTIFF.entryValue``：四字节及以下的值内联在条目里。"""
        if entry < 0 or entry + 12 > len(self.data):
            return None
        unit = _tiff_unit_size(int.from_bytes(self.data[entry + 2:entry + 4], self.order))
        if unit == 0:
            return None
        size = int.from_bytes(self.data[entry + 4:entry + 8], self.order) * unit
        if size == 0 or size > len(self.data):
            return None
        if size <= 4:
            return self.data[entry + 8:entry + 8 + size]
        start = int.from_bytes(self.data[entry + 8:entry + 12], self.order)
        if start + size > len(self.data):
            return None
        return self.data[start:start + size]

    def entry_pointer(self, entry: int) -> int | None:
        """对应 Go ``metadataTIFF.entryPointer``：读条目里存的单个 SHORT/LONG。"""
        if entry < 0 or entry + 12 > len(self.data):
            return None
        kind = int.from_bytes(self.data[entry + 2:entry + 4], self.order)
        if kind == 3:  # SHORT
            value, ok = self.u16(entry + 8)
            return value if ok else None
        if kind == 4:  # LONG
            value, ok = self.u32(entry + 8)
            return value if ok else None
        return None

def _tiff_unit_size(kind: int) -> int:
    """对应 Go ``metadataTIFFUnitSize``：TIFF 值类型到元素字节数的映射。"""
    if kind in (1, 2, 6, 7):  # BYTE, ASCII, SBYTE, UNDEFINED
        return 1
    if kind in (3, 8):  # SHORT, SSHORT
        return 2
    if kind in (4, 9, 11):  # LONG, SLONG, FLOAT
        return 4
    if kind in (5, 10, 12):  # RATIONAL, SRATIONAL, DOUBLE
        return 8
    return 0

def _exif_user_comment(block: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataExifUserComment``：读 tag 0x9286（UserComment）；``JIS`` 前缀与
    任何未知字符码一样回落成纯文本（Go 亦然）。

    ``ImageDescription``（0x010E）与 ``XPComment``/``XPTitle``（0x9C9C/0x9C9B）**有意
    不读**：Go 的 metadata.go 只读 UserComment，而把相机/Windows 常见的描述文本收成
    文本块会让 ``found`` 对这些图片变成真，进而挡掉 ``service.py`` 的留档兜底路径。"""
    tiff = block
    if tiff.startswith(EXIF_HEADER):
        tiff = tiff[len(EXIF_HEADER):]
    if len(tiff) < 8:
        return
    if tiff[0:2] == b"II":
        order = "little"
    elif tiff[0:2] == b"MM":
        order = "big"
    else:
        return
    if int.from_bytes(tiff[2:4], order) != 42:
        return
    value = _TIFFReader(tiff, order).find_tag(TAG_USER_COMMENT)
    if value is None:
        return
    comment = _decode_user_comment(value, order)
    if comment:
        text.add("UserComment", comment)

def _decode_user_comment(value: bytes, order: str) -> str:
    """对应 Go ``metadataDecodeUserComment``：按 8 字节字符码解值；未知字符码回落成
    纯文本，免得提示词被静默丢掉。"""
    if len(value) < 8:
        return _decode_text(value).rstrip("\x00")
    prefix = value[:8]
    if prefix == b"ASCII\x00\x00\x00":
        return _latin1(value[8:]).rstrip("\x00")
    if prefix == b"UNICODE\x00":
        return _decode_utf16(value[8:], order)
    if prefix == UNDEFINED_CODE:
        return _decode_text(value[8:]).rstrip("\x00")
    return _decode_text(value).rstrip("\x00")

def _decode_utf16(raw: bytes, order: str) -> str:
    """对应 Go ``metadataDecodeUTF16``：解 UTF-16，认字节序标记（无标记时用 TIFF 头）。

    落单的代理项与 Go 的 ``utf16.Decode`` 一样变成 U+FFFD：``errors="replace"`` 的语义
    与 Go 一致（补一个替换符，再照常处理下一个码元）。"""
    if len(raw) >= 2:
        if raw[0] == 0xFF and raw[1] == 0xFE:
            order, raw = "little", raw[2:]
        elif raw[0] == 0xFE and raw[1] == 0xFF:
            order, raw = "big", raw[2:]
    if len(raw) % 2 != 0:
        raw = raw[:-1]
    codec = "utf-16-le" if order == "little" else "utf-16-be"
    return raw.decode(codec, "replace").rstrip("\x00")

# --- XMP ---

def _xmp_packet(packet: bytes, text: MetadataText) -> None:
    """对应 Go ``metadataXMPPacket``：从 XMP 数据包里读出元数据载体。

    XMP 就是普通 XML：先用段长定界，再截到一个文本块上限（超过就告警），最后才交给
    ``ElementTree`` 的拉取式解析器；畸形 XML 降级为「用已经吐出来的部分」。"""
    body = _xmp_body(packet)
    if not body or b"<" not in body:
        return
    if len(body) > MaxMetadataTextBytes:
        text.warn("XMP 数据包超过 1 MiB 上限，只解析前 1 MiB")
        body = body[:MaxMetadataTextBytes]
    if body.count(b"<") > XMP_MAX_ELEMENTS:
        # Go 的 xml.Decoder 是流式的，内存只与嵌套深度有关；ElementTree 会给每个元素
        # 造对象，所以按元素数量设一道闸门，避免一个全是标签的 1 MiB 数据包把内存
        # 放大上百倍。真实生成参数的数据包远低于这个量级。
        text.warn("XMP 数据包的元素数量超过上限，已跳过解析")
        return
    root = _parse_xmp_tree(body)
    if root is not None:
        _walk_xmp_tree(root, text)

def _parse_xmp_tree(body: bytes) -> ET.Element | None:
    """容忍畸形 XML 地拿回（可能是局部的）文档树，拿不到就返回 None。``feed`` 把解析
    错误排进事件队列，``close`` 则直接抛（尾部未闭合）；两种情况下都只用已经产出的部分，
    对应 Go ``Token()`` 出错前的语义。"""
    parser = ET.XMLPullParser(events=("start", "end"))
    try:
        parser.feed(body)
    except ET.ParseError:
        pass  # 错误已入队，交给 read_events() 抛出
    except Exception:  # noqa: BLE001 - expat 对极端输入还可能报别的错，一律降级
        return None
    try:
        parser.close()
    except Exception:  # noqa: BLE001 - 尾部未闭合：已产出的部分照用
        pass
    try:
        for event, element in parser.read_events():
            if event == "start":
                return element
    except Exception:  # noqa: BLE001 - 队列里的解析错误到此为止，前面的部分保留
        return None
    return None

def _iter_xmp_tokens(root: ET.Element):
    """按文档顺序产出 ``("start", el)`` / ``("text", str)`` / ``("end", el)``。等价 Go
    ``xml.Decoder.Token()`` 的事件流；用显式栈而不是递归，再深也不 RecursionError。"""
    stack: list[tuple[str, object]] = [("node", root)]
    while stack:
        kind, payload = stack.pop()
        if kind != "node":
            yield kind, payload
            continue
        yield "start", payload
        stack.append(("end", payload))
        pending: list[tuple[str, object]] = []
        if payload.text:
            pending.append(("text", payload.text))
        for child in list(payload):
            pending.append(("node", child))
            if child.tail:
                pending.append(("text", child.tail))
        stack.extend(reversed(pending))

def _local_name(name: str) -> str:
    """把 ElementTree 标签/属性名折成局部名（对应 Go 的 ``Name.Local``）。"""
    return name.partition("}")[2] if name.startswith("{") else name

def _walk_xmp_tree(root: ET.Element, text: MetadataText) -> None:
    """对应 Go ``metadataXMPPacket`` 的 token 循环。

    ``stack`` 记录每个打开元素处生效的载体关键字以及该元素自身是否引入它，这样包在
    ``rdf:Alt``/``rdf:li`` 里的值也能回到载体；``skipped`` 统计越过嵌套上限的元素，它们
    被消费但不被跟踪（Go 的深度上限同样只限制跟踪，不限制文本累积）。"""
    stack: list[tuple[str, bool]] = []
    skipped = 0
    key = ""
    parts: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal key, parts, length
        if not key or not parts:
            return
        text.add(key, "".join(parts).strip())
        key, parts, length = "", [], 0

    for kind, payload in _iter_xmp_tokens(root):
        if kind == "start":
            for name, value in payload.attrib.items():
                attribute_key = _xmp_key(_local_name(name))
                if attribute_key:
                    text.add(attribute_key, value)
            if skipped > 0:
                skipped += 1
                continue
            if len(stack) >= XMP_MAX_DEPTH:
                skipped = 1
                continue
            own = _xmp_key(_local_name(payload.tag))
            inherited = own or (stack[-1][0] if stack else "")
            if own:
                key, parts, length = own, [], 0
            stack.append((inherited, own != ""))
        elif kind == "text":
            if stack and stack[-1][0] and length <= MaxMetadataTextBytes:
                parts.append(payload)
                length += len(payload)
        else:
            if skipped > 0:
                skipped -= 1
                continue
            if stack and stack.pop()[1]:
                flush()
    flush()

def _xmp_body(packet: bytes) -> bytes:
    """对应 Go ``metadataXMPBody``：去掉 xpacket 外壳与前导 BOM。只剥尾部的填充处理
    指令：没有收尾指令的数据包不能因为开头那条就丢掉内容。"""
    body = packet.strip()
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    if body.startswith(b"<?xpacket"):
        end = body.find(b"?>")
        if end >= 0:
            body = body[end + 2:].strip()
    end = body.rfind(b"<?xpacket")
    if end > 0:
        body = body[:end].strip()
    return body

# XMP 元素/属性名到「PNG 里同义的文本块关键字」的映射（Go metadataXMPKey）。
_XMP_KEYS = {
    "parameters": "parameters", "sd-metadata": "sd-metadata", "sdmetadata": "sd-metadata",
    "invokeai_metadata": "invokeai_metadata", "invokeaimetadata": "invokeai_metadata",
    "prompt": "prompt", "negative_prompt": "negative_prompt",
    "negativeprompt": "negative_prompt", "workflow": "workflow", "comment": "Comment",
    "description": "Description", "software": "Software", "usercomment": "UserComment",
    "user_comment": "UserComment",
}

def _xmp_key(local: str) -> str:
    """对应 Go ``metadataXMPKey``：把 XMP 元素/属性名折到 PNG 里同义的文本块关键字。"""
    return _XMP_KEYS.get(local.strip().lower(), "")

# --- 方言判定与对外入口 ---

def _promote_user_comment(text: MetadataText) -> None:
    """对应 Go ``metadataPromoteUserComment``：按形状给 EXIF UserComment 归类——同一个
    通道里可能是 A1111 参数、NovelAI JSON 或 InvokeAI JSON，取决于写入方。"""
    comment, ok = text.get("UserComment")
    if not ok or not comment:
        return
    object = decode_json_object(comment)
    if object is None:
        if parse_a1111(comment)[3] and not text.has("parameters"):
            text.add("parameters", comment)
        return
    if object.get("sd-metadata") is not None and not text.has("sd-metadata"):
        text.add("sd-metadata", comment)
    elif object.get("invokeai_metadata") is not None and not text.has("invokeai_metadata"):
        text.add("invokeai_metadata", comment)
    elif not text.has("Comment"):
        text.add("Comment", comment)

def resolve_dialects(meta: Metadata, text: MetadataText) -> None:
    """对应 Go ``metadataResolveDialects``：定方言并填提示词字段，永不失败；认不出来的
    文本留在 ``meta.text``，方言报 ``unknown``。"""
    _promote_user_comment(text)
    # Go 的 switch 是短路求值：第一个认下来的方言说了算。
    for apply in (apply_a1111, apply_novelai, apply_comfyui, apply_invokeai):
        if apply(meta, text):
            break
    apply_comfy_workflow_fallback(meta, text)
    if meta.source == "":
        meta.source = MetadataSourceUnknown

def extract_metadata(data: bytes) -> tuple[Metadata, bool]:
    """对应 Go ``ExtractMetadata``：从原始图片字节里只读恢复生成参数。

    容器未知或没有文本元数据时 ``found`` 为假；它从不失败，可恢复的问题都通过
    ``Metadata.warnings`` 报告，已知容器下 ``parameters``/``text`` 总是可用（非 None）。
    ``found`` 报告的是「容器已知且带了文本块」：被识别但因解不出来或越过上限而被丢掉的块
    同样算数，原因写在 ``warnings`` 里。"""
    meta = Metadata()
    if not data:
        return meta, False
    text = MetadataText()
    if data.startswith(PNG_SIGNATURE):
        meta.container = MetadataContainerPNG
        _scan_png(data, text)
    elif len(data) >= 2 and data[0] == 0xFF and data[1] == 0xD8:
        meta.container = MetadataContainerJPEG
        _scan_jpeg(data, text)
    elif len(data) >= 12 and data[0:4] == RIFF_MAGIC and data[8:12] == WEBP_MAGIC:
        meta.container = MetadataContainerWebP
        _scan_webp(data, text)
    else:
        return meta, False
    if not text.seen:
        meta.warnings = list(text.warnings)
        return meta, False
    for keyword in text.order:
        meta.text[keyword] = text.values[keyword]
    resolve_dialects(meta, text)
    meta.warnings = list(text.warnings)
    return meta, True

def parse_metadata(data: bytes) -> Metadata | None:
    """``extract_metadata`` 的便利包装：什么都没找到时返回 None。"""
    meta, found = extract_metadata(data)
    return meta if found else None

def parse_metadata_with_origin(data: bytes, origin: str) -> ImageMetadata | None:
    """从字节里解析并标注来源（``archive`` / ``embedded``），未命中返回 None。

    对应 Go ``Service.imageMetadataFrom`` 的 metadata 部分；画师串提取
    （``ExtractFromGraph`` → ``Extraction``）由 ``atri_x/artist/service.py`` 负责，这里返回
    的 ``ImageMetadata.extraction`` 保持默认值。"""
    meta = parse_metadata(data)
    if meta is None:
        return None
    return ImageMetadata(metadata=meta, found=True, origin=origin)

# 不设 __all__：本模块的公开面就是「容器层入口 + 从 metadata_formats 引入的方言层名字」，
# 等同于 Go 单文件 metadata.go 的对外面。
