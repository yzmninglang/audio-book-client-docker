"""统一异常体系。

编排层只认 `RetryableError` / `FatalError` 两个分类,不认 HTTP 状态码 ——
把「协议错误码」翻译成「领域语义」是 `tts/` 适配器的职责。
"""

from __future__ import annotations


class AudiobookError(Exception):
    """所有领域异常的基类。

    `kind` 用于 API 层统一序列化(见 doc/api/design.md §5)。
    """

    kind: str = "error"

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"kind": self.kind, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class RetryableError(AudiobookError):
    """可重试:网络抖动、超时、后端 5xx。由 tts/retry.py 退避重试。"""

    kind = "retryable"


class FatalError(AudiobookError):
    """不可重试:参数错误、数据损坏。重试是纯粹的浪费。"""

    kind = "fatal"


class InvalidInputError(FatalError):
    """输入不合法:后端 400,或本地的用户传参错误。

    **继承 `FatalError`** —— 重试参数错误是纯粹的浪费(见 doc/tts/design.md §4)。
    单独成类只是为了在 API 层能映射成 400 而非 500(`kind="invalid"`)。

    `FatalError` 表达「重试语义」,`InvalidInputError` 表达「错误归因」,两者正交而非矛盾。
    """

    kind = "invalid"


class NotFoundError(AudiobookError):
    kind = "not_found"


class ConfigError(AudiobookError):
    kind = "config"


class BindingError(ConfigError):
    """角色未绑定音色,或绑定指向的音色已不存在。

    必须在**章节开跑前**抛出,而不是跑到一半才失败
    (见 doc/synthesis/design.md §8)。
    """

    kind = "binding"


class ManifestError(FatalError):
    """manifest 读写失败。

    这是**严重**错误:manifest 是断点续传的唯一依据,写不进去就不能继续合成,
    否则会产生「以为完成了、其实没有」的静默损坏。
    """

    kind = "manifest"
