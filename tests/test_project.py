"""M3 工程模块离线单测(见 `doc/dev-workflow.md` §4.2)。

覆盖 `doc/project/design.md` §3 解析容错、§4 绑定、§5 续传台账(重点)、§6 工作区扫描。
全部离线,用真实 `book-json/P01_扉页.json` 与真实音频素材。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.audio import to_reference_audio
from src.errors import (
    BindingError,
    InvalidInputError,
    ManifestError,
    NotFoundError,
)
from src.models import Chapter, ChapterStatus, FragmentStatus, Sentence
from src.project import (
    BindingTable,
    FragmentEntry,
    Manifest,
    Project,
    Workspace,
    aggregate_roles,
    detect_binding_stale,
    ensure_project_layout,
    find_projects_using_voice,
    fragment_key,
    mark_binding_stale,
    parse_chapter,
    parse_raw_sentences,
)
from src.project.manifest import MANIFEST_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOK_JSON = REPO_ROOT / "book-json" / "P01_扉页.json"
F_44K_STEREO = REPO_ROOT / "reference" / "audio" / "丁仪" / "1.wav"

CHAPTER = "P01_扉页"
PROVIDER = "indextts2"


def _fragment_bytes() -> bytes:
    """一段真实可解码、字节数 > 1024 的片段(22050 mono,约 1s)。"""
    return to_reference_audio(F_44K_STEREO.read_bytes(), max_seconds=1.0)


def _sentence(i: int, speaker: str = "旁白", content: str | None = None) -> dict:
    return {
        "speaker": speaker,
        "content": content if content is not None else f"第{i}句",
        "emo_vector": [0.0] * 8,
        "delay": 100,
    }


def _write_chapter(root: Path, name: str, sentences: list[dict]) -> None:
    (root / "json" / f"{name}.json").write_text(
        json.dumps(sentences, ensure_ascii=False), encoding="utf-8"
    )


def _setup_project(tmp_path: Path, name: str = "三体", n: int = 3) -> Project:
    root = tmp_path / name
    Project.create(root, name=name)
    _write_chapter(root, CHAPTER, [_sentence(i) for i in range(n)])
    return Project.open(root, book=name)


# --------------------------------------------------------------------------- #
# §3 章节解析 + 角色聚合
# --------------------------------------------------------------------------- #


def test_parse_real_chapter() -> None:
    result = parse_chapter(BOOK_JSON)
    chapter = result.chapter
    assert chapter.name == CHAPTER
    assert chapter.sentence_count == 103
    assert result.warnings == []  # 真实样本应有 103 条、无容错告警
    assert sorted({s.speaker for s in chapter.sentences}) == ["旁白"]
    assert all(s.emo_vector == [0.0] * 8 for s in chapter.sentences)
    assert chapter.sentences[0].delay_ms == 600
    assert all(400 <= s.delay_ms <= 1200 for s in chapter.sentences)
    assert chapter.json_sha256 and len(chapter.json_sha256) == 64


def test_parse_chapter_relpath_is_relative() -> None:
    root = BOOK_JSON.parent.parent
    result = parse_chapter(BOOK_JSON, root=root)
    assert result.chapter.json_relpath == "book-json/P01_扉页.json"
    assert not result.chapter.json_relpath.startswith("/")


def test_parse_tolerance_keeps_indices_contiguous() -> None:
    raw = [
        _sentence(1),
        {"speaker": "旁白", "content": "   ", "emo_vector": [0.0] * 8, "delay": 100},  # 空 → 跳过
        {"speaker": "旁白", "content": "缺向量", "delay": 100},  # emo_vector 缺失
        {"speaker": "旁白", "content": "错维度", "emo_vector": [0.1, 0.2], "delay": 100},
        {"speaker": "旁白", "content": "负延迟", "emo_vector": [0.0] * 8, "delay": -50},
        "不是对象",
    ]
    sentences, warnings = parse_raw_sentences(raw, chapter_name="C")
    assert [s.index for s in sentences] == [1, 2, 3, 4]  # 空条目未占编号
    assert [s.content for s in sentences] == ["第1句", "缺向量", "错维度", "负延迟"]
    assert sentences[1].emo_vector == [0.0] * 8  # 缺失 → 全零
    assert sentences[2].emo_vector == [0.0] * 8  # 长度 ≠ 8 → 全零
    assert sentences[3].delay_ms == 0  # 负 → 归零
    assert len(warnings) == 5


def test_parse_chapter_top_level_must_be_list(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(InvalidInputError):
        parse_chapter(p)


def test_parse_chapter_missing_file() -> None:
    with pytest.raises(NotFoundError):
        parse_chapter("/nonexistent/x.json")


def test_aggregate_roles() -> None:
    chapters = [
        Chapter("a", "json/a.json", [Sentence(1, "甲", "x"), Sentence(2, "乙", "y")]),
        Chapter("b", "json/b.json", [Sentence(1, "乙", "z"), Sentence(2, "丙", "w")]),
    ]
    assert aggregate_roles(chapters) == ["丙", "乙", "甲"] or set(aggregate_roles(chapters)) == {
        "甲",
        "乙",
        "丙",
    }


# --------------------------------------------------------------------------- #
# §6 工作区扫描 + 工程创建
# --------------------------------------------------------------------------- #


def test_workspace_scan_and_create(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    assert ws.list_projects() == []

    ws.create("三体")
    (tmp_path / "不是工程").mkdir()  # 无 json/ → 不列出
    assert ws.list_projects() == ["三体"]
    assert ws.is_project("三体")

    for sub in ("json", "process_audio", "audio"):
        assert (tmp_path / "三体" / sub).is_dir()


def test_workspace_rejects_traversal(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    for bad in ("../evil", "a/b", ".."):
        with pytest.raises(InvalidInputError):
            ws.create(bad)


def test_project_create_and_open(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=2)
    assert project.name == "三体"
    assert project.chapter_names == [CHAPTER]
    assert project.roles == ["旁白"]
    assert (project.root / "binding.json").is_file()
    assert (project.root / ".manifest.json").is_file()


# --------------------------------------------------------------------------- #
# §4 绑定
# --------------------------------------------------------------------------- #


def test_binding_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "三体"
    ensure_project_layout(root)
    table = BindingTable(root)
    table.set("旁白", "男旁白2")
    table.set("汪淼", "汪淼", emo_ref="叙述")

    reloaded = BindingTable.load(root)
    assert reloaded.get("旁白").voice == "男旁白2"
    assert reloaded.get("汪淼").emo_ref == "叙述"
    assert set(reloaded.all()) == {"旁白", "汪淼"}


def test_binding_multi_role_share_voice(tmp_path: Path) -> None:
    root = tmp_path / "p"
    ensure_project_layout(root)
    table = BindingTable(root)
    table.set("旁白", "男旁白2")
    table.set("甲", "男旁白2")
    assert {b.voice for b in BindingTable.load(root).all().values()} == {"男旁白2"}


def test_binding_invalid_after_voice_removed(tmp_path: Path) -> None:
    root = tmp_path / "p"
    ensure_project_layout(root)
    table = BindingTable(root)
    table.set("旁白", "男旁白2")
    table.set("汪淼", "汪淼")

    invalid = table.invalid({"男旁白2"})  # 汪淼 音色已删除
    assert invalid == {"汪淼": "音色不存在: 汪淼"}


def test_binding_missing_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "p"
    ensure_project_layout(root)
    table = BindingTable.load(root)
    assert table.all() == {}


def test_binding_malformed_raises(tmp_path: Path) -> None:
    root = tmp_path / "p"
    ensure_project_layout(root)
    (root / "binding.json").write_text('{"version": 2, "bindings": {}}', encoding="utf-8")
    with pytest.raises(BindingError):
        BindingTable.load(root)


def test_project_validate_bindings(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    with pytest.raises(BindingError):
        project.validate_bindings({"男旁白2"})  # 旁白 未绑定

    project.bindings.set("旁白", "男旁白2")
    project.validate_bindings({"男旁白2"})  # 通过

    status = project.binding_status({"男旁白2"})
    assert status == {"旁白": ""}

    project.bindings.set("旁白", "不存在的音色")
    with pytest.raises(BindingError):
        project.validate_bindings({"男旁白2"})


def test_find_projects_using_voice(tmp_path: Path) -> None:
    a = _setup_project(tmp_path, name="甲书", n=1)
    _setup_project(tmp_path, name="乙书", n=1)
    a.bindings.set("旁白", "男旁白2")

    assert find_projects_using_voice(tmp_path, "男旁白2") == ["甲书"]
    assert find_projects_using_voice(tmp_path, "独白") == []


# --------------------------------------------------------------------------- #
# §5 manifest:读写 / 原子写 / 序列化
# --------------------------------------------------------------------------- #


def test_fragment_key_width() -> None:
    assert fragment_key(1) == "0001"
    assert fragment_key(12345) == "12345"  # 溢出到 5 位仍零填充


def test_manifest_load_missing_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "三体"
    ensure_project_layout(root)
    manifest = Manifest.load(root)
    assert manifest.book == "三体"
    assert manifest.chapters == {}
    assert manifest.to_dict()["version"] == MANIFEST_VERSION


def test_manifest_save_load_roundtrip(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=2)
    manifest = project.manifest
    rec = manifest.get_or_create(
        CHAPTER,
        json_sha256="deadbeef",
        sentence_count=2,
        provider=PROVIDER,
        provider_contract_version=1,
    )
    rec.status = ChapterStatus.DONE
    rec.output = manifest.chapter_audio_relpath(CHAPTER)
    manifest.set_chapter(rec)
    manifest.save()

    reloaded = Manifest.load(project.root)
    got = reloaded.chapter(CHAPTER)
    assert got.json_sha256 == "deadbeef"
    assert got.sentence_count == 2
    assert got.status is ChapterStatus.DONE
    assert got.provider == PROVIDER
    assert got.output == f"audio/{CHAPTER}.wav"


def test_manifest_corrupt_raises(tmp_path: Path) -> None:
    root = tmp_path / "p"
    ensure_project_layout(root)
    (root / ".manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError):
        Manifest.load(root)


def test_manifest_write_failure_raises(tmp_path: Path) -> None:
    root = tmp_path / "p"
    ensure_project_layout(root)
    (root / ".manifest.json").mkdir()  # 用目录占位 → os.replace 失败
    with pytest.raises(ManifestError):
        Manifest(root, "p").save()


def test_manifest_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    project.manifest.save()
    assert not (project.root / ".manifest.json.tmp").exists()


def test_save_fragment_writes_file_and_sha256_on_failure(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    manifest = project.manifest
    manifest.get_or_create(
        CHAPTER, json_sha256=project.chapter(CHAPTER).json_sha256, sentence_count=1
    )

    entry = manifest.save_fragment(
        CHAPTER, 1, None, status=FragmentStatus.FAILED, speaker="旁白",
        voice="男旁白2", attempts=3, error="HTTP 500",
    )
    assert entry.status is FragmentStatus.FAILED
    assert entry.sha256 is None  # 无数据 → 无 sha

    done = manifest.save_fragment(
        CHAPTER, 1, _fragment_bytes(), status=FragmentStatus.DONE, speaker="旁白",
        voice="男旁白2", attempts=1, provider=PROVIDER,
    )
    assert done.sha256 is not None  # 重试过 → 额外存 sha256
    assert manifest.fragment_path(CHAPTER, 1).is_file()

    raw = json.loads((project.root / ".manifest.json").read_text(encoding="utf-8"))
    assert raw["chapters"][CHAPTER]["fragments"]["0001"]["sha256"] == done.sha256


def test_manifest_only_stores_relative_paths(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    manifest = project.manifest
    rec = manifest.get_or_create(
        CHAPTER, json_sha256=project.chapter(CHAPTER).json_sha256, sentence_count=1
    )
    rec.output = manifest.chapter_audio_relpath(CHAPTER)
    manifest.save_fragment(CHAPTER, 1, _fragment_bytes(), provider=PROVIDER)

    raw = json.loads((project.root / ".manifest.json").read_text(encoding="utf-8"))
    assert raw["chapters"][CHAPTER]["output"] == f"audio/{CHAPTER}.wav"
    text = json.dumps(raw, ensure_ascii=False)
    assert '"output": "/' not in text
    assert manifest.fragment_relpath(CHAPTER, 1) == f"process_audio/{CHAPTER}/0001.wav"


# --------------------------------------------------------------------------- #
# §5.4 启动对账
# --------------------------------------------------------------------------- #


def test_reconcile_missing_file_regenerates(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    project.manifest.record_fragment(
        CHAPTER,
        FragmentEntry(
            index=1, status=FragmentStatus.DONE, bytes=9999,
            speaker="旁白", voice="男旁白2",
        ),
        persist=True,
    )

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    assert 1 in report.todo_indices[CHAPTER]  # 记录 done 但文件缺失 → 重生成


def test_save_fragment_data_none_infers_bytes_from_disk(tmp_path: Path) -> None:
    """回归:两步式写法(先落盘再 `save_fragment(data=None)` 记账)不得把续传打回原点。

    旧缺陷:`save_fragment(data=None)` 把 `bytes` 记为 `None`,
    对账 `frag.bytes == size` 恒假 → 已完成片段被判未完成 → 续传全部重做。
    """
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    manifest = project.manifest

    # 两步式:先原子落盘,再只记账(data=None)
    rel = manifest.write_fragment_bytes(CHAPTER, 1, _fragment_bytes())
    assert rel == f"process_audio/{CHAPTER}/0001.wav"
    entry = manifest.save_fragment(
        CHAPTER, 1, None, status=FragmentStatus.DONE,
        speaker="旁白", voice="男旁白2", provider=PROVIDER,
    )

    size = manifest.fragment_path(CHAPTER, 1).stat().st_size
    assert size > 0
    assert entry.bytes == size  # 从磁盘推断,而非 None

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    assert 1 not in report.todo_indices.get(CHAPTER, [])  # 不被重做
    assert project.manifest.chapter(CHAPTER).fragments["0001"].bytes == size


def test_reconcile_byte_mismatch_regenerates(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    project.manifest.save_fragment(
        CHAPTER, 1, _fragment_bytes(), status=FragmentStatus.DONE,
        speaker="旁白", voice="男旁白2", provider=PROVIDER,
    )
    # 篡改记录的字节数 → 与磁盘不符
    project.manifest.chapter(CHAPTER).fragments["0001"].bytes = 1

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    assert 1 in report.todo_indices[CHAPTER]


def test_reconcile_adopts_orphan_file(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    # 直接落一个合法片段文件,但 manifest 无记录
    frag = project.manifest.fragment_path(CHAPTER, 1)
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_bytes(_fragment_bytes())

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    action = report.action_for(CHAPTER)
    assert action.adopted == [1]
    assert action.todo == []
    recorded = project.manifest.chapter(CHAPTER).fragments["0001"]
    assert recorded.status is FragmentStatus.DONE
    assert recorded.bytes == frag.stat().st_size


def test_reconcile_rejects_tiny_orphan_file(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    frag = project.manifest.fragment_path(CHAPTER, 1)
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_bytes(b"RIFF")  # 太小 / 不可解码 → 不补记

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    assert report.action_for(CHAPTER).adopted == []
    assert 1 in report.todo_indices[CHAPTER]


def test_reconcile_hash_change_invalidates_fragments(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    project.manifest.save_fragment(
        CHAPTER, 1, _fragment_bytes(), status=FragmentStatus.DONE,
        speaker="旁白", voice="男旁白2", provider=PROVIDER,
    )
    assert list(project.manifest.fragment_dir(CHAPTER).glob("*.wav"))

    # 换稿子:同名章节内容变了 → json_sha256 变
    _write_chapter(project.root, CHAPTER, [_sentence(1, content="改了内容")])
    reopened = Project.open(project.root, book=project.name)
    report = reopened.reconcile(provider=PROVIDER, provider_contract_version=1)

    action = report.action_for(CHAPTER)
    assert action.action == "redo"
    assert reopened.manifest.chapter(CHAPTER).fragments == {}
    assert not list(reopened.manifest.fragment_dir(CHAPTER).glob("*.wav"))


def test_reconcile_whole_chapter_done_skips(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    audio = project.root / "audio" / f"{CHAPTER}.wav"
    audio.write_bytes(_fragment_bytes())

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    action = report.action_for(CHAPTER)
    assert action.action == "skip"
    rec = project.manifest.chapter(CHAPTER)
    assert rec.status is ChapterStatus.DONE
    assert rec.output == f"audio/{CHAPTER}.wav"
    assert not (project.root / "process_audio" / CHAPTER).exists()


def test_reconcile_provider_mismatch_redoes_and_stales(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    chapter = project.chapter(CHAPTER)
    manifest = project.manifest
    manifest.get_or_create(
        CHAPTER,
        json_sha256=chapter.json_sha256,
        sentence_count=1,
        provider="old-provider",
        provider_contract_version=9,
    )
    audio = project.root / "audio" / f"{CHAPTER}.wav"
    audio.write_bytes(_fragment_bytes())
    manifest.save()

    report = project.reconcile(provider=PROVIDER, provider_contract_version=1)
    action = report.action_for(CHAPTER)
    assert action.action == "redo"
    assert "来源不一致" in action.reason
    assert not audio.exists()
    assert (project.root / "audio" / ".stale" / f"{CHAPTER}.wav").is_file()


def test_reconcile_binding_change_marks_stale(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    project.manifest.save_fragment(
        CHAPTER, 1, _fragment_bytes(), status=FragmentStatus.DONE,
        speaker="旁白", voice="男旁白2", provider=PROVIDER,
    )

    report = project.reconcile(
        provider=PROVIDER, provider_contract_version=1, voice_bindings={"旁白": "独白"}
    )
    action = report.action_for(CHAPTER)
    assert action.action == "stale"
    assert action.stale == [1]
    assert project.manifest.chapter(CHAPTER).fragments["0001"].status is FragmentStatus.STALE


def test_detect_binding_stale_is_read_only(tmp_path: Path) -> None:
    """只读计算:detect_binding_stale 不得改写任何状态(供工程详情 GET 使用)。"""
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    project.manifest.save_fragment(
        CHAPTER, 1, _fragment_bytes(), status=FragmentStatus.DONE,
        speaker="旁白", voice="男旁白2", provider=PROVIDER,
    )
    rec = project.manifest.chapter(CHAPTER)
    status_before = rec.status

    changed = detect_binding_stale(project.manifest, {"旁白": "独白"})
    assert changed == {CHAPTER: [1]}
    # 未改写状态 → 同一请求内 done 计数不受影响
    assert rec.fragments["0001"].status is FragmentStatus.DONE
    assert rec.status is status_before


def test_mark_binding_stale_mutates_and_can_be_read_only(tmp_path: Path) -> None:
    project = _setup_project(tmp_path, n=1)
    project.reconcile(provider=PROVIDER, provider_contract_version=1)
    project.manifest.save_fragment(
        CHAPTER, 1, _fragment_bytes(), status=FragmentStatus.DONE,
        speaker="旁白", voice="男旁白2", provider=PROVIDER,
    )

    # mutate=False → 只读,不改写
    changed = mark_binding_stale(project.manifest, {"旁白": "独白"}, mutate=False)
    assert changed == {CHAPTER: [1]}
    assert project.manifest.chapter(CHAPTER).fragments["0001"].status is FragmentStatus.DONE

    # 默认 mutate=True → 就地改写
    mark_binding_stale(project.manifest, {"旁白": "独白"})
    assert project.manifest.chapter(CHAPTER).fragments["0001"].status is FragmentStatus.STALE
    assert project.manifest.chapter(CHAPTER).status is ChapterStatus.PARTIAL
