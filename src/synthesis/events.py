"""进度事件总线(供 `api/` 的 SSE 消费)。

对应设计:`doc/synthesis/design.md` §5。事件类型与载荷字段与前端
`src/web/src/api/types.ts` 的 `ProgressEvent` 严格对齐(前端零代码共享,只走 SSE)。

事件类型(固定 7 种):

    chapter_started      章节名、总条数、其中需生成条数
    fragment_done        章节名、索引、耗时、累计进度
    fragment_failed      章节名、索引、错误分类、重试次数
    chapter_concatenated 章节名、成品路径、时长
    chapter_failed       章节名、失败清单
    job_state            running / paused / stopped / idle
    log                  分级日志(供 UI 日志面板)

设计原则:

    * **非阻塞**:合成线程发布事件绝不因订阅者慢而卡住(满则丢弃最旧事件)。
    * 事件是**不可变快照**,`to_dict()` 直接给出 SSE 可序列化的扁平字典
      `{"type": ..., **payload, "ts": ...}`。
"""

from __future__ import annotations

import queue as _queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..models import JobState

# --------------------------------------------------------------------------- #
# 事件类型常量
# --------------------------------------------------------------------------- #

CHAPTER_STARTED = "chapter_started"
FRAGMENT_DONE = "fragment_done"
FRAGMENT_FAILED = "fragment_failed"
CHAPTER_CONCATENATED = "chapter_concatenated"
CHAPTER_FAILED = "chapter_failed"
JOB_STATE = "job_state"
LOG = "log"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        CHAPTER_STARTED,
        FRAGMENT_DONE,
        FRAGMENT_FAILED,
        CHAPTER_CONCATENATED,
        CHAPTER_FAILED,
        JOB_STATE,
        LOG,
    }
)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    """一条进度事件。`data` 为扁平载荷(字段见模块 docstring 与前端 types.ts)。"""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """SSE 序列化形态:`{"type": ..., **data, "ts": ...}`。"""
        out: dict[str, Any] = {"type": self.type, "ts": self.ts}
        out.update(self.data)
        return out


class Subscription:
    """一个消费者(SSE 连接)的订阅句柄:线程安全的有界队列。"""

    def __init__(self, maxsize: int = 2048) -> None:
        self._queue: _queue.Queue[Event] = _queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    def put(self, event: Event) -> None:
        """非阻塞投递;队列满时丢弃最旧事件(绝不阻塞合成线程)。"""
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except _queue.Full:
            try:
                self._queue.get_nowait()
            except _queue.Empty:  # pragma: no cover - 竞态兜底
                pass
            self.dropped += 1
            try:
                self._queue.put_nowait(event)
            except _queue.Full:  # pragma: no cover - 竞态兜底
                pass

    def get(self, timeout: float | None = None) -> Event | None:
        """取一条事件;超时返回 `None`(便于 SSE 循环做心跳)。"""
        try:
            return self._queue.get(timeout=timeout)
        except _queue.Empty:
            return None

    def drain(self) -> list[Event]:
        """取走当前所有积压事件。"""
        out: list[Event] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except _queue.Empty:
                return out

    def __iter__(self) -> Iterator[Event]:
        """迭代直到 `close()`(阻塞等待,不返回 `None`)。"""
        while not self._closed:
            event = self.get(timeout=1.0)
            if event is not None:
                yield event

    def close(self) -> None:
        self._closed = True


class EventBus:
    """进程内事件总线:多订阅者广播,发布非阻塞。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[Subscription] = set()
        self._listeners: list[Any] = []

    # -- 进程内监听(队列自用,同步调用、异常吞掉) ----------------------- #

    def add_listener(self, callback: Any) -> None:
        """注册同步监听器(与订阅者不同:不排队、不丢弃、异常被吞)。"""
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Any) -> None:
        with self._lock:
            self._listeners = [cb for cb in self._listeners if cb is not callback]

    # -- 订阅 ------------------------------------------------------------- #

    def subscribe(self, *, maxsize: int = 2048) -> Subscription:
        sub = Subscription(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(sub)
        sub.close()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- 发布 ------------------------------------------------------------- #

    def publish(self, event: Event) -> Event:
        with self._lock:
            subs = list(self._subscribers)
            listeners = list(self._listeners)
        for sub in subs:
            sub.put(event)
        for callback in listeners:
            try:
                callback(event)
            except Exception:  # pragma: no cover - 监听器异常不影响发布
                pass
        return event

    def emit(self, event_type: str, **payload: Any) -> Event:
        """按类型发布事件。`ts` 可显式传入,否则自动生成。"""
        return self.publish(Event(type=event_type, data=payload))

    # -- 便捷发布器(载荷字段见前端 types.ts) ---------------------------- #

    def chapter_started(
        self, chapter: str, *, total: int, remaining: int
    ) -> Event:
        return self.emit(CHAPTER_STARTED, chapter=chapter, total=total, remaining=remaining)

    def fragment_done(
        self, chapter: str, index: int, *, elapsed_ms: int, done: int
    ) -> Event:
        return self.emit(
            FRAGMENT_DONE, chapter=chapter, index=index, elapsed_ms=elapsed_ms, done=done
        )

    def fragment_failed(
        self, chapter: str, index: int, *, error_kind: str, error: str, attempts: int
    ) -> Event:
        return self.emit(
            FRAGMENT_FAILED,
            chapter=chapter,
            index=index,
            error_kind=error_kind,
            error=error,
            attempts=attempts,
        )

    def chapter_concatenated(
        self, chapter: str, *, output: str, duration_ms: int
    ) -> Event:
        return self.emit(
            CHAPTER_CONCATENATED, chapter=chapter, output=output, duration_ms=duration_ms
        )

    def chapter_failed(
        self,
        chapter: str,
        *,
        message: str,
        error_kind: str = "fatal",
        failed: list[int] | None = None,
    ) -> Event:
        return self.emit(
            CHAPTER_FAILED,
            chapter=chapter,
            message=message,
            error_kind=error_kind,
            failed=list(failed or []),
        )

    def job_state(self, state: JobState, **extra: Any) -> Event:
        return self.emit(JOB_STATE, state=state.value, **extra)

    def log(
        self,
        message: str,
        *,
        level: str = "info",
        chapter: str | None = None,
        index: int | None = None,
    ) -> Event:
        return self.emit(LOG, level=level, message=message, chapter=chapter, index=index)


__all__ = [
    "CHAPTER_CONCATENATED",
    "CHAPTER_FAILED",
    "CHAPTER_STARTED",
    "EVENT_TYPES",
    "FRAGMENT_DONE",
    "FRAGMENT_FAILED",
    "JOB_STATE",
    "LOG",
    "Event",
    "EventBus",
    "Subscription",
]
