"""★ 进程级全局单例串行队列 + 暂停/停止(见 `doc/synthesis/design.md` §4/§6)。

**硬约束**:进程级**全局单例**,不是每会话一个。NAS 上多设备/多人共用页面,而后端
`ui-api.py` 无锁、无并发保护 —— 并发请求会导致 GPU OOM 或崩溃。

    | 并发度   | 默认 1;配置项(`Settings.concurrency`)预留。M4 强制串行。       |
    | 任务粒度 | 「章节」为调度单元,「片段」为执行单元。                        |
    | 持久性   | 不做持久化队列 —— 状态落在 manifest,重启后重新扫描即可。        |
    | 多用户   | 队列全局共享,UI 显示「正在运行其他任务」。                     |

暂停 = 当前片段跑完后停(不中断进行中的 HTTP);停止 = 同暂停但清空未开始任务。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, ClassVar

from ..config import Settings
from ..errors import AudiobookError, ManifestError, NotFoundError
from ..models import JobState
from ..project import AUDIO_DIRNAME, STALE_DIRNAME, Project, ReconcileReport
from ..tts import registry
from ..voice import VoiceLibrary
from . import events
from .chapter_runner import ChapterRunner, planned_indices
from .events import Event, EventBus
from .pipeline import ChapterContext, RunControl, provider_contract_version

DEFAULT_ETA_SECONDS = 3.0
"""每条片段的默认耗时基准(实测 1.8~4.3s,见 doc/findings.md §3)。"""


@dataclass
class _Task:
    """一个调度单元:一章。"""

    project: Project
    name: str
    provider: Any
    voices: VoiceLibrary
    voice_names: set[str]
    contract_version: int
    params: dict[str, Any] | None
    policy: str
    retries: int
    report: ReconcileReport


def _force_redo(project: Project, chapter_name: str) -> None:
    """显式作废某章的旧成品与片段(见 design §6「重做章节」)。

    旧成品移到 `audio/.stale/`(保留,不删 —— project/design.md §7 #4)。
    """
    manifest = project.manifest
    assert manifest is not None

    audio = manifest.chapter_audio_path(chapter_name)
    if audio.is_file():
        stale_dir = manifest.root / AUDIO_DIRNAME / STALE_DIRNAME
        stale_dir.mkdir(parents=True, exist_ok=True)
        dst = stale_dir / audio.name
        if dst.exists():
            dst = stale_dir / f"{audio.stem}.{int(time.time())}{audio.suffix}"
        os.replace(audio, dst)

    fragment_dir = manifest.fragment_dir(chapter_name)
    if fragment_dir.is_dir():
        shutil.rmtree(fragment_dir, ignore_errors=True)

    record = manifest.chapter(chapter_name)
    if record is not None:
        record.fragments.clear()
        record.output = None
    manifest.save()


class SynthesisQueue:
    """全局串行合成队列(进程级单例;测试可自行实例化)。"""

    _global: ClassVar[SynthesisQueue | None] = None

    def __init__(self, *, concurrency: int = 1, bus: EventBus | None = None) -> None:
        self.concurrency = max(1, int(concurrency))
        self._bus = bus or EventBus()
        self._lock = threading.RLock()
        self._tasks: deque[_Task] = deque()
        self._queued: set[tuple[str, str]] = set()
        self._control = RunControl()
        self._worker: threading.Thread | None = None
        self._runner = ChapterRunner()

        # 状态快照(供 /api/jobs/state 断线兜底)
        self._state = JobState.IDLE
        self._project_name: str | None = None
        self._current_chapter: str | None = None
        self._total = 0
        self._done = 0
        self._durations: list[float] = []
        self._chapters: dict[str, dict[str, Any]] = {}
        self._logs: deque[dict[str, Any]] = deque(maxlen=200)

        self._bus.add_listener(self._on_event)

    # -- 全局单例 --------------------------------------------------------- #

    @classmethod
    def instance(cls, **kwargs: Any) -> SynthesisQueue:
        """取进程级全局单例(NAS 上多设备共享)。"""
        if cls._global is None:
            cls._global = cls(**kwargs)
        return cls._global

    @classmethod
    def _reset_global(cls) -> None:  # pragma: no cover - 测试辅助
        cls._global = None

    # -- 对外 API --------------------------------------------------------- #

    def enqueue_chapters(
        self,
        project: Project,
        chapters: list[str] | None = None,
        *,
        redo: bool = False,
        provider: Any = None,
        voices: VoiceLibrary | None = None,
        params: dict[str, Any] | None = None,
        policy: str = "warn",
        retries: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        """把章节入队(串行执行)。`chapters=None` 表示全部章节。

        依赖(provider/voices)可显式注入(测试);否则从 `Settings` 构建。
        同一(工程,章节)在队列中只保留一份,重复入队幂等;入队会清除暂停态。
        """
        if provider is None:
            settings = settings or Settings.load()
            config = settings.provider()
            provider = registry.create(settings.active_provider, config)
            if retries is None:
                retries = config.retries
        if voices is None:
            settings = settings or Settings.load()
            voices = VoiceLibrary(settings.voices_root)
        if retries is None:
            retries = 3

        voice_names = {entry.name for entry in voices.list()}
        names = list(chapters) if chapters else list(project.chapter_names)
        for name in names:
            if project.chapter(name) is None:
                raise NotFoundError(f"章节不存在: {name}")

        if redo:
            for name in names:
                _force_redo(project, name)

        contract_version = provider_contract_version(provider)
        current_bindings: dict[str, str] = {}
        if project.bindings is not None:
            current_bindings = {r: b.voice for r, b in project.bindings.all().items()}
        report = project.reconcile(
            provider=provider.id,
            provider_contract_version=contract_version,
            voice_bindings=current_bindings,
            persist=True,
        )

        with self._lock:
            fresh = not self._tasks
            if fresh:
                self._reset_status_locked()
            self._project_name = project.name
            for name in names:
                key = (str(project.root), name)
                if key in self._queued:
                    continue
                self._tasks.append(
                    _Task(
                        project=project,
                        name=name,
                        provider=provider,
                        voices=voices,
                        voice_names=voice_names,
                        contract_version=contract_version,
                        params=params,
                        policy=policy,
                        retries=retries,
                        report=report,
                    )
                )
                self._queued.add(key)

                chapter = project.chapter(name)
                planned = planned_indices(report.action_for(name), chapter.sentence_count)
                self._total += chapter.sentence_count
                self._done += chapter.sentence_count - len(planned)

            self._control.resume()
            self._ensure_worker_locked()
            self._set_state_locked(JobState.RUNNING)

    def pause(self) -> None:
        """暂停:当前片段跑完后停(不中断进行中的 HTTP)。"""
        with self._lock:
            if self._state is not JobState.RUNNING:
                return
            self._control.request_pause()
            self._set_state_locked(JobState.PAUSED)

    def stop(self) -> None:
        """停止:同暂停,并清空队列中未开始的任务。"""
        with self._lock:
            self._control.request_stop()
            self._tasks.clear()
            self._queued.clear()
            self._set_state_locked(JobState.STOPPED)

    def resume(self) -> None:
        """从暂停/停止恢复队列中尚未开始的任务。"""
        with self._lock:
            self._control.resume()
            if self._tasks:
                self._ensure_worker_locked()
                self._set_state_locked(JobState.RUNNING)

    def state(self) -> JobState:
        with self._lock:
            return self._state

    def snapshot(self) -> dict[str, Any]:
        """`/api/jobs/state` 的载荷(与前端 `JobStatus` 对齐)。"""
        with self._lock:
            average = (
                sum(self._durations) / len(self._durations)
                if self._durations
                else DEFAULT_ETA_SECONDS
            )
            remaining = max(0, self._total - self._done)
            eta = (
                int(remaining * average)
                if self._state in (JobState.RUNNING, JobState.PAUSED)
                else None
            )
            return {
                "state": self._state.value,
                "project": self._project_name,
                "current_chapter": self._current_chapter,
                "done_fragments": self._done,
                "total_fragments": self._total,
                "chapters": [dict(c) for c in self._chapters.values()],
                "eta_seconds": eta,
                "recent_logs": list(self._logs),
            }

    @property
    def bus(self) -> EventBus:
        return self._bus

    def wait_idle(self, timeout: float | None = 30.0) -> bool:
        """等待当前批次跑完(测试/关停用)。返回是否已空闲。"""
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return self.state() is not JobState.RUNNING

    # -- 内部:状态 ------------------------------------------------------- #

    def _reset_status_locked(self) -> None:
        self._current_chapter = None
        self._total = 0
        self._done = 0
        self._durations = []
        self._chapters = {}
        self._logs.clear()

    def _set_state_locked(self, state: JobState) -> None:
        self._state = state
        self._bus.job_state(
            state,
            project=self._project_name,
            current_chapter=self._current_chapter,
        )

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name="synthesis-queue", daemon=True
            )
            self._worker.start()

    # -- 内部:worker ----------------------------------------------------- #

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._control.stop_requested:
                        self._tasks.clear()
                        self._queued.clear()
                        self._set_state_locked(JobState.STOPPED)
                        return
                    if self._control.pause_requested:
                        self._set_state_locked(JobState.PAUSED)
                        return
                    if not self._tasks:
                        self._set_state_locked(JobState.IDLE)
                        return
                    task = self._tasks.popleft()
                    self._queued.discard((str(task.project.root), task.name))
                self._run_task(task)
        finally:
            with self._lock:
                self._worker = None

    def _run_task(self, task: _Task) -> None:
        chapter = task.project.chapter(task.name)
        if chapter is None:  # pragma: no cover - 入队时已校验
            self._bus.chapter_failed(task.name, message="章节不存在", error_kind="not_found")
            return

        with self._lock:
            self._current_chapter = task.name

        context = ChapterContext(
            project=task.project,
            provider=task.provider,
            voices=task.voices,
            voice_names=task.voice_names,
            provider_contract_version=task.contract_version,
            params=task.params,
            policy=task.policy,
            retries=task.retries,
            control=self._control,
            bus=self._bus,
            report=task.report,
        )
        result = None
        try:
            result = self._runner.run(chapter, context)
        except ManifestError as exc:
            # manifest 是续传唯一依据 → 严重错误:停止任务并告警(design §8)。
            self._bus.log(
                f"manifest 写失败,停止任务: {exc}", level="error", chapter=task.name
            )
            self._bus.chapter_failed(task.name, message=str(exc), error_kind="manifest")
            self._control.request_stop()
        except AudiobookError as exc:
            # 开跑前校验失败(BindingError / ConfigError)等 → 该章失败,继续下一章。
            self._bus.chapter_failed(task.name, message=str(exc), error_kind=exc.kind)
        except Exception as exc:  # pragma: no cover - 兜底,避免 worker 静默死亡
            self._bus.chapter_failed(
                task.name, message=f"{type(exc).__name__}: {exc}", error_kind="fatal"
            )
        finally:
            with self._lock:
                self._current_chapter = None

        # 暂停(非停止):把本章放回队首,resume() 后从断点续跑。
        # report 置空 → 下次强制重新对账,只补未完成片段(不重做已完成)。
        if (
            result is not None
            and result.paused
            and not self._control.stop_requested
        ):
            task.report = None
            with self._lock:
                self._tasks.appendleft(task)
                self._queued.add((str(task.project.root), task.name))

    # -- 内部:事件 → 状态快照 ------------------------------------------- #

    def _on_event(self, event: Event) -> None:
        # 与 snapshot() 互斥,保证状态快照一致(RLock 允许同线程在持锁时发布事件)。
        with self._lock:
            self._apply_event(event)

    def _apply_event(self, event: Event) -> None:
        data = event.data
        if event.type == events.CHAPTER_STARTED:
            name = data.get("chapter")
            self._current_chapter = name
            self._chapters[name] = {
                "name": name,
                "total": data.get("total", 0),
                "done": 0,
                "failed": 0,
                "status": "synthesizing",
            }
        elif event.type == events.FRAGMENT_DONE:
            name = data.get("chapter")
            progress = self._chapters.get(name)
            if progress is not None:
                progress["done"] = data.get("done", progress["done"] + 1)
            self._done += 1
            elapsed = data.get("elapsed_ms")
            if elapsed:
                self._durations.append(float(elapsed) / 1000.0)
        elif event.type == events.FRAGMENT_FAILED:
            name = data.get("chapter")
            progress = self._chapters.get(name)
            if progress is not None:
                progress["failed"] += 1
            self._logs.append(
                {
                    "level": "error",
                    "message": (
                        f"片段失败(章节 {name} #{data.get('index')}):"
                        f"{data.get('error') or data.get('error_kind')}"
                    ),
                    "chapter": name,
                    "index": data.get("index"),
                    "ts": event.ts,
                }
            )
        elif event.type == events.CHAPTER_CONCATENATED:
            name = data.get("chapter")
            progress = self._chapters.get(name)
            if progress is not None:
                progress["status"] = "done"
            self._logs.append(
                {
                    "level": "info",
                    "message": f"章节完成:{name} → {data.get('output', '')}",
                    "chapter": name,
                    "ts": event.ts,
                }
            )
        elif event.type == events.CHAPTER_FAILED:
            name = data.get("chapter")
            progress = self._chapters.get(name)
            if progress is not None:
                progress["status"] = "partial"
            self._logs.append(
                {
                    "level": "error",
                    "message": f"章节失败:{name} - {data.get('message', '')}",
                    "chapter": name,
                    "ts": event.ts,
                }
            )
        elif event.type == events.LOG:
            self._logs.append(
                {
                    "level": data.get("level", "info"),
                    "message": data.get("message", ""),
                    "chapter": data.get("chapter"),
                    "index": data.get("index"),
                    "ts": event.ts,
                }
            )


def get_queue() -> SynthesisQueue:
    """取进程级全局队列单例。"""
    return SynthesisQueue.instance()


__all__ = ["SynthesisQueue", "get_queue"]
