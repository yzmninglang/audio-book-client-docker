"""HTTP 层专用的异常(协议语义,不含业务逻辑)。

`kind` 仍复用 `src/errors.py` 的分类,便于统一序列化。
"""

from __future__ import annotations

from ..errors import AudiobookError, InvalidInputError


class AlreadyExistsError(InvalidInputError):
    """资源已存在,不静默覆盖 → HTTP **409**,`kind` 仍为 `invalid`(design §8.9)。

    单独成类只为在错误处理器里把状态码钉成 409(否则 `invalid` 默认映射 400)。
    """

    kind = "invalid"


class ConflictError(AudiobookError):
    """操作与进行中的任务冲突 → HTTP **409**(design §8.10)。

    例:删除正在合成的工程/章节 —— 删到一半会产生孤儿片段,必须拒绝。
    """

    kind = "conflict"
