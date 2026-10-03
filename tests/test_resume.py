"""★ 断点续传的破坏性测试(`doc/dev-workflow.md` §3 M4 完成判据 ②、§4.3 负面测试)。

全部离线,用 `FakeProvider`,**绝不触碰真实后端**。核心是:

    * `kill -9` 子进程 → 重开 → **只补未完成部分,不重做已完成章节**(判据 ②)
    * 修改章节 JSON → 片段作废重生成(§4.3 #2)
    * 半截/字节数不符的片段 → 判为未完成,重新生成(§4.3 #6)
    * 无记录但文件存在 → 补记 done(收养孤儿,不重生成)
    * 绑定变更 → 标 stale,**不自动重做**(project/design.md §5.5)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from _synth_helpers import (
    CHAPTER,
    DEFAULT_VOICE,
    FAKE_AUDIO,
    REF_AUDIO,
    REPO_ROOT,
    TESTS_DIR,
    FakeProvider,
    make_project,
    sentence,
    write_chapter,
)

from src.audio import to_reference_audio
from src.models import ChapterStatus, FragmentStatus
from src.project import Manifest, Project
from src.synthesis import SynthesisQueue
from src.voice import VoiceLibrary

# --------------------------------------------------------------------------- #
# 判据 ②:kill -9 后续传
# --------------------------------------------------------------------------- #

_CHILD_SCRIPT = r'''
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["REPO_ROOT"])
sys.path.insert(0, os.environ["TESTS_DIR"])

from _synth_helpers import FakeProvider  # noqa: E402
from src.project import Project  # noqa: E402
from src.synthesis import ChapterContext, ChapterRunner  # noqa: E402
from src.synthesis.pipeline import provider_contract_version  # noqa: E402
from src.voice import VoiceLibrary  # noqa: E402

root = Path(os.environ["PROJECT_ROOT"])
voices_root = Path(os.environ["VOICES_ROOT"])
chapter_name = os.environ["CHAPTER"]

fake = FakeProvider(
    block_on=int(os.environ["BLOCK_AT"]),
    block_marker=os.environ["MARKER"],
    call_log=os.environ["CALL_LOG"],
)
project = Project.open(root, book=os.environ.get("BOOK", "三体"))
voices = VoiceLibrary(voices_root)
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
)
ChapterRunner().run(project.chapter(chapter_name), ctx)
'''


def test_kill9_then_resume_only_generates_missing(tmp_path: Path) -> None:
    n = 8
    block_at = 4
    project, voices = make_project(tmp_path, n=n)
    script = tmp_path / "_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")

    call_log = tmp_path / "calls_child.txt"
    marker = tmp_path / "blocked.marker"
    env = dict(os.environ)
    env.update(
        REPO_ROOT=str(REPO_ROOT),
        TESTS_DIR=str(TESTS_DIR),
        PROJECT_ROOT=str(project.root),
        VOICES_ROOT=str(voices.root),
        CHAPTER=CHAPTER,
        BLOCK_AT=str(block_at),
        MARKER=str(marker),
        CALL_LOG=str(call_log),
        PYTHONPATH=str(REPO_ROOT),
    )

    # --- 阶段 1:子进程跑一部分后被 kill -9 ------------------------ #
    proc = subprocess.Popen([sys.executable, str(script)], env=env)
    deadline = time.time() + 20
    while not marker.exists() and time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert marker.exists(), "子进程未到达阻塞点(可能提前失败)"
    proc.kill()  # SIGKILL
    returncode = proc.wait(timeout=10)
    assert returncode == -9

    # --- 磁盘/manifest 状态:1..block_at-1 已完成,block_at 未完成 --- #
    manifest = Manifest.load(project.root)
    rec = manifest.chapter(CHAPTER)
    done_before = sorted(
        int(k) for k, v in rec.fragments.items() if v.status is FragmentStatus.DONE
    )
    assert done_before == [1, 2, 3]
    assert "0004" not in rec.fragments or rec.fragments["0004"].status is not FragmentStatus.DONE
    assert not (project.root / "audio" / f"{CHAPTER}.wav").exists()

    # 子进程尝试了 S1..S4,但第 4 条在阻塞中被 kill(未完成、未落盘)
    child_calls = call_log.read_text(encoding="utf-8").strip().splitlines()
    assert [line.split("\t")[1] for line in child_calls] == ["S1", "S2", "S3", "S4"]

    # 记录已完成片段的字节数,续传后应保持不变(不重做)
    frag1_bytes = manifest.fragment_path(CHAPTER, 1).read_bytes()

    # --- 阶段 2:重开(全新进程内对象)续传 -------------------------- #
    reopened = Project.open(project.root, book="三体")
    parent_log = tmp_path / "calls_parent.txt"
    fake = FakeProvider(call_log=parent_log)
    queue = SynthesisQueue()
    queue.enqueue_chapters(reopened, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    # 续传只生成未完成的 4..8,共 5 次;绝不重做 1..3
    assert len(fake.calls) == 5
    assert [c.text for c in fake.calls] == ["S4", "S5", "S6", "S7", "S8"]
    assert (reopened.root / "audio" / f"{CHAPTER}.wav").is_file()

    # 片段 1 的原始字节未被改写(重建过程未触碰已完成的片段)
    # 注:成品生成后 process_audio 被清理,故仅在它仍存在时校验。
    frag1 = reopened.manifest.fragment_path(CHAPTER, 1)
    if frag1.exists():
        assert frag1.read_bytes() == frag1_bytes

    final = Manifest.load(project.root).chapter(CHAPTER)
    assert final.status is ChapterStatus.DONE


# --------------------------------------------------------------------------- #
# §4.3 #2:改稿 → 片段作废
# --------------------------------------------------------------------------- #


def test_modified_json_invalidates_fragments(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    first = FakeProvider()
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, [CHAPTER], provider=first, voices=voices)
    assert queue.wait_idle(30)
    assert len(first.calls) == 3

    # 换稿子:同名章节内容变了 → json_sha256 变
    write_chapter(project.root, CHAPTER, [sentence(1, content="改过的内容")])
    reopened = Project.open(project.root, book="三体")

    second = FakeProvider()
    queue2 = SynthesisQueue()
    queue2.enqueue_chapters(reopened, [CHAPTER], provider=second, voices=voices)
    assert queue2.wait_idle(30)

    assert len(second.calls) == 1  # 新稿 1 条,重新生成
    assert (project.root / "audio" / ".stale" / f"{CHAPTER}.wav").is_file()
    assert (project.root / "audio" / f"{CHAPTER}.wav").is_file()


# --------------------------------------------------------------------------- #
# §4.3 #6:半截/字节数不符 → 重新生成
# --------------------------------------------------------------------------- #


def test_byte_mismatch_fragment_regenerated(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    manifest = project.manifest
    chapter = project.chapter(CHAPTER)
    manifest.get_or_create(
        CHAPTER, json_sha256=chapter.json_sha256, sentence_count=3, provider="fake"
    )
    # 记录 done 且文件存在,但字节数与记录不符 → 视为未完成
    manifest.save_fragment(
        CHAPTER, 1, FAKE_AUDIO, status=FragmentStatus.DONE,
        speaker="旁白", voice=DEFAULT_VOICE, provider="fake",
    )
    manifest.chapter(CHAPTER).fragments["0001"].bytes = 1
    manifest.save()

    fake = FakeProvider()
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert [c.text for c in fake.calls] == ["S1", "S2", "S3"]  # 1 被重新生成
    assert (project.root / "audio" / f"{CHAPTER}.wav").is_file()


def test_tiny_half_written_fragment_not_adopted(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=2)
    # 模拟半截写入:文件存在但不可解码(manifest 无记录)
    frag = project.manifest.fragment_path(CHAPTER, 1)
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_bytes(b"RIFF\x00\x00")  # 远小于最小字节数 / 不可解码

    fake = FakeProvider()
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert [c.text for c in fake.calls] == ["S1", "S2"]  # 半截文件不被采纳,重新生成


# --------------------------------------------------------------------------- #
# 孤儿片段(无记录但文件有效)→ 收养,不重生成
# --------------------------------------------------------------------------- #


def test_valid_orphan_fragment_adopted_not_regenerated(tmp_path: Path) -> None:
    project, voices = make_project(tmp_path, n=3)
    frag = project.manifest.fragment_path(CHAPTER, 1)
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_bytes(FAKE_AUDIO)  # 合法 canonical 片段,但 manifest 无记录

    fake = FakeProvider()
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert [c.text for c in fake.calls] == ["S2", "S3"]  # 1 被收养,不重生成
    rec = Manifest.load(project.root).chapter(CHAPTER)
    assert rec.status is ChapterStatus.DONE


# --------------------------------------------------------------------------- #
# §5.5:绑定变更 → 标 stale,不自动重做
# --------------------------------------------------------------------------- #


def test_binding_change_marks_stale_without_auto_redo(tmp_path: Path) -> None:
    # 用「有一条失败 → 无成品」的 partial 章节,进入 §5.4 第 4 步逐条对账分支。
    project, voices = make_project(tmp_path, n=2)
    _run_and_wait(project, voices, FakeProvider(fail_on={2}))
    assert not (project.root / "audio" / f"{CHAPTER}.wav").exists()
    assert (
        project.manifest.chapter(CHAPTER).fragments["0001"].status is FragmentStatus.DONE
    )

    # 建一个新音色并把「旁白」改绑到它 → 已完成片段应被标 stale(且不自动重做)
    voices.create("新音色", to_reference_audio(REF_AUDIO, max_seconds=1.0), "文本")
    assert project.bindings is not None
    project.bindings.set("旁白", "新音色")

    report = project.reconcile(
        provider="fake",
        provider_contract_version=1,
        voice_bindings={"旁白": "新音色"},
        persist=True,
    )
    action = report.action_for(CHAPTER)
    assert action.action == "stale"
    assert action.stale == [1]
    assert CHAPTER not in report.chapters_to_redo  # 不自动整章重做
    assert (
        project.manifest.chapter(CHAPTER).fragments["0001"].status is FragmentStatus.STALE
    )


def test_provider_mismatch_redo_after_resume(tmp_path: Path) -> None:
    """后端切换 → 整章重做(来源检查),旧成品移入 .stale。"""
    project, voices = make_project(tmp_path, n=3)
    _run_and_wait(project, voices, FakeProvider())

    # 人为把 manifest 的 provider 改成旧值,模拟「换后端后重开」
    manifest = project.manifest
    rec = manifest.chapter(CHAPTER)
    rec.provider = "old-provider"
    rec.provider_contract_version = 1
    rec.status = ChapterStatus.DONE
    manifest.save()

    reopened = Project.open(project.root, book="三体")
    fake = FakeProvider()
    queue = SynthesisQueue()
    queue.enqueue_chapters(reopened, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)

    assert len(fake.calls) == 3  # 整章重做
    assert (project.root / "audio" / ".stale" / f"{CHAPTER}.wav").is_file()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _run_and_wait(project: Project, voices: VoiceLibrary, fake: FakeProvider) -> None:
    queue = SynthesisQueue()
    queue.enqueue_chapters(project, [CHAPTER], provider=fake, voices=voices)
    assert queue.wait_idle(30)
