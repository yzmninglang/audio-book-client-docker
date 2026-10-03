"""编排层(M4)—— 唯一同时 import `tts/` 与 `audio/` 的地方(硬规则 R4)。

对应设计:`doc/synthesis/design.md`、`doc/architecture.md` §5.2。

    pipeline.py        单条片段:绑定 → VoiceRef → TTS → 归一化(R7) → 原子写 → 记账
    chapter_runner.py  单章状态机:来源检查 → 能力检查 → 续传对账 → 逐条 → 拼接 → 清理
    queue.py           ★ 进程级全局单例串行队列 + 暂停/停止
    events.py          进度事件总线(供 api/ 的 SSE 消费)

边界:`synthesis/` 知道 HTTP 的成败,也知道文件怎么写,但**不知道 HTTP 报文长什么样**,
也不知道 FFmpeg/编码细节 —— 那些分别属于 `tts/` 与 `audio/`。
"""

from __future__ import annotations

from .chapter_runner import ChapterResult, ChapterRunner, planned_indices
from .events import (
    CHAPTER_CONCATENATED,
    CHAPTER_FAILED,
    CHAPTER_STARTED,
    FRAGMENT_DONE,
    FRAGMENT_FAILED,
    JOB_STATE,
    LOG,
    Event,
    EventBus,
    Subscription,
)
from .pipeline import (
    ChapterContext,
    FragmentResult,
    ReferenceCache,
    RunControl,
    check_capabilities,
    provider_contract_version,
    required_emotion_kinds,
    synthesize_one,
)
from .queue import SynthesisQueue, get_queue

__all__ = [
    "CHAPTER_CONCATENATED",
    "CHAPTER_FAILED",
    "CHAPTER_STARTED",
    "FRAGMENT_DONE",
    "FRAGMENT_FAILED",
    "JOB_STATE",
    "LOG",
    "ChapterContext",
    "ChapterResult",
    "ChapterRunner",
    "Event",
    "EventBus",
    "FragmentResult",
    "ReferenceCache",
    "RunControl",
    "Subscription",
    "SynthesisQueue",
    "check_capabilities",
    "get_queue",
    "planned_indices",
    "provider_contract_version",
    "required_emotion_kinds",
    "synthesize_one",
]
