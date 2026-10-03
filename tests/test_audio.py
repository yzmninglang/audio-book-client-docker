"""M1 音频层离线单测(见 `doc/dev-workflow.md` §4.2)。

fixture 直接用仓库里的真实素材,它们天然覆盖所有异常形态:

    丁仪/1.wav       44.1kHz 立体声(需降采样)
    标准女声/1.wav   16kHz 单声道(**不得升采样**)
    陈平安1/1.wav    伪 wav(实为 MP3)

全部离线,不碰网络,不依赖 FFmpeg。
"""

from __future__ import annotations

import wave
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from src.audio import (
    AudioInfo,
    b64_and_estimate,
    b64_decode,
    concat,
    probe,
    silence,
    to_canonical,
    to_reference_audio,
)
from src.errors import FatalError, InvalidInputError
from src.models import CANONICAL_FORMAT

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIO_DIR = REPO_ROOT / "reference" / "audio"

F_44K_STEREO = AUDIO_DIR / "丁仪" / "1.wav"  # 44100Hz 2ch PCM_16 12.0s
F_16K_MONO = AUDIO_DIR / "标准女声" / "1.wav"  # 16000Hz 1ch PCM_16 5.85s
F_FAKE_MP3 = AUDIO_DIR / "陈平安1" / "1.wav"  # 伪 wav,实为 MP3 44.1kHz 2ch 8.0s


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def test_fixtures_exist() -> None:
    for p in (F_44K_STEREO, F_16K_MONO, F_FAKE_MP3):
        assert p.is_file(), f"缺少真实素材:{p}"


def test_probe_44100_stereo_wav() -> None:
    info = probe(F_44K_STEREO.read_bytes())
    assert info.format == "wav"
    assert info.sample_rate == 44100
    assert info.channels == 2
    assert info.bits == 16
    assert info.duration == pytest.approx(12.0, abs=0.05)


def test_probe_16000_mono_wav() -> None:
    info = probe(F_16K_MONO.read_bytes())
    assert info.format == "wav"
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.bits == 16
    assert info.duration == pytest.approx(5.85, abs=0.05)


def test_probe_sees_through_fake_wav() -> None:
    """核心判据:伪 wav 必须被识破为 MP3,而不是 WAV。"""
    info = probe(F_FAKE_MP3.read_bytes())
    assert info.format == "mp3"
    assert info.sample_rate == 44100
    assert info.channels == 2
    assert info.duration == pytest.approx(8.0, abs=0.1)


def test_probe_flags_extension_mismatch() -> None:
    info = probe(F_FAKE_MP3.read_bytes(), filename="1.wav")
    assert info.format == "mp3"
    assert info.is_actually_what_ext_says is False

    ok = probe(F_44K_STEREO.read_bytes(), filename="1.wav")
    assert ok.is_actually_what_ext_says is True


def test_probe_rejects_garbage() -> None:
    with pytest.raises(FatalError):
        probe(b"\x00\x01\x02\x03 not audio at all")
    with pytest.raises(FatalError):
        probe(b"")


# --------------------------------------------------------------------------- #
# convert:mono + 只降不升 + 裁剪
# --------------------------------------------------------------------------- #


def _wav_params(data: bytes) -> tuple[int, int, int, float]:
    with wave.open(BytesIO(data), "rb") as w:
        return (
            w.getframerate(),
            w.getnchannels(),
            w.getsampwidth() * 8,
            w.getnframes() / w.getframerate(),
        )


def test_convert_does_not_upsample_16k() -> None:
    """核心判据:16k 输入不得升采样,输出仍是 16000Hz。"""
    out = to_reference_audio(F_16K_MONO.read_bytes())
    rate, channels, bits, duration = _wav_params(out)
    assert rate == 16000
    assert channels == 1
    assert bits == 16
    assert duration == pytest.approx(5.85, abs=0.05)


def test_convert_downsamples_44100_stereo_to_22050_mono() -> None:
    out = to_reference_audio(F_44K_STEREO.read_bytes())
    rate, channels, bits, duration = _wav_params(out)
    assert rate == 22050
    assert channels == 1
    assert bits == 16
    assert duration == pytest.approx(12.0, abs=0.05)


def test_convert_mp3_to_canonical() -> None:
    out = to_reference_audio(F_FAKE_MP3.read_bytes())
    rate, channels, bits, duration = _wav_params(out)
    assert rate == 22050
    assert channels == 1
    assert bits == 16
    assert duration == pytest.approx(8.0, abs=0.1)
    assert probe(out).format == "wav"


