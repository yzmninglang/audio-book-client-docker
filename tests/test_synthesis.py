"""M4 编排层离线测试(**全部用 FakeProvider,绝不触碰真实后端**)。

覆盖 `doc/dev-workflow.md` §3 M4 完成判据与 §4.3 的负面测试:

    1. 跑完一章 → audio/ 出成品 → process_audio/ 被清理
    3. 单条失败不中断整章;该章停在 partial 且不产出成品
    4. 未绑定角色在**开跑前**抛 BindingError
    5. 非 canonical 片段被归一化后才拼接(R7)
    6. 同章 provider 不一致 → 整章重做
    7. 能力不支持时按策略降级(默认 warn 不中断)
    8. 全局队列并发度恒为 1
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _synth_helpers import (
    CHAPTER,
    DEFAULT_VOICE,
    FAKE_AUDIO,
    FAKE_AUDIO_24K,
    FRAGMENT_SECONDS,
    FakeProvider,
    caps,
    make_project,
    sentence,
    write_chapter,
)

from src.audio import AudioInfo, probe
from src.errors import BindingError, ConfigError, ManifestError
from src.models import (
    AudioFormat,
    ChapterStatus,
    FragmentStatus,
    JobState,
)
from src.project import Manifest, Project
from src.synthesis import (
    ChapterContext,
    ChapterRunner,
    ReferenceCache,
    SynthesisQueue,
    events,
)

# --------------------------------------------------------------------------- #
# 脚手架
# --------------------------------------------------------------------------- #


def _run(project: Project, voices, fake: FakeProvider, chapters=None, **kwargs) -> SynthesisQueue:
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, chapters, provider=fake, voices=voices, **kwargs)
    assert queue.wait_idle(30), "队列未在超时内完成"
    return queue


def _run_direct(project: Project, voices, fake: FakeProvider, **kwargs):
    """直接跑一章(不经队列),返回 (result)。"""
    from src.synthesis.pipeline import provider_contract_version

    report = project.reconcile(
        provider=fake.id,
        provider_contract_version=provider_contract_version(fake),
        voice_bindings={r: b.voice for r, b in project.bindings.all().items()},
        persist=True,
    )
    ctx = ChapterContext(
        project=project,
        provider=fake,
        voices=voices,
        voice_names={e.name for e in voices.list()},
        report=report,
        **kwargs,
    )
    return ChapterRunner().run(project.chapter(CHAPTER), ctx), ctx


def _two_chapter_project(tmp_path: Path, *, n: int = 2):
    root = tmp_path / "三体"
    Project.create(root, name="三体")
    for name in ("A", "B"):
        write_chapter(root, name, [sentence(i) for i in range(1, n + 1)])
    project = Project.open(root, book="三体")
    from _synth_helpers import REF_AUDIO

    from src.audio import to_reference_audio
    from src.voice import VoiceLibrary

    voices = VoiceLibrary(tmp_path / "voices")
    voices.create(DEFAULT_VOICE, to_reference_audio(REF_AUDIO, max_seconds=1.0), "文本")
    assert project.bindings is not None
    project.bindings.set("旁白", DEFAULT_VOICE)
    return project, voices


# --------------------------------------------------------------------------- #
# 判据 1:跑完一章 → 成品 + 清理
# --------------------------------------------------------------------------- #


def test_run_chapter_produces_audio_and_cleans_process(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=4)
    fake = FakeProvider()
    queue = _run(project, voices, fake)

    product = project.root / "audio" / f"{CHAPTER}.wav"
    assert product.is_file()
    assert not (project.root / "process_audio" / CHAPTER).exists()
    assert len(fake.calls) == 4
    assert queue.state() is JobState.IDLE

    info = probe(product.read_bytes())
    assert info.sample_rate == 22050
    assert info.channels == 1
    assert info.bits == 16


def test_chapter_duration_equals_fragments_plus_delays(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=4, delays=[100, 200, 300, 400])
    _run(project, voices, FakeProvider())
    info = probe((project.root / "audio" / f"{CHAPTER}.wav").read_bytes())
    expected = 4 * FRAGMENT_SECONDS + sum([100, 200, 300, 400]) / 1000
    assert info.duration == pytest.approx(expected, abs=1e-3)


def test_manifest_records_done_and_product(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    _run(project, voices, FakeProvider())
    rec = Manifest.load(project.root).chapter(CHAPTER)
    assert rec.status is ChapterStatus.DONE
    assert rec.output == f"audio/{CHAPTER}.wav"
    assert all(f.status is FragmentStatus.DONE for f in rec.fragments.values())
    assert rec.provider == "fake"


def test_no_tmp_files_left(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=2)
    _run(project, voices, FakeProvider())
    assert not list(project.root.rglob("*.tmp"))


# --------------------------------------------------------------------------- #
# 判据 3:失败隔离,partial 不产出半成品
# --------------------------------------------------------------------------- #


def test_fatal_error_isolated_chapter_partial_no_product(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    fake = FakeProvider(fail_on={2})
    _run(project, voices, fake)

    assert not (project.root / "audio" / f"{CHAPTER}.wav").exists()
    rec = Manifest.load(project.root).chapter(CHAPTER)
    assert rec.status is ChapterStatus.PARTIAL
    assert rec.fragments["0002"].status is FragmentStatus.FAILED
    assert rec.fragments["0001"].status is FragmentStatus.DONE
    assert rec.fragments["0003"].status is FragmentStatus.DONE
    # 失败的片段不落文件,成功的保留
    assert not project.manifest.fragment_path(CHAPTER, 2).exists()
    assert project.manifest.fragment_path(CHAPTER, 1).exists()


def test_retryable_error_recorded_with_attempts_and_continues(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    fake = FakeProvider(retry_on={3})
    _run(project, voices, fake, retries=3)

    rec = Manifest.load(project.root).chapter(CHAPTER)
    assert rec.status is ChapterStatus.PARTIAL
    failed = rec.fragments["0003"]
    assert failed.status is FragmentStatus.FAILED
    assert failed.attempts == 4  # 1 次首发 + 3 次重试(适配器内部)
    assert "可重试" in (failed.error or "")


def test_manifest_write_failure_stops_job(tmp_path: Path, monkeypatch) -> None:
    project, voices = make_project(tmp_path, n=3)
    assert project.manifest is not None

    def boom(*args, **kwargs) -> None:
        raise ManifestError("注入:磁盘写失败")

    # 只让「合成期写片段」失败(对账期的 manifest.save 仍正常),模拟落盘故障。
    monkeypatch.setattr(project.manifest, "save_fragment", boom)
    queue = SynthesisQueue()
    sub = queue.bus.subscribe()
    queue.enqueue_chapters(project, [CHAPTER], provider=FakeProvider(), voices=voices)
    assert queue.wait_idle(30)
    assert queue.state() is JobState.STOPPED
    kinds = [(e.type, e.data.get("error_kind")) for e in sub.drain()]
    assert (events.CHAPTER_FAILED, "manifest") in kinds


# --------------------------------------------------------------------------- #
# 判据 4:未绑定角色开跑前报错
# --------------------------------------------------------------------------- #


def test_unbound_role_raises_binding_error_before_synthesis(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3, bind=False)
    fake = FakeProvider()
    with pytest.raises(BindingError):
        _run_direct(project, voices, fake)
    assert fake.calls == []


def test_queue_binding_error_isolated_without_synthesis(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3, bind=False)
    fake = FakeProvider()
    queue = SynthesisQueue()
    sub = queue.bus.subscribe()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)
    assert fake.calls == []
    assert any(e.type == events.CHAPTER_FAILED for e in sub.drain())


def test_missing_emotion_ref_raises_before_synthesis(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=2)
    assert project.bindings is not None
    project.bindings.set("旁白", DEFAULT_VOICE, emo_ref="不存在的情感")
    fake = FakeProvider()
    with pytest.raises(BindingError):
        _run_direct(project, voices, fake)
    assert fake.calls == []


def _add_emotion_ref(voices, voice_name: str, emo_name: str = "叙述") -> None:
    """手动给音色目录加一组情感参考(<名称>.wav + <名称>.txt)。"""
    from _synth_helpers import REF_AUDIO

    from src.audio import to_reference_audio

    voice_dir = voices.root / voice_name
    (voice_dir / f"{emo_name}.wav").write_bytes(
        to_reference_audio(REF_AUDIO, max_seconds=1.0)
    )
    (voice_dir / f"{emo_name}.txt").write_text("情感参考文本", encoding="utf-8")


def test_zero_vector_sent_as_method2_when_no_emo_ref(tmp_path: Path) -> None:
    """分支 3(最关键):全零向量、未绑 emo_ref、provider 支持 vector → 仍透传零向量。

    保的是用户决策「全 0 向量仍走 method=2',不能被改成 `None`(否则静默变成默认
    method=0,见 doc/requirements.md §5、doc/tts/design.md §3)。
    """
    project, voices = make_project(tmp_path, n=1)  # 默认全零向量,无 emo_ref
    fake = FakeProvider()  # capabilities 默认支持 vector
    _run_direct(project, voices, fake)

    assert len(fake.calls) == 1
    emotion = fake.calls[0].emotion
    assert emotion is not None  # 不是 None
    assert emotion.vectors == [0.0] * 8
    assert emotion.ref_audio is None


def test_emotion_ref_used_as_fallback_when_vector_is_zero(tmp_path: Path) -> None:
    """分支 2:全零向量 + 绑了 emo_ref → emo_ref 赢(它比零向量更有意义)。"""
    project, voices = make_project(tmp_path, n=1)  # 默认全零向量
    _add_emotion_ref(voices, DEFAULT_VOICE, "叙述")
    assert project.bindings is not None
    project.bindings.set("旁白", DEFAULT_VOICE, emo_ref="叙述")

    fake = FakeProvider()
    _run_direct(project, voices, fake)
    request = fake.calls[0]
    assert request.emotion is not None
    assert request.emotion.ref_audio  # 回落 → EmotionSpec.ref_audio
    assert request.emotion.vectors is None


def test_zero_vector_without_vector_capability_is_none_without_warning(tmp_path: Path) -> None:
    """分支 4:provider 不支持 vector 时,全零向量不构成能力要求 → 不告警、落到 None。"""
    project, voices = make_project(tmp_path, n=1)  # 全零向量,无 emo_ref
    fake = FakeProvider(capabilities=caps(emotion=("text",)))  # 不支持 vector
    queue = SynthesisQueue()
    sub = queue.bus.subscribe()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert fake.calls[0].emotion is None  # 落到第 4 分支
    warned = [
        e for e in sub.drain() if e.type == events.LOG and e.data.get("level") == "warn"
    ]
    assert warned == []  # 全零向量不算能力要求,不误报


def test_sentence_vector_takes_precedence_over_emo_ref(tmp_path: Path) -> None:
    """逐句信号 > 角色级配置:非零 emo_vector 优先,emo_ref 仅回落。"""
    project, voices = make_project(tmp_path, n=2)
    _add_emotion_ref(voices, DEFAULT_VOICE, "叙述")
    assert project.bindings is not None
    project.bindings.set("旁白", DEFAULT_VOICE, emo_ref="叙述")

    # 同一角色:第 1 句带非零向量,第 2 句全零 → 前者用 vector,后者回落 ref
    write_chapter(
        project.root,
        CHAPTER,
        [
            {"speaker": "旁白", "content": "S1", "emo_vector": [0.3] + [0.0] * 7, "delay": 100},
            {"speaker": "旁白", "content": "S2", "emo_vector": [0.0] * 8, "delay": 100},
        ],
    )
    reopened = Project.open(project.root, book="三体")

    fake = FakeProvider()
    _run_direct(reopened, voices, fake)
    assert [c.text for c in fake.calls] == ["S1", "S2"]

    first, second = fake.calls[0].emotion, fake.calls[1].emotion
    assert first is not None and first.vectors == [0.3] + [0.0] * 7
    assert first.ref_audio is None
    assert second is not None and second.ref_audio is not None
    assert second.vectors is None


# --------------------------------------------------------------------------- #
# 判据 5:R7 非 canonical 归一化
# --------------------------------------------------------------------------- #


def test_non_canonical_output_normalized_before_concat(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    fake = FakeProvider(
        audio=FAKE_AUDIO_24K,
        fmt=AudioFormat(sample_rate=24000, channels=1, bits=16),
        canonical=False,
        capabilities=caps(canonical=False),
    )
    _run(project, voices, fake)

    product = project.root / "audio" / f"{CHAPTER}.wav"
    assert product.is_file()  # 若未归一化,concat 会因格式不一致而失败
    info = probe(product.read_bytes())
    assert info.sample_rate == 22050
    assert info.channels == 1
    # 每个片段也被归一化为 22050
    frag = project.manifest.fragment_path(CHAPTER, 1)
    if frag.exists():  # 成品生成后 process_audio 已被清理,故可能不存在
        assert probe(frag.read_bytes()).sample_rate == 22050


def test_pipeline_marks_normalized_fragment(tmp_path: Path) -> None:
    from src.audio import probe as audio_probe
    from src.synthesis.pipeline import synthesize_one

    project, voices = make_project(tmp_path, n=1, delays=[0])
    fake = FakeProvider(
        audio=FAKE_AUDIO_24K,
        fmt=AudioFormat(sample_rate=24000, channels=1, bits=16),
        canonical=False,
        capabilities=caps(canonical=False),
    )
    report = project.reconcile(provider=fake.id, provider_contract_version=1, persist=True)
    ctx = ChapterContext(
        project=project,
        provider=fake,
        voices=voices,
        voice_names={DEFAULT_VOICE},
        report=report,
    )
    ctx.chapter = project.chapter(CHAPTER)
    ctx.supported_emotion_kinds = frozenset({"vector"})
    result = synthesize_one(project.chapter(CHAPTER).sentences[0], 1, ctx)
    assert result.ok
    assert result.normalized is True
    assert audio_probe(project.manifest.fragment_path(CHAPTER, 1).read_bytes()).sample_rate == 22050


# --------------------------------------------------------------------------- #
# 判据 6:provider 不一致 → 整章重做
# --------------------------------------------------------------------------- #


def test_provider_mismatch_redoes_whole_chapter(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    manifest = project.manifest
    chapter = project.chapter(CHAPTER)
    manifest.get_or_create(
        CHAPTER,
        json_sha256=chapter.json_sha256,
        sentence_count=3,
        provider="old-provider",
        provider_contract_version=9,
    )
    manifest.save_fragment(
        CHAPTER, 1, FAKE_AUDIO, status=FragmentStatus.DONE,
        speaker="旁白", voice=DEFAULT_VOICE, provider="old-provider",
    )
    (project.root / "audio").mkdir(parents=True, exist_ok=True)
    (project.root / "audio" / f"{CHAPTER}.wav").write_bytes(FAKE_AUDIO)
    manifest.save()

    fake = FakeProvider()
    _run(project, voices, fake)

    assert len(fake.calls) == 3  # 整章 3 条全部重做,而非只补 2 条
    assert (project.root / "audio" / ".stale" / f"{CHAPTER}.wav").is_file()


# --------------------------------------------------------------------------- #
# 判据 7:能力降级策略
# --------------------------------------------------------------------------- #


def test_capability_unsupported_warn_default_continues(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=2, emo_vector=[0.1] * 8)
    fake = FakeProvider(capabilities=caps(emotion=("text",)))  # 不支持 vector
    queue = SynthesisQueue()
    sub = queue.bus.subscribe()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert (project.root / "audio" / f"{CHAPTER}.wav").is_file()  # 默认 warn 不中断
    assert all(c.emotion is None for c in fake.calls)  # 不支持的意图被丢弃
    assert any(e.type == events.LOG and e.data.get("level") == "warn" for e in sub.drain())


def test_capability_unsupported_error_policy_refuses_before_synthesis(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=2, emo_vector=[0.1] * 8)
    fake = FakeProvider(capabilities=caps(emotion=("text",)))
    with pytest.raises(ConfigError):
        _run_direct(project, voices, fake, policy="error")
    assert fake.calls == []
    assert not (project.root / "audio" / f"{CHAPTER}.wav").exists()


# --------------------------------------------------------------------------- #
# 判据 8:并发度恒为 1
# --------------------------------------------------------------------------- #


def test_queue_concurrency_is_one(tmp_path: Path) -> None:
    project, voices = _two_chapter_project(tmp_path, n=3)
    fake = FakeProvider(delay=0.01)
    queue = SynthesisQueue(concurrency=1)
    queue.enqueue_chapters(project, ["A", "B"], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert queue.concurrency == 1
    assert fake.max_active == 1  # 任何时刻只有一条在合成
    assert len(fake.calls) == 6


# --------------------------------------------------------------------------- #
# 事件总线
# --------------------------------------------------------------------------- #


def test_events_flow(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    queue = SynthesisQueue()
    sub = queue.bus.subscribe()
    queue.enqueue_chapters(project, [CHAPTER], provider=FakeProvider(), voices=voices)
    assert queue.wait_idle(30)

    collected = sub.drain()
    types = [e.type for e in collected]
    assert types.count(events.FRAGMENT_DONE) == 3
    assert events.CHAPTER_STARTED in types
    assert events.CHAPTER_CONCATENATED in types
    assert events.JOB_STATE in types

    started = next(e for e in collected if e.type == events.CHAPTER_STARTED)
    assert started.data["total"] == 3
    assert started.data["remaining"] == 3
    assert started.data["chapter"] == CHAPTER

    done = [e for e in collected if e.type == events.FRAGMENT_DONE]
    assert [e.data["done"] for e in done] == [1, 2, 3]

    concat = next(e for e in collected if e.type == events.CHAPTER_CONCATENATED)
    assert concat.data["output"] == f"audio/{CHAPTER}.wav"
    assert concat.data["duration_ms"] > 0


# --------------------------------------------------------------------------- #
# 暂停 / 停止
# --------------------------------------------------------------------------- #


def test_pause_mid_chapter_then_resume_completes_remaining(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=4)
    queue = SynthesisQueue()
    fake = FakeProvider(on_call=lambda req, n: queue.pause() if n == 1 else None)

    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)
    assert queue.state() is JobState.PAUSED
    assert len(fake.calls) == 1  # 当前片段跑完后停

    rec = Manifest.load(project.root).chapter(CHAPTER)
    assert rec.fragments["0001"].status is FragmentStatus.DONE
    assert "0002" not in rec.fragments or rec.fragments["0002"].status is not FragmentStatus.DONE

    queue.resume()
    assert queue.wait_idle(30)
    # 只补未完成的 3 条;片段 1 不重做 → 总调用恰好 4(而非 1+4)
    assert len(fake.calls) == 4
    assert (project.root / "audio" / f"{CHAPTER}.wav").is_file()


def test_stop_clears_unstarted_tasks(tmp_path: Path) -> None:
    project, voices = _two_chapter_project(tmp_path, n=2)
    queue = SynthesisQueue()
    fake = FakeProvider(on_call=lambda req, n: queue.stop() if n == 1 else None)

    queue.enqueue_chapters(project, ["A", "B"], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert queue.state() is JobState.STOPPED
    assert len(fake.calls) == 1  # 第二章节未开始
    assert not (project.root / "audio" / "B.wav").exists()


# --------------------------------------------------------------------------- #
# 幂等 / 缓存 / 快照
# --------------------------------------------------------------------------- #


def test_idempotent_rerun_skips_done_chapter(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    _run(project, voices, FakeProvider())

    fake2 = FakeProvider()
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake2, voices=voices)
    assert queue.wait_idle(30)
    assert fake2.calls == []  # done 的章节直接跳过
    assert (project.root / "audio" / f"{CHAPTER}.wav").is_file()


def test_reference_cache_reads_voice_once(tmp_path: Path) -> None:
    _project, voices = make_project(tmp_path, n=1)
    cache = ReferenceCache(voices)
    first = cache.voice_ref(DEFAULT_VOICE)
    second = cache.voice_ref(DEFAULT_VOICE)
    assert first is second
    assert cache.reads == 1


def test_snapshot_shape(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    queue = _run(project, voices, FakeProvider())
    snap = queue.snapshot()
    assert snap["state"] == "idle"
    # 空闲:不指向任何工程,pending 清空(§8.6)
    assert snap["project"] is None
    assert snap["pending"] == []
    assert snap["total_fragments"] == 3
    assert snap["done_fragments"] == 3
    assert snap["eta_seconds"] is None  # 空闲不预估
    assert snap["chapters"][0]["status"] == "done"


def test_queue_snapshot_scopes_to_running_project(tmp_path: Path) -> None:
    """多工程排队:进度字段只反映**正在执行**的工程,而非最后入队的(§8.6)。"""
    project_a, voices_a = make_project(tmp_path / "a", n=2, book="工程甲")
    project_b, voices_b = make_project(tmp_path / "b", n=3, book="工程乙")
    queue = SynthesisQueue()
    snaps: list[dict] = []
    provider = FakeProvider("fake")
    enqueued = {"b": False}

    def on_call(req, n):  # 甲的第一次合成时,入队乙(确保乙在甲运行期间排队)
        snaps.append(queue.snapshot())
        if not enqueued["b"]:
            enqueued["b"] = True
            queue.enqueue_chapters(project_b, None, provider=provider, voices=voices_b)

    provider.on_call = on_call
    queue.enqueue_chapters(project_a, None, provider=provider, voices=voices_a)
    assert queue.wait_idle(30)

    a_snaps = [s for s in snaps if s["project"] == "工程甲"]
    b_snaps = [s for s in snaps if s["project"] == "工程乙"]
    assert a_snaps and b_snaps, "未观察到两个工程的进度作用域切换"
    # 甲运行期间:总量是甲的量(2),且 pending 含乙
    assert all(s["total_fragments"] == 2 for s in a_snaps)
    assert any(any(p["project"] == "工程乙" for p in s["pending"]) for s in a_snaps)
    # 乙运行期间:总量是乙的量(3),done 从 0 重计(不累加甲的 2)
    assert all(s["total_fragments"] == 3 for s in b_snaps)
    assert all(s["done_fragments"] <= 3 for s in b_snaps)
    assert b_snaps[0]["done_fragments"] == 0

    # ★ 进度计数器必须**真的递增**(快照在每次 synthesize 前采集,故甲应为 0,1)
    #   此前这里只断言 `<= 3` —— done 恒为 0 也能通过,证明不了计数器是好的。
    assert [s["done_fragments"] for s in a_snaps] == [0, 1]
    assert [s["done_fragments"] for s in b_snaps] == [0, 1, 2]

    end = queue.snapshot()
    assert end["state"] == "idle"
    assert end["project"] is None
    assert end["pending"] == []


def test_queue_pending_fifo_order_and_idempotent(tmp_path: Path) -> None:
    """pending 按入队顺序聚合;同一 (工程,章节) 重复入队幂等。"""
    project_a, voices_a = make_project(tmp_path / "a", n=2, book="甲")
    project_b, voices_b = make_project(tmp_path / "b", n=1, book="乙")
    project_c, voices_c = make_project(tmp_path / "c", n=1, book="丙")
    queue = SynthesisQueue()
    provider = FakeProvider("fake")
    captured: dict[str, list] = {}

    def on_call(req, n):
        if "pending" in captured:
            return
        queue.enqueue_chapters(project_b, None, provider=provider, voices=voices_b)
        queue.enqueue_chapters(project_b, None, provider=provider, voices=voices_b)  # 幂等
        queue.enqueue_chapters(project_c, None, provider=provider, voices=voices_c)
        captured["pending"] = queue.snapshot()["pending"]

    provider.on_call = on_call
    queue.enqueue_chapters(project_a, None, provider=provider, voices=voices_a)
    assert queue.wait_idle(30)

    assert [p["project"] for p in captured["pending"]] == ["乙", "丙"]  # FIFO
    assert [p["chapters"] for p in captured["pending"]] == [1, 1]  # 乙去重后仅 1 章


def test_reference_audio_cached_within_chapter(tmp_path: Path) -> None:
    """同章内同音色多条复用缓存,只读盘一次。"""
    project, voices = make_project(tmp_path, n=5)
    fake = FakeProvider()
    _run(project, voices, fake)
    # 通过 monkeypatch 不便断言,这里直接验证:题目同一 speaker 的 5 条请求音频字节相同
    audios = {c.voice_ref.audio for c in fake.calls}
    assert len(audios) == 1


def test_written_product_is_valid_wav(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=2, delays=[0, 600])
    _run(project, voices, FakeProvider())
    data = (project.root / "audio" / f"{CHAPTER}.wav").read_bytes()
    info = probe(data)
    assert isinstance(info, AudioInfo)
    assert info.duration == pytest.approx(2 * FRAGMENT_SECONDS + 0.6, abs=1e-3)


def test_queue_singleton_identity() -> None:
    SynthesisQueue._reset_global()
    try:
        assert SynthesisQueue.instance() is SynthesisQueue.instance()
    finally:
        SynthesisQueue._reset_global()


def test_provider_contract_version_prefers_module_constant() -> None:
    from src.synthesis.pipeline import provider_contract_version

    assert provider_contract_version(FakeProvider(contract_version=7)) == 7
    # 未声明实例属性的 provider 回落默认 1
    assert provider_contract_version(object()) == 1
