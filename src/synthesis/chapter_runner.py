"""单章执行器 —— 章节状态机(见 `doc/synthesis/design.md` §3)。

    pending → scanning(续传对账) → synthesizing(逐条) → concatenating(拼接) → done
                                            ↓ 失败
                                        partial(记录失败条目,可重跑)

关键行为:

    | 来源检查 | 本章 provider 与当前激活后端不一致 → 整章重做,不做部分混合 |
    | 能力检查 | 开跑前校验 provider 是否支持本章需要的情感意图,按策略告警/报错 |
    | 续传对账 | 进入 synthesizing 前,按 project/design.md §5.4 确定哪些条目要跑 |
    | 失败隔离 | 单条失败不中断整章,记录后继续;章末汇总 |
    | 拼接前提 | 仅当**全部条目成功**才拼接成章;有失败则停在 partial,不产出半成品 |
    | 清理时机 | **拼接成功并写入 audio/ 之后**才删 process_audio/<章>/ |
    | 幂等     | 对 done 的章节重跑 = 直接跳过 |
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

from ..audio import AudioInfo, concat, probe
from ..errors import BindingError, InvalidInputError, NotFoundError
from ..models import CANONICAL_FORMAT, Chapter, ChapterStatus
from ..project import atomic_write_bytes
from .events import Event, EventBus
from .pipeline import ChapterContext, FragmentResult, check_capabilities, synthesize_one

_CANONICAL_TEMPLATE = AudioInfo(
    format="wav",
    sample_rate=CANONICAL_FORMAT.sample_rate,
    channels=CANONICAL_FORMAT.channels,
    bits=CANONICAL_FORMAT.bits,
    duration=0.0,
)


@dataclass
class ChapterResult:
    """单章运行结果。"""

    name: str
    status: ChapterStatus
    generated: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)  # 对账时已完成的
    failed: list[FragmentResult] = field(default_factory=list)
    adopted: list[int] = field(default_factory=list)  # 孤儿片段被补记 done
    concatenated: bool = False
    output_relpath: str | None = None
    duration_s: float | None = None
    paused: bool = False
    skipped_whole: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ChapterStatus.DONE


def planned_indices(action: object, sentence_count: int) -> list[int]:
    """由对账动作推出本章要生成的片段序号(1-based)。

    * 无动作 / `redo` → 全部序号;
    * `skip` → 空(整章已完成);
    * `resume` / `stale` → 对账给出的 todo ∪ stale。
    """
    if action is None:
        return list(range(1, sentence_count + 1))
    kind = getattr(action, "action", "resume")
    if kind == "skip":
        return []
    if kind == "redo":
        return list(range(1, sentence_count + 1))
    todo = set(getattr(action, "todo", []) or [])
    todo |= set(getattr(action, "stale", []) or [])
    return sorted(todo)


class ChapterRunner:
    """执行单章:`run(chapter, ctx, on_event=None)`。"""

    def run(
        self,
        chapter: Chapter,
        ctx: ChapterContext,
        on_event: Callable[[Event], None] | None = None,
    ) -> ChapterResult:
        bus = ctx.bus
        if on_event is not None:
            bus = _TeeBus(bus, on_event)

        ctx.chapter = chapter

        # --- scanning:续传对账 ------------------------------------------ #
        report = ctx.report
        if report is None:
            assert ctx.project.manifest is not None
            report = ctx.project.reconcile(
                provider=ctx.provider.id,
                provider_contract_version=ctx.provider_contract_version,
                voice_bindings=ctx.bindings_map(),
                persist=True,
            )
            ctx.report = report
        action = report.action_for(chapter.name) if report is not None else None

        planned = planned_indices(action, chapter.sentence_count)

        # 幂等:done 的章节直接跳过。
        if action is not None and getattr(action, "action", "") == "skip":
            return ChapterResult(
                name=chapter.name, status=ChapterStatus.DONE, skipped_whole=True
            )

        # --- 开跑前一次性校验:绑定有效性(§8) --------------------------- #
        self._validate_bindings(chapter, ctx)

        # --- 开跑前能力检查(§9) ---------------------------------------- #
        ctx.supported_emotion_kinds = check_capabilities(chapter, ctx)

        # --- 开跑前校验情感参考有效性(仅当后端支持 ref_audio) ---------- #
        self._validate_emotion_refs(chapter, ctx)

        # --- synthesizing:逐条 ----------------------------------------- #
        done_before = chapter.sentence_count - len(planned)
        generated: list[int] = []
        failed: list[FragmentResult] = []
        paused = False
        done_counter = done_before
        sentences = {s.index: s for s in chapter.sentences}

        bus.chapter_started(
            chapter.name, total=chapter.sentence_count, remaining=len(planned)
        )

        for idx in planned:
            # 暂停/停止:当前片段跑完后停,不中断进行中的 HTTP(§6)。
            if ctx.control.stop_requested or ctx.control.pause_requested:
                paused = True
                break

            sentence = sentences[idx]
            result = synthesize_one(sentence, idx, ctx)

            if result.ok:
                generated.append(idx)
                done_counter += 1
                bus.fragment_done(
                    chapter.name,
                    idx,
                    elapsed_ms=int(result.duration_s * 1000),
                    done=done_counter,
                )
            else:
                failed.append(result)
                bus.fragment_failed(
                    chapter.name,
                    idx,
                    error_kind=result.error_kind or "fatal",
                    error=result.error or "",
                    attempts=result.attempts,
                )

        manifest = ctx.project.manifest
        assert manifest is not None
        record = manifest.chapter(chapter.name)

        if paused:
            # 暂停:不产出半成品;有失败则记 partial,否则退回未完成(pending)。
            if record is not None:
                record.status = (
                    ChapterStatus.PARTIAL if failed else ChapterStatus.PENDING
                )
                manifest.save()
            return ChapterResult(
                name=chapter.name,
                status=ChapterStatus.PARTIAL if failed else ChapterStatus.PENDING,
                generated=generated,
                failed=failed,
                paused=True,
            )

        # --- 失败隔离:有失败 → partial,不拼接(§3) --------------------- #
        if failed:
            if record is not None:
                record.status = ChapterStatus.PARTIAL
                manifest.save()
            bus.chapter_failed(
                chapter.name,
                message=f"{len(failed)} 条片段失败,不产出成品",
                error_kind="fatal",
                failed=[f.index for f in failed],
            )
            return ChapterResult(
                name=chapter.name,
                status=ChapterStatus.PARTIAL,
                generated=generated,
                failed=failed,
                paused=False,
            )

        # --- concatenating:全部成功 → 拼接 ----------------------------- #
        try:
            data, duration_s = self._concatenate(chapter, ctx)
        except (InvalidInputError, NotFoundError) as exc:
            if record is not None:
                record.status = ChapterStatus.PARTIAL
                manifest.save()
            bus.chapter_failed(
                chapter.name, message=f"拼接失败: {exc}", error_kind="fatal"
            )
            return ChapterResult(
                name=chapter.name,
                status=ChapterStatus.PARTIAL,
                generated=generated,
                failed=failed,
            )

        # 写入 audio/ 之后才清理 process_audio/<章>/(§3 清理时机)。
        atomic_write_bytes(manifest.chapter_audio_path(chapter.name), data)

        if record is not None:
            record.status = ChapterStatus.DONE
            record.output = manifest.chapter_audio_relpath(chapter.name)
            record.provider = ctx.provider.id
            record.provider_contract_version = ctx.provider_contract_version
            manifest.save()

        shutil.rmtree(manifest.fragment_dir(chapter.name), ignore_errors=True)

        bus.chapter_concatenated(
            chapter.name,
            output=manifest.chapter_audio_relpath(chapter.name),
            duration_ms=int(duration_s * 1000),
        )
        return ChapterResult(
            name=chapter.name,
            status=ChapterStatus.DONE,
            generated=generated,
            failed=[],
            concatenated=True,
            output_relpath=manifest.chapter_audio_relpath(chapter.name),
            duration_s=duration_s,
        )

    # -- 内部 ------------------------------------------------------------- #

    @staticmethod
    def _validate_bindings(chapter: Chapter, ctx: ChapterContext) -> None:
        """开跑前一次性校验本章所有绑定有效性(§8)。

        Raises:
            BindingError: 任一角未绑定,或绑定指向的音色不存在。
        """
        if ctx.project.bindings is None:  # pragma: no cover - Project 总会加载绑定表
            return
        roles = sorted({s.speaker for s in chapter.sentences if s.speaker})
        problems = ctx.project.bindings.missing(roles, ctx.voice_names)
        if problems:
            raise BindingError(
                f"章节 {chapter.name} 存在未绑定或失效的角色: " + ";".join(problems)
            )

    @staticmethod
    def _validate_emotion_refs(chapter: Chapter, ctx: ChapterContext) -> None:
        """开跑前校验绑定声明的 `emo_ref` 在音色里真实存在(§8 精神)。

        仅当后端支持 `ref_audio` 时才校验(否则按能力降级策略忽略该意图)。

        Raises:
            BindingError: `emo_ref` 指向的情感参考不存在。
        """
        from ..errors import NotFoundError as _NotFound
        from .pipeline import EMOTION_KIND_REF_AUDIO

        if EMOTION_KIND_REF_AUDIO not in ctx.supported_emotion_kinds:
            return
        bindings = ctx.project.bindings
        if bindings is None:
            return
        roles = sorted({s.speaker for s in chapter.sentences if s.speaker})
        for role in roles:
            binding = bindings.get(role)
            emo_ref = getattr(binding, "emo_ref", None) if binding is not None else None
            if not emo_ref:
                continue
            try:
                entry = ctx.voices.get(binding.voice)
            except _NotFound as exc:
                raise BindingError(f"角色 {role} 绑定的音色不可用: {exc}") from exc
            if emo_ref not in entry.emo_refs:
                raise BindingError(
                    f"角色 {role} 的情感参考不存在: {binding.voice}/{emo_ref}"
                )

    @staticmethod
    def _concatenate(chapter: Chapter, ctx: ChapterContext) -> tuple[bytes, float]:
        """按 JSON 顺序拼接全部片段,插入每条的 delay 静音。"""
        manifest = ctx.project.manifest
        assert manifest is not None

        fragments: list[bytes] = []
        delays: list[int] = []
        for sentence in chapter.sentences:
            path = manifest.fragment_path(chapter.name, sentence.index)
            if not path.is_file():
                relpath = manifest.fragment_relpath(chapter.name, sentence.index)
                raise NotFoundError(f"片段文件缺失,无法拼接: {relpath}")
            fragments.append(path.read_bytes())
            delays.append(sentence.delay_ms)

        data = concat(fragments, delays, template=_CANONICAL_TEMPLATE)
        try:
            duration = probe(data).duration
        except Exception:  # pragma: no cover - concat 产物恒可探测
            duration = len(data)
        return data, float(duration)


class _TeeBus(EventBus):
    """把事件同时交给 `on_event` 回调(便于 api/ 侧不订阅总线时的直连消费)。"""

    def __init__(self, inner: EventBus, hook: Callable[[Event], None]) -> None:
        super().__init__()
        self._inner = inner
        self._hook = hook

    def publish(self, event: Event) -> Event:
        self._inner.publish(event)
        try:
            self._hook(event)
        except Exception:  # pragma: no cover - 回调异常不影响合成
            pass
        return event


__all__ = ["ChapterResult", "ChapterRunner", "planned_indices"]