def test_convert_crops_over_15s(tmp_path: Path) -> None:
    """超 15s 的输入被裁到 ≤15s。合成 20s 素材(离线)。"""
    sr = 44100
    t = np.linspace(0, 20.0, sr * 20, endpoint=False, dtype=np.float32)
    tone = 0.1 * np.sin(2 * np.pi * 220 * t)
    src = tmp_path / "long.wav"
    sf.write(str(src), tone, sr, format="WAV", subtype="PCM_16")

    assert probe(src.read_bytes()).duration == pytest.approx(20.0, abs=0.1)
    out = to_reference_audio(src.read_bytes())
    rate, channels, _bits, duration = _wav_params(out)
    assert rate == 22050
    assert channels == 1
    assert duration <= 15.0 + 1e-3
    assert duration == pytest.approx(15.0, abs=0.01)


def test_convert_respects_custom_limits() -> None:
    out = to_reference_audio(F_44K_STEREO.read_bytes(), max_seconds=3.0, max_sample_rate=8000)
    rate, _channels, _bits, duration = _wav_params(out)
    assert rate == 8000
    assert duration == pytest.approx(3.0, abs=0.02)


# --------------------------------------------------------------------------- #
# convert:to_canonical(硬规则 R7 归一化入口,与 to_reference_audio 刻意不同)
# --------------------------------------------------------------------------- #


def _canonical_wav() -> bytes:
    """构造一段已是标准格式(22050/1ch/16bit)的 WAV 字节。"""
    return to_reference_audio(F_44K_STEREO.read_bytes())


def test_to_canonical_passthrough_is_byte_identical() -> None:
    """已符合 CANONICAL_FORMAT:原样返回,零重编码。"""
    canonical = _canonical_wav()
    assert _wav_params(canonical)[:3] == (22050, 1, 16)

    # 无声明 → 探测后走快路径
    out, fmt = to_canonical(canonical)
    assert out is canonical or out == canonical
    assert fmt == CANONICAL_FORMAT

    # 有声明且等于标准 → 直接快路径
    out2, fmt2 = to_canonical(canonical, CANONICAL_FORMAT)
    assert out2 == canonical
    assert fmt2 == CANONICAL_FORMAT


def test_to_canonical_downsamples_44100_stereo() -> None:
    out, fmt = to_canonical(F_44K_STEREO.read_bytes())
    rate, channels, bits, _duration = _wav_params(out)
    assert (rate, channels, bits) == (22050, 1, 16)
    assert fmt == CANONICAL_FORMAT
    assert probe(out).format == "wav"


def test_to_canonical_upsamples_16000_mono() -> None:
    """与 to_reference_audio 刻意相反:16k **必须升到 22050**。"""
    out, fmt = to_canonical(F_16K_MONO.read_bytes())
    rate, channels, bits, duration = _wav_params(out)
    assert (rate, channels, bits) == (22050, 1, 16)
    assert fmt == CANONICAL_FORMAT
    assert duration == pytest.approx(5.85, abs=0.05)  # 不裁剪、时长保持


def test_to_canonical_mp3_input() -> None:
    out, fmt = to_canonical(F_FAKE_MP3.read_bytes())
    rate, channels, bits, _duration = _wav_params(out)
    assert (rate, channels, bits) == (22050, 1, 16)
    assert fmt == CANONICAL_FORMAT


def test_to_canonical_does_not_trim(tmp_path: Path) -> None:
    """归一化**不裁剪**:20s 输入仍是 20s(与 to_reference_audio 裁 15s 相反)。"""
    sr = 44100
    t = np.linspace(0, 20.0, sr * 20, endpoint=False, dtype=np.float32)
    src = tmp_path / "long20.wav"
    sf.write(str(src), 0.1 * np.sin(2 * np.pi * 220 * t), sr, format="WAV", subtype="PCM_16")

    out, _fmt = to_canonical(src.read_bytes())
    rate, _channels, _bits, duration = _wav_params(out)
    assert rate == 22050
    assert duration == pytest.approx(20.0, abs=0.05)


def test_to_canonical_rejects_lying_canonical_declaration() -> None:
    """防「provider 谎报」:声明 CANONICAL_FORMAT 但字节实为 MP3,不得透传。"""
    mp3 = F_FAKE_MP3.read_bytes()
    assert probe(mp3).format == "mp3"  # 确为 MP3,不是 WAV

    out, fmt = to_canonical(mp3, CANONICAL_FORMAT)  # 谎报已标准
    assert out != mp3  # 未被原样透传
    assert fmt == CANONICAL_FORMAT
    assert probe(out).format == "wav"  # 落回真实转换路径,产出真 WAV
    assert _wav_params(out)[:3] == (22050, 1, 16)


def test_canonical_vs_reference_differ_on_16k() -> None:
    """钉死二者差异:同一个 16k 输入,采样率必须一个 16000、一个 22050。"""
    data = F_16K_MONO.read_bytes()

    ref = to_reference_audio(data)
    can, _ctx = to_canonical(data)

    assert _wav_params(ref)[0] == 16000  # 入库:只降不升
    assert _wav_params(can)[0] == 22050  # 归一化:恰好到标准格式
    assert _wav_params(ref)[0] != _wav_params(can)[0]


# --------------------------------------------------------------------------- #
# segment:静音 + 拼接
# --------------------------------------------------------------------------- #


