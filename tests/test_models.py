"""M0:领域模型 —— 重点验证「没有后端专有概念」这条可不变量。

这是「新增 TTS 后端只需加一个适配器文件」的前提:
一旦 `emo_control_method`(IndexTTS 的模式枚举)或硬编码的 9 个生成参数
渗进领域层,所有后端就被迫适配 IndexTTS。
见 doc/tts/design.md §6。
"""

from __future__ import annotations

import dataclasses

import pytest

from src.errors import (
    AudiobookError,
    BindingError,
    FatalError,
    ManifestError,
    RetryableError,
)
from src.models import (
    CANONICAL_FORMAT,
    EMOTION_LABELS,
    EMOTION_VECTOR_DIMS,
    AudioFormat,
    Chapter,
    EmotionSpec,
    JobState,
    Sentence,
    TTSRequest,
    TTSResult,
    VoiceRef,
)

# --------------------------------------------------------------------------- #
# 领域纯净性
# --------------------------------------------------------------------------- #

BACKEND_SPECIFIC_NAMES = {
    "emo_control_method",  # IndexTTS 的模式枚举
    "emo_alpha",  # IndexTTS 的参数名
    "emo_audio_prompt",
    "max_text_tokens_per_segment",
    "max_mel_tokens",
    "top_k",
    "num_beams",
}

DOMAIN_TYPES = [VoiceRef, EmotionSpec, TTSRequest, TTSResult, Sentence, Chapter]


@pytest.mark.parametrize("cls", DOMAIN_TYPES)
def test_domain_types_have_no_backend_specific_fields(cls: type) -> None:
    fields = {f.name for f in dataclasses.fields(cls)}
    leaked = fields & BACKEND_SPECIFIC_NAMES
    assert not leaked, f"{cls.__name__} 泄漏了后端专有字段: {sorted(leaked)}"


def test_voiceref_carries_more_than_bare_bytes() -> None:
    """参考音色不能是裸 bytes —— 新引擎可能要参考文本或它自己的音色 ID。"""
    fields = {f.name for f in dataclasses.fields(VoiceRef)}

    assert {"audio", "prompt_text", "voice_id"} <= fields


def test_voiceref_is_empty_helper() -> None:
    assert VoiceRef().is_empty
    assert not VoiceRef(audio=b"\x00").is_empty
    assert not VoiceRef(voice_id="v1").is_empty


def test_emotionspec_is_intent_not_wire_format() -> None:
    """情感只表达意图(本子/参考音频/文本),不表达任何后端的模式编号。"""
    fields = {f.name for f in dataclasses.fields(EmotionSpec)}

    assert fields == {"vectors", "ref_audio", "text", "weight"}
    assert EmotionSpec().is_empty
    assert not EmotionSpec(vectors=[0.0] * 8).is_empty


def test_ttsresult_declares_its_own_format() -> None:
    """硬规则 R7:provider 声明输出格式,编排层据此决定是否归一化。"""
    fields = {f.name for f in dataclasses.fields(TTSResult)}

    assert {"audio", "format", "is_canonical"} <= fields
    assert TTSResult(audio=b"", format=CANONICAL_FORMAT).is_canonical is False


def test_ttsrequest_params_is_an_opaque_dict() -> None:
    """参数键名来自各 provider 自描述的 param_schema,领域层不认识具体键。"""
    req = TTSRequest(text="你好", voice_ref=VoiceRef(audio=b"x"), params={"whatever": 1})

    assert req.params == {"whatever": 1}


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #


def test_canonical_format_matches_measured_backend_output() -> None:
    """实测后端输出恒为 22050Hz/1ch/16bit,与参考音频无关。"""
    assert CANONICAL_FORMAT == AudioFormat(sample_rate=22050, channels=1, bits=16)


def test_emotion_vector_shape() -> None:
    assert EMOTION_VECTOR_DIMS == 8
    assert len(EMOTION_LABELS) == EMOTION_VECTOR_DIMS
    # 顺序与源数据一致,不可调整
    assert EMOTION_LABELS[0] == "喜"
    assert EMOTION_LABELS[-1] == "平静"


def test_audio_format_is_hashable_and_readable() -> None:
    assert str(CANONICAL_FORMAT) == "22050Hz/1ch/16bit"
    assert len({CANONICAL_FORMAT, AudioFormat(22050, 1, 16)}) == 1


# --------------------------------------------------------------------------- #
# 章节
# --------------------------------------------------------------------------- #


def test_chapter_counts_sentences() -> None:
    chapter = Chapter(
        name="P01_扉页",
        json_relpath="json/P01_扉页.json",
        sentences=[
            Sentence(index=1, speaker="旁白", content="扉页", delay_ms=600),
            Sentence(index=2, speaker="旁白", content="投资策略", delay_ms=800),
        ],
    )

    assert chapter.sentence_count == 2


def test_chapter_stores_relative_path_only() -> None:
    """容器化约束 #9:只存相对工程根的路径,换挂载点才不失效。"""
    chapter = Chapter(name="c", json_relpath="json/c.json")

    assert not chapter.json_relpath.startswith("/")


def test_sentence_defaults_are_permissive() -> None:
    """emo_vector 缺失按全零处理、delay 缺失按 0 —— 解析容错,不中断。"""
    sentence = Sentence(index=1, speaker="旁白", content="x")

    assert sentence.emo_vector is None
    assert sentence.delay_ms == 0


# --------------------------------------------------------------------------- #
# 异常体系
# --------------------------------------------------------------------------- #


def test_error_kinds_are_machine_readable() -> None:
    assert RetryableError("x").kind == "retryable"
    assert FatalError("x").kind == "fatal"
    assert BindingError("x").kind == "binding"


def test_manifest_error_is_fatal() -> None:
    """manifest 写失败不能继续合成,否则会造成静默损坏。"""
    assert issubclass(ManifestError, FatalError)
    assert not issubclass(ManifestError, RetryableError)


def test_error_to_dict() -> None:
    err = RetryableError("连不上", detail={"url": "http://x"})

    assert err.to_dict() == {
        "kind": "retryable",
        "message": "连不上",
        "detail": {"url": "http://x"},
    }


def test_all_errors_share_base() -> None:
    for cls in (RetryableError, FatalError, BindingError, ManifestError):
        assert issubclass(cls, AudiobookError)


def test_job_states() -> None:
    assert {s.value for s in JobState} == {"idle", "running", "paused", "stopped"}
