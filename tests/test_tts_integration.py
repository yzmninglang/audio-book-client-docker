"""TTS 真实后端集成测试。

⚠️ **联调节制**:后端在公网、无鉴权、消耗真实 GPU。故:
    * 全部标记 `@pytest.mark.integration`(默认 `-m 'not integration'` 跳过)
    * 只用极短文本,严格串行
    * `retries=0`,每个测试最多 1 次真实请求;本文件总共 3 次请求(≤4)

运行:`uv run pytest tests/test_tts_integration.py -m integration -v -s`
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from src.config import DEFAULT_INDEXTTS_BASE_URL, ProviderConfig
from src.models import CANONICAL_FORMAT, EmotionSpec, TTSRequest, VoiceRef
from src.tts.indextts_client import IndexttsClient

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
# 标准女声/1.wav 是体积最小的参考音频(183KB)
REFERENCE_AUDIO = REPO_ROOT / "reference" / "audio" / "标准女声" / "1.wav"


def _load_voice() -> VoiceRef:
    if not REFERENCE_AUDIO.is_file():
        pytest.skip(f"缺少参考音频 fixture: {REFERENCE_AUDIO}")
    return VoiceRef(audio=REFERENCE_AUDIO.read_bytes())


def _make_client(**kwargs) -> IndexttsClient:
    cfg = ProviderConfig(base_url=DEFAULT_INDEXTTS_BASE_URL, timeout=300.0, retries=0)
    cfg.params = {}
    return IndexttsClient(cfg, **kwargs)


def _wav_info(audio: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(audio)) as handle:
        return handle.getframerate(), handle.getnchannels(), handle.getsampwidth()


def test_probe_returns_ok_with_latency_and_audio():
    client = _make_client(probe_voice=_load_voice())
    try:
        health = client.probe()
    finally:
        client.close()

    assert health.ok is True, health.message
    assert health.kind == "ok"
    assert health.latency_ms is not None
    assert health.sample_rate == 22050
    assert health.duration_ms is not None and health.duration_ms > 0
    assert health.sample_audio
    rate, channels, width = _wav_info(health.sample_audio)
    assert (rate, channels, width) == (22050, 1, 2)
    print(
        f"[probe] kind={health.kind} latency_ms={health.latency_ms} "
        f"sample_rate={rate} duration_ms={health.duration_ms}"
    )


def test_synthesize_short_text_returns_playable_canonical_wav():
    client = _make_client()
    try:
        result = client.synthesize(TTSRequest(text="测试。", voice_ref=_load_voice()))
    finally:
        client.close()

    assert result.audio[:4] == b"RIFF"
    assert result.format == CANONICAL_FORMAT
    assert result.is_canonical is True
    rate, channels, width = _wav_info(result.audio)
    assert (rate, channels, width) == (22050, 1, 2)
    print(f"[synthesize] bytes={len(result.audio)} sample_rate={rate} format={result.format}")


def test_synthesize_emotion_vector_sum_1_2_is_scaled_not_rejected():
    # sum=1.2 在 0.8~1.5 危险区:客户端应缩放后仍成功(而非 400)
    client = _make_client()
    try:
        result = client.synthesize(
            TTSRequest(
                text="测试。",
                voice_ref=_load_voice(),
                emotion=EmotionSpec(vectors=[0.15] * 8),  # sum = 1.2
            )
        )
    finally:
        client.close()

    assert result.audio[:4] == b"RIFF"
    assert result.is_canonical is True
    print(f"[synthesize+vector] bytes={len(result.audio)} sample_rate={result.format.sample_rate}")
