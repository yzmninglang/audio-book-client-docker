"""合成任务(`/api/jobs`)：开始 / 状态 / **SSE 事件流** / 暂停 / 停止。

SSE(design §4):
    * 订阅全局队列的事件总线(多客户端各自独立广播)
    * 定期发**心跳注释行**保活(反代常掐断空闲连接)
    * 客户端断线在 `finally` 里 `unsubscribe`,不泄漏订阅
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ...project import Project, Workspace
from ...synthesis import Event, SynthesisQueue
from ..deps import QueueDep, SettingsDep, WorkspaceDep
from ..schemas import JobStatusOut, StartJobRequest

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

DEFAULT_HEARTBEAT_SECONDS = 15.0
"""心跳间隔:反代通常 30~60s 掐空闲连接,15s 足够安全。"""


def _open_project(name: str, workspace: Workspace) -> Project:
    root = workspace.open(name)
    return Project.open(root, book=name)


@router.post("", response_model=JobStatusOut)
def start_job(
    body: StartJobRequest,
    workspace: WorkspaceDep,
    queue: QueueDep,
    settings: SettingsDep,
) -> JobStatusOut:
    """入队即返回(不阻塞等待整章完成);进度走 SSE(§5)。"""
    project = _open_project(body.project, workspace)
    queue.enqueue_chapters(
        project,
        body.chapters,
        redo=body.redo,
        settings=settings,
    )
    return JobStatusOut(**queue.snapshot())


@router.get("/state", response_model=JobStatusOut)
def job_state(queue: QueueDep) -> JobStatusOut:
    """当前任务状态(SSE 断线后的兜底,§8.6)。"""
    return JobStatusOut(**queue.snapshot())


@router.post("/pause", response_model=JobStatusOut)
def pause_job(queue: QueueDep) -> JobStatusOut:
    queue.pause()
    return JobStatusOut(**queue.snapshot())


@router.post("/stop", response_model=JobStatusOut)
def stop_job(queue: QueueDep) -> JobStatusOut:
    queue.stop()
    return JobStatusOut(**queue.snapshot())


def _format_event(event: Event) -> str:
    """具名事件帧(§8.7):`event: <type>\\ndata: <json>\\n\\n`。"""
    payload = json.dumps(event.to_dict(), ensure_ascii=False)
    return f"event: {event.type}\ndata: {payload}\n\n"


async def open_event_stream(
    queue: SynthesisQueue,
    heartbeat: float,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """SSE 帧生成器:广播事件 + 定期心跳注释行;收尾时 `unsubscribe`。

    协程生成器(而非同步生成器):Starlette 在客户端断线时取消的是**异步任务**,
    取消会落在 `await` 上并触发下面的 `finally`;同步生成器跑在线程池里,
    被取消不会终止,订阅会泄漏到 GC 为止。

    独立成函数便于**离线单测**在有限帧内验证「事件 + 心跳 + 断线清理」——
    真实 HTTP 下这是无限流,`TestClient` 缓冲后无法收尾。
    """
    bus = queue.bus
    poll = min(1.0, heartbeat) if heartbeat > 0 else 0.05
    subscription = bus.subscribe()
    try:
        yield ": connected\n\n"
        last = time.monotonic()
        while True:
            event = subscription.get(timeout=0)  # 非阻塞取队列
            if event is not None:
                yield _format_event(event)
                last = time.monotonic()
                continue
            if is_disconnected is not None and await is_disconnected():
                break
            await anyio.sleep(poll)
            if time.monotonic() - last >= heartbeat:
                yield ": keep-alive\n\n"
                last = time.monotonic()
    finally:
        bus.unsubscribe(subscription)


@router.get("/events")
def job_events(request: Request, queue: QueueDep) -> StreamingResponse:
    """SSE 进度事件流(`text/event-stream`)。"""
    heartbeat = float(
        getattr(request.app.state, "sse_heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)
    )
    return StreamingResponse(
        open_event_stream(queue, heartbeat, request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 提示反代关闭缓冲(design §4)
        },
    )
