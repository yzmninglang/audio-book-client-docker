"""分层重试策略(设计见 doc/tts/design.md §4)。

核心原则:**不能一刀切**。400 类错误重试是纯粹的浪费;而后端无并发保护,
5xx 必须**退避**重试,不可立即重发。

分层归属:

    | 情况                                   | 分类             | 退避        |
    |----------------------------------------|------------------|-------------|
    | 连接失败 / 超时 / 网络异常             | RetryableError   | 1s→2s→4s…   |
    | HTTP 5xx(除模型未加载)               | RetryableError   | 1s→2s→4s…   |
    | HTTP 4xx(含 400)                     | FatalError       | 不重试      |
    | 500 + 「模型未成功加载」               | FatalError       | 不重试      |
    | 响应解析 / 音频解码失败                | FatalError       | 不重试      |

编排层只认 `RetryableError` / `FatalError`(见 errors.py),不认 HTTP 状态码。
为便于 `probe()` 精确区分失败种类,分类异常在 `detail` 里携带 `ErrorDetail`。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from ..errors import AudiobookError, FatalError, InvalidInputError, RetryableError

T = TypeVar("T")

# 退避参数:1s → 2s → 4s …,上限 30s
BASE_DELAY_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
MAX_DELAY_SECONDS = 30.0
DEFAULT_RETRIES = 3

# 后端在模型未加载时的固定文案(见 ui-api.py:131)
MODEL_NOT_LOADED_MARKER = "模型未成功加载"

# ErrorDetail.reason 取值
REASON_NETWORK = "network"
REASON_TIMEOUT = "timeout"
REASON_SERVER = "server"
REASON_BAD_REQUEST = "bad_request"
REASON_MODEL_NOT_LOADED = "model_not_loaded"
REASON_DECODE = "decode"


@dataclass
class ErrorDetail:
    """挂在异常 `detail` 上的结构化信息,供 probe() 精确分类。"""

    reason: str
    http_status: int | None = None
    message: str = ""


def classify_http_error(status: int, message: str) -> AudiobookError:
    """把后端的 HTTP 错误状态翻译成领域异常。"""
    if status == 400:
        # 归因是「输入不合法」→ InvalidInputError(继承 FatalError,仍不可重试,
        # 但 API 层可据此映射成 400 而非 500)。
        return InvalidInputError(
            f"后端拒绝请求(HTTP 400): {message}",
            detail=ErrorDetail(REASON_BAD_REQUEST, status, message),
        )
    if status >= 500 and MODEL_NOT_LOADED_MARKER in message:
        # 后端已启动但模型没加载好 —— 重试无用,立即失败并提示用户。
        return FatalError(
            f"后端模型未成功加载: {message}",
            detail=ErrorDetail(REASON_MODEL_NOT_LOADED, status, message),
        )
    if status >= 500:
        return RetryableError(
            f"后端内部错误(HTTP {status}): {message}",
            detail=ErrorDetail(REASON_SERVER, status, message),
        )
    # 其余 4xx(401/403/404…)同样归因为请求错误,不可重试。
    return InvalidInputError(
        f"后端返回错误(HTTP {status}): {message}",
        detail=ErrorDetail(REASON_BAD_REQUEST, status, message),
    )


def classify_transport_error(exc: BaseException) -> RetryableError:
    """把 httpx / 底层网络异常翻译成可重试异常。"""
    if isinstance(exc, httpx.TimeoutException):
        reason = REASON_TIMEOUT
    else:
        reason = REASON_NETWORK
    return RetryableError(
        f"连接 TTS 后端失败: {exc}",
        detail=ErrorDetail(reason, None, str(exc)),
    )


def backoff_delay(
    retry_number: int,
    *,
    base: float = BASE_DELAY_SECONDS,
    factor: float = BACKOFF_FACTOR,
    max_delay: float = MAX_DELAY_SECONDS,
) -> float:
    """第 `retry_number`(1-based)次重试前的退避时长。"""
    return min(base * (factor ** (retry_number - 1)), max_delay)


def call_with_retry(
    fn: Callable[[], T],
    *,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, RetryableError], None] | None = None,
) -> T:
    """调用 `fn`,对 `RetryableError` 做指数退避重试。

    `retries` 是**首次失败后的重试次数**(总尝试次数 = retries + 1)。
    `sleep` 可注入以便离线测试(不真的等待)。
    `FatalError` 等其他异常立即抛出,不重试。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except RetryableError as exc:
            if attempt >= retries:
                raise
            attempt += 1
            delay = backoff_delay(attempt)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)
