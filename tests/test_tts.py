"""TTS 模块离线单测(不碰真实后端)。

全部通过 `httpx.MockTransport` 拦截 HTTP;`sleep` 注入为记录器,不真正等待。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import wave

import httpx
import pytest

from src.config import ProviderConfig
from src.errors import ConfigError, FatalError, InvalidInputError, RetryableError
from src.models import CANONICAL_FORMAT, EmotionSpec, TTSRequest, VoiceRef
from src.tts.indextts_client import CAPABILITIES, PROVIDER_CONTRACT_VERSION, IndexttsClient
from src.tts.registry import PROVIDERS, create


def _make_wav(duration_ms: int = 100, sample_rate: int = 22050) -> bytes:
    """构造一段真实的小 wav(供 probe 时长解析使用;纯内存,无文件 IO)。"""
    frames = int(sample_rate * duration_ms / 1000)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


AUDIO_BYTES = _make_wav()  # 22050Hz/mono/16bit,100ms
VOICE = VoiceRef(audio=b"reference-audio-bytes")

PARAM_NAMES = {
    "max_text_tokens_per_sentence",
    "do_sample",
    "temperature",
    "top_p",
    "top_k",
    "num_beams",
    "repetition_penalty",
    "length_penalty",
    "max_mel_tokens",
}


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #


def _success_body(sample_rate: int = 22050, audio: bytes = AUDIO_BYTES) -> dict:
    return {
        "status": "success",
        "sample_rate": sample_rate,
        "audio": base64.b64encode(audio).decode("ascii"),
    }


def make_client(handler, *, retries=3, params=None, probe_voice=None):
    """构造一个走 MockTransport 的客户端,返回 (client, 请求列表, 退避列表)。"""
    recorded: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(recording_handler))
    delays: list[float] = []
    cfg = ProviderConfig(base_url="http://backend.test:8300", timeout=5.0, retries=retries)
    cfg.params = params or {}
    client = IndexttsClient(
        cfg, probe_voice=probe_voice, http_client=http, sleep=delays.append
    )
    return client, recorded, delays


def _capture_payload(emotion=None, params=None, cfg_params=None) -> dict:
    client, calls, _ = make_client(
        lambda r: httpx.Response(200, json=_success_body()), params=cfg_params
    )
    client.synthesize(TTSRequest(text="句子", voice_ref=VOICE, emotion=emotion, params=params))
    assert len(calls) == 1
    return json.loads(calls[0].content)


# --------------------------------------------------------------------------- #
# 响应解析 / 格式声明(架构 R7)
# --------------------------------------------------------------------------- #


def test_synthesize_success_declares_canonical_format():
    client, calls, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    result = client.synthesize(TTSRequest(text="你好", voice_ref=VOICE))
    assert result.audio == AUDIO_BYTES
    assert result.format == CANONICAL_FORMAT
    assert result.is_canonical is True


def test_non_canonical_sample_rate_is_declared_not_normalized():
    # tts/ 只声明格式,不做归一化(归一化归 synthesis/,架构 R7)
    body = _success_body(sample_rate=24000)
    client, _, _ = make_client(lambda r: httpx.Response(200, json=body))
    result = client.synthesize(TTSRequest(text="你好", voice_ref=VOICE))
    assert result.format.sample_rate == 24000
    assert result.is_canonical is False


def test_invalid_base64_audio_is_fatal():
    body = {"status": "success", "sample_rate": 22050, "audio": "###not-base64###"}
    client, calls, _ = make_client(lambda r: httpx.Response(200, json=body))
    with pytest.raises(FatalError) as excinfo:
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    # 数据损坏归因为 FatalError,而非输入错误
    assert not isinstance(excinfo.value, InvalidInputError)
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# 领域意图 → 报文映射(只在本模块内)
# --------------------------------------------------------------------------- #


def test_emotion_vectors_map_to_method_2():
    body = _capture_payload(EmotionSpec(vectors=[0.1] * 8))
    assert body["emo_control_method"] == 2
    assert body["emo_vector"] == pytest.approx([0.1] * 8)


def test_emotion_ref_audio_maps_to_method_1():
    body = _capture_payload(EmotionSpec(ref_audio=b"emo-bytes", weight=0.5))
    assert body["emo_control_method"] == 1
    assert base64.b64decode(body["emo_audio"]) == b"emo-bytes"
    assert body["emo_weight"] == 0.5


def test_emotion_text_maps_to_method_3():
    body = _capture_payload(EmotionSpec(text="高兴"))
    assert body["emo_control_method"] == 3
    assert body["emo_text"] == "高兴"


def test_no_emotion_omits_method_field():
    body = _capture_payload(None)
    assert "emo_control_method" not in body


def test_zero_vector_still_uses_method_2():
    body = _capture_payload(EmotionSpec(vectors=[0.0] * 8))
    assert body["emo_control_method"] == 2
    assert body["emo_vector"] == [0.0] * 8


# --------------------------------------------------------------------------- #
# 0.8 卡口(IndexTTS2 专有,放在适配器内)
# --------------------------------------------------------------------------- #


def test_emotion_vector_over_limit_scaled_to_0_8_and_warned(caplog):
    # sum = 1.2 应被缩放到 0.8,而非直发,也不报错
    with caplog.at_level(logging.WARNING, logger="src.tts.indextts_client"):
        body = _capture_payload(EmotionSpec(vectors=[0.15] * 8))
    vector = body["emo_vector"]
    assert body["emo_control_method"] == 2
    assert sum(vector) == pytest.approx(0.8)
    assert vector == pytest.approx([0.1] * 8)
    assert "0.8" in caplog.text
    assert "缩放" in caplog.text


def test_emotion_vector_within_limit_untouched():
    body = _capture_payload(EmotionSpec(vectors=[0.05] * 8))  # sum = 0.4
    assert body["emo_vector"] == pytest.approx([0.05] * 8)


def test_wrong_vector_dims_raise_invalid_input():
    client, calls, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    with pytest.raises(InvalidInputError):
        client.synthesize(
            TTSRequest(text="x", voice_ref=VOICE, emotion=EmotionSpec(vectors=[0.1] * 7))
        )
    assert calls == []  # 本地校验失败,不发请求


# --------------------------------------------------------------------------- #
# 参数转发(键名用请求体拼写 max_text_tokens_per_sentence)
# --------------------------------------------------------------------------- #


def test_params_merged_config_then_request():
    body = _capture_payload(
        params={"temperature": 1.1}, cfg_params={"temperature": 0.8, "num_beams": 5}
    )
    assert body["temperature"] == 1.1  # 本次请求覆盖配置
    assert body["num_beams"] == 5  # 配置默认值


def test_unknown_param_keys_are_not_forwarded():
    body = _capture_payload(params={"evil": 1, "top_k": 42})
    assert "evil" not in body
    assert body["top_k"] == 42


def test_payload_uses_request_field_spelling():
    body = _capture_payload(params={"max_text_tokens_per_sentence": 200})
    assert body["max_text_tokens_per_sentence"] == 200
    assert "max_text_tokens_per_segment" not in body


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #


def test_missing_reference_audio_raises_invalid_input():
    client, calls, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    with pytest.raises(InvalidInputError):
        client.synthesize(TTSRequest(text="x", voice_ref=VoiceRef(audio=None)))
    assert calls == []


def test_voice_id_mode_unsupported_raises_invalid_input():
    client, calls, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    with pytest.raises(InvalidInputError):
        client.synthesize(TTSRequest(text="x", voice_ref=VoiceRef(voice_id="v1")))
    assert calls == []


# --------------------------------------------------------------------------- #
# 分层重试
# --------------------------------------------------------------------------- #


def test_http_400_is_invalid_input_and_not_retried():
    client, calls, delays = make_client(
        lambda r: httpx.Response(400, json={"status": "error", "message": "bad"})
    )
    with pytest.raises(InvalidInputError) as excinfo:
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    # 归因为输入错误(API 层可映射成 400),但仍继承 FatalError,不重试
    assert isinstance(excinfo.value, FatalError)
    assert excinfo.value.detail.reason == "bad_request"
    assert len(calls) == 1
    assert delays == []


def test_other_4xx_is_invalid_input_and_not_retried():
    client, calls, delays = make_client(
        lambda r: httpx.Response(403, json={"status": "error", "message": "forbidden"})
    )
    with pytest.raises(InvalidInputError):
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    assert len(calls) == 1
    assert delays == []


def test_http_500_retries_with_exponential_backoff_then_raises():
    client, calls, delays = make_client(
        lambda r: httpx.Response(500, json={"status": "error", "message": "boom"}),
        retries=3,
    )
    with pytest.raises(RetryableError):
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    assert len(calls) == 4  # 1 次首发 + 3 次重试
    assert delays == [1.0, 2.0, 4.0]


def test_retry_recovers_on_eventual_success():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] < 2:
            return httpx.Response(500, json={"status": "error", "message": "boom"})
        return httpx.Response(200, json=_success_body())

    client, calls, delays = make_client(handler, retries=3)
    result = client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    assert result.audio == AUDIO_BYTES
    assert len(calls) == 2
    assert delays == [1.0]


def test_model_not_loaded_is_fatal_and_not_retried():
    client, calls, delays = make_client(
        lambda r: httpx.Response(
            500, json={"status": "error", "message": "模型未成功加载，无法处理请求"}
        )
    )
    with pytest.raises(FatalError) as excinfo:
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    assert not isinstance(excinfo.value, InvalidInputError)
    assert excinfo.value.detail.reason == "model_not_loaded"
    assert len(calls) == 1
    assert delays == []


def test_network_error_is_retryable_and_retried():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client, calls, delays = make_client(handler, retries=2)
    with pytest.raises(RetryableError) as excinfo:
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    assert excinfo.value.detail.reason == "network"
    assert len(calls) == 3
    assert delays == [1.0, 2.0]


def test_timeout_is_retryable_with_timeout_reason():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client, calls, delays = make_client(handler, retries=1)
    with pytest.raises(RetryableError) as excinfo:
        client.synthesize(TTSRequest(text="x", voice_ref=VOICE))
    assert excinfo.value.detail.reason == "timeout"
    assert delays == [1.0]


# --------------------------------------------------------------------------- #
# 能力 / 参数自描述
# --------------------------------------------------------------------------- #


def test_capabilities_match_design():
    caps = CAPABILITIES
    assert caps.emotion == {"vector", "ref_audio", "text"}
    assert caps.emotion_vector_dims == 8
    assert caps.voice_modes == {"audio_bytes"}
    assert caps.needs_prompt_text is False
    assert caps.supports_concurrent is False
    assert caps.returns_canonical is True


def test_param_schema_covers_nine_params_with_request_spelling():
    specs = {spec.name: spec for spec in CAPABILITIES.param_schema}
    assert set(specs) == PARAM_NAMES
    assert "max_text_tokens_per_sentence" in specs
    assert "max_text_tokens_per_segment" not in specs
    for spec in specs.values():
        assert spec.label
        assert spec.type in {"bool", "int", "float", "str", "enum"}
        assert spec.default is not None
    for name in ("temperature", "top_p", "max_text_tokens_per_sentence"):
        assert specs[name].minimum is not None
        assert specs[name].maximum is not None


def test_client_exposes_id_and_capabilities():
    client, _, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    assert client.id == "indextts2"
    assert client.capabilities is CAPABILITIES


def test_client_exposes_contract_version_on_instance():
    # 编排层从实例读取并写入 manifest.provider_contract_version;
    # 钉住「实例可访问」,防止将来有人把它退回成纯模块常量。
    client, _, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    assert client.contract_version == PROVIDER_CONTRACT_VERSION


def test_client_exposes_label_on_instance():
    # 展示名由 provider 自带,API 层不得硬编码 id→名称映射;
    # 同样钉住「实例可访问」。
    client, _, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    assert client.label == "IndexTTS2"


# --------------------------------------------------------------------------- #
# 注册表(加后端只改这里 +1 行)
# --------------------------------------------------------------------------- #


def test_registry_contains_indextts():
    assert PROVIDERS["indextts2"] is IndexttsClient


def test_registry_create_returns_provider():
    provider = create("indextts2", ProviderConfig(base_url="http://example.invalid:1"))
    try:
        assert isinstance(provider, IndexttsClient)
    finally:
        provider.close()


def test_registry_unknown_provider_raises_config_error():
    with pytest.raises(ConfigError):
        create("does-not-exist", ProviderConfig())


# --------------------------------------------------------------------------- #
# 测试连接(极短真实合成探测;此处用 mock)
# --------------------------------------------------------------------------- #


def test_probe_ok_returns_latency_sample_and_duration():
    client, calls, _ = make_client(
        lambda r: httpx.Response(200, json=_success_body()), probe_voice=VOICE
    )
    health = client.probe()
    assert health.ok is True
    assert health.kind == "ok"
    assert health.latency_ms is not None
    assert health.sample_audio == AUDIO_BYTES
    assert health.sample_rate == 22050
    assert health.duration_ms == 100  # AUDIO_BYTES 为 100ms 的 wav
    assert len(calls) == 1


def test_probe_ok_with_unparseable_audio_has_no_duration():
    body = {
        "status": "success",
        "sample_rate": 22050,
        "audio": base64.b64encode(b"not-a-wav").decode("ascii"),
    }
    client, _, _ = make_client(lambda r: httpx.Response(200, json=body), probe_voice=VOICE)
    health = client.probe()
    assert health.ok is True
    assert health.sample_rate == 22050
    assert health.duration_ms is None  # 解析不出时长不影响成功判定


def test_probe_without_voice_is_bad_request_without_http():
    client, calls, _ = make_client(lambda r: httpx.Response(200, json=_success_body()))
    health = client.probe()
    assert health.ok is False
    assert health.kind == "bad_request"
    assert health.sample_rate is None
    assert health.duration_ms is None
    assert calls == []


def test_probe_unreachable_and_does_not_retry():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client, calls, delays = make_client(handler, probe_voice=VOICE, retries=3)
    health = client.probe()
    assert health.kind == "unreachable"
    assert health.sample_rate is None
    assert health.duration_ms is None
    assert len(calls) == 1  # 探测单次,不重试
    assert delays == []


def test_probe_model_not_loaded():
    client, calls, _ = make_client(
        lambda r: httpx.Response(
            500, json={"status": "error", "message": "模型未成功加载，无法处理请求"}
        ),
        probe_voice=VOICE,
    )
    health = client.probe()
    assert health.kind == "model_not_loaded"
    assert len(calls) == 1


def test_probe_server_error():
    client, calls, _ = make_client(
        lambda r: httpx.Response(500, json={"status": "error", "message": "boom"}),
        probe_voice=VOICE,
    )
    health = client.probe()
    assert health.kind == "server_error"
    assert len(calls) == 1


def test_probe_bad_request():
    client, calls, _ = make_client(
        lambda r: httpx.Response(400, json={"status": "error", "message": "bad"}),
        probe_voice=VOICE,
    )
    health = client.probe()
    assert health.kind == "bad_request"
    assert len(calls) == 1
