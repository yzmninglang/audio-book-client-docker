"""M3 音色库离线单测(见 `doc/dev-workflow.md` §4.2,用真实素材)。

覆盖 `doc/voice/design.md` §3 的三种异常结构、§4 入库标准化、§7 命名安全。
全部离线,不碰网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.audio import probe
from src.errors import InvalidInputError, NotFoundError
from src.voice import VoiceEntry, VoiceLibrary, parse_voice_dir, validate_voice_name

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = REPO_ROOT / "reference" / "audio"

F_44K_STEREO = AUDIO_DIR / "丁仪" / "1.wav"


@pytest.fixture()
def library() -> VoiceLibrary:
    return VoiceLibrary(AUDIO_DIR)


# --------------------------------------------------------------------------- #
# 扫描真实音色库
# --------------------------------------------------------------------------- #


def test_scan_finds_37_voices(library: VoiceLibrary) -> None:
    entries = library.list()
    assert len(entries) == 37
    names = [e.name for e in entries]
    assert names == sorted(names)
    assert set(names) >= {"丁仪", "汪淼", "男旁白2", "独白", "标准女声"}


def test_wangmiao_has_six_emotion_refs(library: VoiceLibrary) -> None:
    entry = library.get("汪淼")
    assert set(entry.emo_refs) == {"不解", "叙述", "安慰", "提议", "猜测", "略带不解和不满，解释"}
    assert entry.main_audio.endswith("1.wav")
    for emo, path in entry.emo_refs.items():
        assert Path(path).is_file()
        assert Path(path).with_suffix(".txt").is_file()
        assert Path(path).name == f"{emo}.wav"


def test_spleeter_output_is_ignored(library: VoiceLibrary) -> None:
    """`output_spleeter/` 分离产物不得被当成音色或情感参考。"""
    entry = library.get("男旁白2")
    assert entry.emo_refs == {}
    assert "output_spleeter" not in entry.main_audio
    assert all("output_spleeter" not in p for p in entry.emo_refs.values())
    # 整个库中都不该出现分离产物路径
    for e in library.list():
        assert "output_spleeter" not in e.main_audio
        assert all("output_spleeter" not in p for p in e.emo_refs.values())


def test_subdir_separation_ignored_but_main_kept(library: VoiceLibrary) -> None:
    """`独白/1/`(含 vocals/accompaniment)被忽略,`独白/1.wav` 仍是主音频。"""
    entry = library.get("独白")
    assert Path(entry.main_audio).name == "1.wav"
    assert entry.emo_refs == {}
    assert not any(Path(p).parent.name == "1" for p in entry.emo_refs.values())


def test_copy_is_ignored(library: VoiceLibrary) -> None:
    entry = library.get("大佬齐静春")
    assert Path(entry.main_audio).name == "1.wav"
    assert entry.emo_refs == {}


def test_non_wav_extra_file_ignored(library: VoiceLibrary) -> None:
    """`宁摇` 除 1.wav 外还有一个无同名 txt 的 .mp3 → 不构成情感参考集。"""
    entry = library.get("宁摇")
    assert Path(entry.main_audio).name == "1.wav"
    assert entry.emo_refs == {}


def test_fake_wav_is_still_listed(library: VoiceLibrary) -> None:
    """`陈平安1/1.wav` 实为 MP3,但文件名是 `1` → 仍作为主参考音频。"""
    entry = library.get("陈平安1")
    assert Path(entry.main_audio).name == "1.wav"
    assert probe(Path(entry.main_audio).read_bytes()).format == "mp3"


def test_existing_voices_are_not_normalized(library: VoiceLibrary) -> None:
    assert library.get("丁仪").is_normalized is False


# --------------------------------------------------------------------------- #
# 解析规则:兜底 / 无效 / 副本
# --------------------------------------------------------------------------- #


def _touch_wav(path: Path, data: bytes = b"RIFF....WAVEfmt ") -> None:
    path.write_bytes(data)


def test_fallback_when_no_main_named_1(tmp_path: Path) -> None:
    d = tmp_path / "兜底音色"
    d.mkdir()
    _touch_wav(d / "beta.wav")
    _touch_wav(d / "alpha.wav")

    entry = parse_voice_dir(d)
    assert entry is not None
    assert Path(entry.main_audio).name == "alpha.wav"  # 排序第一个
    assert entry.warnings and "非标准结构" in entry.warnings[0]


def test_dir_without_any_audio_is_invalid(tmp_path: Path) -> None:
    d = tmp_path / "空音色"
    d.mkdir()
    (d / "readme.md").write_text("no audio", encoding="utf-8")
    assert parse_voice_dir(d) is None

    lib = VoiceLibrary(tmp_path)
    assert lib.list() == []
    with pytest.raises(NotFoundError):
        lib.get("空音色")


def test_copy_not_used_as_fallback(tmp_path: Path) -> None:
    d = tmp_path / "副本兜底"
    d.mkdir()
    _touch_wav(d / "1 - 副本.wav")
    _touch_wav(d / "real.wav")
    entry = parse_voice_dir(d)
    assert entry is not None
    assert Path(entry.main_audio).name == "real.wav"


def test_emotion_ref_requires_same_named_txt(tmp_path: Path) -> None:
    d = tmp_path / "情感要求"
    d.mkdir()
    _touch_wav(d / "1.wav")
    _touch_wav(d / "有文本.wav")
    (d / "有文本.txt").write_text("参考文本", encoding="utf-8")
    _touch_wav(d / "无文本.wav")

    entry = parse_voice_dir(d)
    assert entry is not None
    assert set(entry.emo_refs) == {"有文本"}


# --------------------------------------------------------------------------- #
# 命名安全(design §7)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "", "   ", ".", "..", "x\x00y"])
def test_reject_unsafe_names(bad: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_voice_name(bad)


def test_reject_overlong_name() -> None:
    with pytest.raises(InvalidInputError):
        validate_voice_name("长" * 200)  # 600 字节 > 255


def test_accept_chinese_name() -> None:
    assert validate_voice_name("汪淼") == "汪淼"
    assert validate_voice_name("略带不解和不满，解释") == "略带不解和不满，解释"


def test_library_ops_reject_traversal(library: VoiceLibrary) -> None:
    for bad in ("../evil", "a/b"):
        with pytest.raises(InvalidInputError):
            library.delete(bad)
        with pytest.raises(InvalidInputError):
            library.get(bad)


# --------------------------------------------------------------------------- #
# CRUD + 入库标准化(design §4)
# --------------------------------------------------------------------------- #


def _sample_audio() -> bytes:
    return F_44K_STEREO.read_bytes()  # 44.1kHz stereo → 应被降为 22050 mono


def test_create_normalizes_and_marks(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    entry = lib.create("测试音色", _sample_audio(), "参考文本")
    assert isinstance(entry, VoiceEntry)
    assert entry.name == "测试音色"
    assert entry.is_normalized is True
    assert entry.prompt_text == "参考文本"
    assert (tmp_path / "测试音色" / ".normalized").is_file()

    info = probe(Path(entry.main_audio).read_bytes())
    assert info.format == "wav"
    assert info.channels == 1
    assert info.sample_rate <= 22050
    assert info.duration <= 15.0 + 1e-3


def test_create_conflict(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("甲", _sample_audio(), None)
    with pytest.raises(InvalidInputError):
        lib.create("甲", _sample_audio(), None)


def test_update_replaces_audio_and_prompt(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("甲", _sample_audio(), "旧")
    entry = lib.update("甲", prompt_text="新")
    assert entry.prompt_text == "新"
    assert entry.is_normalized is True

    entry = lib.update("甲", prompt_text="")  # 空串清除
    assert entry.prompt_text is None


def test_rename_and_conflict(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("甲", _sample_audio(), None)
    lib.create("乙", _sample_audio(), None)

    lib.rename("甲", "丙")
    assert lib.exists("丙") and not lib.exists("甲")

    with pytest.raises(InvalidInputError):
        lib.rename("丙", "乙")  # 冲突,不静默覆盖
    assert lib.exists("丙") and lib.exists("乙")


def test_rename_missing(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    with pytest.raises(NotFoundError):
        lib.rename("不存在", "新")


def test_delete(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("甲", _sample_audio(), None)
    lib.delete("甲")
    assert not lib.exists("甲")
    with pytest.raises(NotFoundError):
        lib.delete("甲")


def test_reference_audio_roundtrip(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("甲", _sample_audio(), None)
    data = lib.reference_audio("甲")
    info = probe(data)
    assert info.format == "wav"
    assert info.channels == 1


def test_emotion_reference_audio(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("汪淼", _sample_audio(), None)
    # 手工补一个情感参考集(create 只处理主音频)
    ref = tmp_path / "汪淼" / "不解.wav"
    ref.write_bytes(_sample_audio())
    (tmp_path / "汪淼" / "不解.txt").write_text("x", encoding="utf-8")

    lib2 = VoiceLibrary(tmp_path)
    assert "不解" in lib2.get("汪淼").emo_refs
    assert lib2.emotion_reference_audio("汪淼", "不解")


def test_list_skips_hidden_and_invalid(tmp_path: Path) -> None:
    lib = VoiceLibrary(tmp_path)
    lib.create("有效", _sample_audio(), None)
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "无效").mkdir()
    (tmp_path / "无效" / "x.txt").write_text("x", encoding="utf-8")

    names = [e.name for e in lib.list()]
    assert names == ["有效"]