def test_silence_length_formula() -> None:
    assert silence(0, 22050, 1, 2) == b""
    assert silence(-5, 22050, 1, 2) == b""
    # 600ms @22050Hz mono 16bit -> 13230 样本 * 2 字节
    assert len(silence(600, 22050, 1, 2)) == 13230 * 2
    # 立体声翻倍
    assert len(silence(600, 22050, 2, 2)) == 13230 * 2 * 2
    assert silence(600, 22050, 1, 2) == b"\x00" * (13230 * 2)


def _fragments() -> tuple[list[bytes], AudioInfo]:
    """三个**同格式**(22050/1ch/16bit)片段 —— 拼接要求零重采样格式一致。

    注意:16k 素材经 `to_reference_audio` 后仍是 16000Hz(只降不升),
    故不能与 22050 片段混拼;这里统一取会降到 22050 的素材。
    """
    frags = [
        to_reference_audio(F_44K_STEREO.read_bytes(), max_seconds=2.0),
        to_reference_audio(F_FAKE_MP3.read_bytes(), max_seconds=2.0),
        to_reference_audio(F_44K_STEREO.read_bytes(), max_seconds=1.0),
    ]
    return frags, probe(frags[0])


def test_concat_duration_equals_sum_fragments_plus_delays() -> None:
    frags, template = _fragments()
    delays = [600, 400, 1200]

    out = concat(frags, delays, template=template)
    rate, channels, bits, duration = _wav_params(out)

    # 格式与输入一致
    assert (rate, channels, bits) == (
        template.sample_rate,
        template.channels,
        template.bits,
    )

    # 帧数精确 = Σ片段帧数 + Σdelay 帧数
    total_frames = 0
    for frag in frags:
        with wave.open(BytesIO(frag), "rb") as w:
            total_frames += w.getnframes()
    total_frames += sum(int(rate * d / 1000) for d in delays)

    with wave.open(BytesIO(out), "rb") as w:
        assert w.getnframes() == total_frames
    assert duration == pytest.approx(total_frames / rate, abs=1e-9)


def test_concat_no_fade_and_silence_is_zero() -> None:
    """拼接不得追加淡入淡出:段间静音区应恰好为数字零。"""
    frags, template = _fragments()
    out = concat(frags[:2], [600, 0], template=template)
    with wave.open(BytesIO(out), "rb") as w:
        frames = w.readframes(w.getnframes())
    with wave.open(BytesIO(frags[0]), "rb") as w:
        first_frames = w.getnframes()
    block = np.frombuffer(frames, dtype="<i2")
    # 第一段结束后的 600ms 静音必须全零(未叠加任何淡入淡出尾巴)
    silence_samples = int(template.sample_rate * 0.6)
    region = block[first_frames : first_frames + silence_samples]
    assert region.size == silence_samples
    assert np.all(region == 0)


def test_concat_rejects_length_mismatch() -> None:
    frags, template = _fragments()
    with pytest.raises(InvalidInputError):
        concat(frags, [100, 200], template=template)


def test_concat_rejects_non_wav_fragment() -> None:
    """concat 无需额外容器头校验:stdlib `wave` 解析即拒绝非 WAV 片段。"""
    _, template = _fragments()
    mp3 = F_FAKE_MP3.read_bytes()
    assert probe(mp3).format == "mp3"
    with pytest.raises(InvalidInputError):
        concat([mp3], [0], template=template)


def test_concat_rejects_format_mismatch() -> None:
    frags, template = _fragments()  # 均为 22050 mono
    bad = to_reference_audio(F_16K_MONO.read_bytes())  # 16000Hz,格式不同
    with pytest.raises(InvalidInputError):
        concat([bad], [0], template=template)


# --------------------------------------------------------------------------- #
# codec
# --------------------------------------------------------------------------- #


def test_codec_roundtrip() -> None:
    data = F_16K_MONO.read_bytes()
    text, est = b64_and_estimate(data)
    assert b64_decode(text) == data
    assert est == len(text)
    assert est >= len(data)  # base64 只会膨胀


def test_codec_rejects_bad_base64() -> None:
    with pytest.raises(InvalidInputError):
        b64_decode("not*valid*base64!!")


# --------------------------------------------------------------------------- #
# 零 FFmpeg / 零业务依赖(R1/R2 + 容器化约束 #6)
# --------------------------------------------------------------------------- #


def test_no_ffmpeg_or_pydub_in_source() -> None:
    """源码中不得出现对这些模块/封装的实际引用(注释里提到不算)。"""
    banned = ("import ffmpeg", "from ffmpeg", "import pydub", "from pydub")
    pkg = REPO_ROOT / "src" / "audio"
    for path in pkg.glob("*.py"):
        src = path.read_text(encoding="utf-8").lower()
        for needle in banned:
            assert needle not in src, f"{path.name} 越界引用了 {needle!r}"
        assert "import tts" not in src and "from tts" not in src, f"{path.name} 越界 import tts"
