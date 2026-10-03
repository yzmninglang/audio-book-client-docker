"""base64 编解码 + 体积估算(供流量预警)。

参考音频**每个请求都要重传**,且以 base64 塞进 JSON,故上行体积是核心成本
(见 `doc/findings.md` §6)。base64 使体积膨胀约 4/3,本模块给出编码结果与预估字节数。
"""

from __future__ import annotations

import base64

from ..errors import InvalidInputError


def estimate_upload_bytes(data: bytes) -> int:
    """估算一段字节经 base64 编码后的上行字节数(不含 JSON 外壳)。

    base64 输出长度为 `4 * ceil(n / 3)`。
    """
    return 4 * ((len(data) + 2) // 3)


def b64_and_estimate(data: bytes) -> tuple[str, int]:
    """把音频字节编码为 base64 文本,并返回预估上行字节数。

    Returns:
        `(base64_text, upload_bytes)`,其中 `upload_bytes` 为 base64 文本长度
        (ASCII,字符数即字节数)。
    """
    encoded = base64.b64encode(data).decode("ascii")
    return encoded, len(encoded)


def b64_decode(text: str | bytes) -> bytes:
    """把 base64 文本解码回字节。

    Raises:
        InvalidInputError: base64 文本非法。
    """
    if isinstance(text, str):
        raw = text.encode("ascii", errors="strict")
    else:
        raw = text
    try:
        return base64.b64decode(raw, validate=True)
    except (ValueError, UnicodeError) as exc:
        raise InvalidInputError("非法的 base64 数据", detail={"reason": "bad_base64"}) from exc
