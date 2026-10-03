"""IndexTTS2 `/api/tts` 适配器。

> ⚠️ 本文件是**唯一**允许出现 IndexTTS 后端专有概念的地方:字段名、模式枚举
> (`emo_control_method` 0/1/2/3)、0.8 情感阈值、9 个生成参数。
> 这些细节严禁外泄到领域模型、编排层或前端(见 doc/tts/design.md §1、§2)。

职责:领域意图 → 后端报文 → 领域语义。
本适配器只吃字节、只吐字节,不读文件、不碰文件系统(架构 R3)。
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from typing import Any

import httpx

from ..config import AuthConfig, ProviderConfig
from ..errors import FatalError, InvalidInputError
from ..models import (
    CANONICAL_FORMAT,
    EMOTION_VECTOR_DIMS,
    AudioFormat,
    EmotionSpec,
    HealthResult,
    TTSRequest,
    TTSResult,
    VoiceRef,
)
from .capabilities import ParamSpec, TTSCapabilities
from .probe import run_probe
from .retry import (
    REASON_DECODE,
    ErrorDetail,
    call_with_retry,
    classify_http_error,
    classify_transport_error,
)

logger = logging.getLogger(__name__)

ENDPOINT_PATH = "/api/tts"
PROVIDER_ID = "indextts2"
PROVIDER_LABEL = "IndexTTS2"
PROVIDER_CONTRACT_VERSION = 1

# 仅 IndexTTS2 专有:情感向量之和的模型预期上限。
# ui-api.py 实际只拒 >1.5,而模型 normalize_emo_vec 期望 ≤0.8;
# 0.8~1.5 是「后端收下但超出模型预期」的危险区,故在适配器内先缩放。
# (见 doc/tts/design.md §3、doc/findings.md §5)
EMOTION_VECTOR_SUM_LIMIT = 0.8

# emo_control_method 枚举(领域层不可见)
_METHOD_SAME_AS_VOICE = 0
_METHOD_REF_AUDIO = 1
_METHOD_VECTOR = 2
_METHOD_TEXT = 3

# 9 个高级参数的自描述。键名用**请求体拼写** `max_text_tokens_per_sentence`
# (模型内部叫 ..._segment,映射时不可写错 —— 见 doc/tts/design.md §2.1)。
# min/max 为设置页的合理取值区间(后端本身不校验),仅作前端渲染提示。
_PARAM_SPECS: list[ParamSpec] = [
    ParamSpec(
        name="max_text_tokens_per_sentence",
        label="每句最大文本 Token 数",
        type="int",
        default=120,
        minimum=1,
        maximum=600,
    ),
    ParamSpec(name="do_sample", label="采样生成", type="bool", default=True),
    ParamSpec(
        name="temperature", label="温度", type="float", default=0.8, minimum=0.0, maximum=2.0
    ),
    ParamSpec(name="top_p", label="Top-P", type="float", default=0.8, minimum=0.0, maximum=1.0),
    ParamSpec(name="top_k", label="Top-K", type="int", default=30, minimum=0, maximum=200),
    ParamSpec(name="num_beams", label="Beam 数", type="int", default=3, minimum=1, maximum=10),
    ParamSpec(
        name="repetition_penalty",
        label="重复惩罚",
        type="float",
        default=10.0,
        minimum=0.0,
        maximum=20.0,
    ),
    ParamSpec(
        name="length_penalty",
        label="长度惩罚",
        type="float",
        default=0.0,
        minimum=-2.0,
        maximum=2.0,
    ),
    ParamSpec(
        name="max_mel_tokens", label="最大 Mel Token 数", type="int", default=1500, minimum=100,
        maximum=4000,
    ),
]

_PARAM_BY_NAME: dict[str, ParamSpec] = {spec.name: spec for spec in _PARAM_SPECS}

CAPABILITIES = TTSCapabilities(
    emotion={"vector", "ref_audio", "text"},
    emotion_vector_dims=EMOTION_VECTOR_DIMS,  # 8
    voice_modes={"audio_bytes"},
    needs_prompt_text=False,
    supports_concurrent=False,  # 后端无锁、无并发保护(见 ui-api.py 全文)
    returns_canonical=True,  # 实测输出恒 22050Hz/16bit/mono
    param_schema=_PARAM_SPECS,
)


class IndexttsClient:
    """IndexTTS2 `/api/tts` 的同步适配器。

    `probe_voice` 是「测试连接」用的已入库音色字节(R3:字节由调用方提供,
    本模块不读文件)。不配置时 `probe()` 返回 `bad_request`。
    """

    id = PROVIDER_ID
    # 展示名(设置页下拉/标题用)。由 provider 自己提供,API 层不得硬编码
    # id→名称映射,否则「加后端只改 tts/」的承诺会被打破。
    label: str = PROVIDER_LABEL
    # 契约版本:必须作为**实例可访问**属性暴露,编排层据此写入 manifest
    # 的 provider_contract_version(版本变化 → 已生成片段整章重做)。
    # 直接引用模块常量,不重复字面量,避免两处漂移。
    contract_version: int = PROVIDER_CONTRACT_VERSION

    def __init__(
        self,
        config: ProviderConfig,
        *,
        probe_voice: VoiceRef | None = None,
        http_client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._config = config
        self.base_url = config.base_url.rstrip("/")
        self.timeout = config.timeout
        self.retries = config.retries
        # auth 为预留字段,本次不使用(见 doc/findings.md §7)。
        self.auth: AuthConfig = config.auth
        self.params: dict[str, Any] = dict(config.params)
        self._probe_voice = probe_voice
        self._sleep = sleep
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self.timeout)

    # ------------------------------------------------------------------ #
    # 契约
    # ------------------------------------------------------------------ #

    @property
    def capabilities(self) -> TTSCapabilities:
        return CAPABILITIES

    def synthesize(self, req: TTSRequest) -> TTSResult:
        """合成一条。网络/5xx 按配置退避重试;400 立即失败。"""
        return call_with_retry(
            lambda: self._synthesize_once(req),
            retries=self.retries,
            sleep=self._sleep,
        )

    def probe(self) -> HealthResult:
        """测试连接 = 一次极短文本的真实合成(单次,不重试)。"""
        return run_probe(self._synthesize_once, self._probe_voice)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> IndexttsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 单次调用(供 synthesize 重试与 probe 复用)
    # ------------------------------------------------------------------ #

    def _synthesize_once(self, req: TTSRequest) -> TTSResult:
        payload = self._build_payload(req)
        url = f"{self.base_url}{ENDPOINT_PATH}"
        try:
            response = self._client.post(url, json=payload)
        except (httpx.HTTPError, OSError) as exc:
            raise classify_transport_error(exc) from exc

        if response.status_code != 200:
            raise classify_http_error(response.status_code, _extract_message(response))

        return self._parse_response(response)

    # ------------------------------------------------------------------ #
    # 请求构造
    # ------------------------------------------------------------------ #

    def _build_payload(self, req: TTSRequest) -> dict[str, Any]:
        voice_ref = req.voice_ref
        if voice_ref is None or voice_ref.audio is None:
            # IndexTTS2 只支持 audio_bytes 音色模式(capabilities.voice_modes)。
            if voice_ref is not None and voice_ref.voice_id is not None:
                raise InvalidInputError("IndexTTS2 不支持 voice_id 音色模式,需要参考音频字节")
            raise InvalidInputError("缺少参考音频(prompt_audio)字节")

        payload: dict[str, Any] = {
            "tts_text": req.text,
            "prompt_audio": base64.b64encode(voice_ref.audio).decode("ascii"),
        }
        payload.update(self._emotion_fields(req.emotion))
        payload.update(self._param_fields(req.params))
        return payload

    def _emotion_fields(self, emotion: EmotionSpec | None) -> dict[str, Any]:
        """领域情感意图 → emo_control_method 报文。映射只在此函数内。"""
        if emotion is None or emotion.is_empty:
            # 不发送任何情感字段 → 后端默认 method=0(与音色相同)。
            return {}

        if emotion.vectors is not None:
            vector = self._normalize_vector(emotion.vectors)
            return {"emo_control_method": _METHOD_VECTOR, "emo_vector": vector}

        if emotion.ref_audio is not None:
            return {
                "emo_control_method": _METHOD_REF_AUDIO,
                "emo_audio": base64.b64encode(emotion.ref_audio).decode("ascii"),
                "emo_weight": emotion.weight,
            }

        if emotion.text is not None:
            return {"emo_control_method": _METHOD_TEXT, "emo_text": emotion.text}

        return {}

    def _normalize_vector(self, vectors: list[float]) -> list[float]:
        """IndexTTS2 专有的 0.8 卡口。

        `ui-api.py` 实际只拒 >1.5,但模型期望 ≤0.8;0.8~1.5 会被后端照单全收却
        超出模型预期。此处按 `0.8/sum` 缩放并告警,**不**直发也不报错。
        全零向量(或 sum ≤ 0.8)原样透传。
        """
        if len(vectors) != EMOTION_VECTOR_DIMS:
            raise InvalidInputError(
                f"情感向量必须为 {EMOTION_VECTOR_DIMS} 维,收到 {len(vectors)} 维"
            )

        total = float(sum(vectors))
        if total > EMOTION_VECTOR_SUM_LIMIT:
            logger.warning(
                "情感向量之和 %.4f 超出模型预期 %.1f,已按 %.1f/sum 缩放",
                total,
                EMOTION_VECTOR_SUM_LIMIT,
                EMOTION_VECTOR_SUM_LIMIT,
            )
            scale = EMOTION_VECTOR_SUM_LIMIT / total
            return [float(x) * scale for x in vectors]
        return [float(x) for x in vectors]

    def _param_fields(self, params: dict[str, Any] | None) -> dict[str, Any]:
        """配置默认值 < 本次请求参数;只转发 param_schema 覆盖的 9 个键。"""
        merged = dict(self.params)
        if params:
            merged.update(params)

        fields: dict[str, Any] = {}
        for spec in _PARAM_SPECS:
            if spec.name in merged:
                fields[spec.name] = spec.coerce(merged[spec.name])
        return fields

    # ------------------------------------------------------------------ #
    # 响应解析
    # ------------------------------------------------------------------ #

    def _parse_response(self, response: httpx.Response) -> TTSResult:
        try:
            data = response.json()
        except ValueError as exc:
            raise FatalError(
                f"后端响应不是合法 JSON: {exc}",
                detail=ErrorDetail(REASON_DECODE, response.status_code, str(exc)),
            ) from exc

        if not isinstance(data, dict) or data.get("status") != "success":
            message = str(data.get("message")) if isinstance(data, dict) else str(data)
            # 200 但 status != success:按服务端错误处理。
            raise FatalError(
                f"后端未返回成功状态: {message}",
                detail=ErrorDetail(REASON_DECODE, response.status_code, message),
            )

        audio_b64 = data.get("audio")
        if not audio_b64:
            raise FatalError(
                "后端响应缺少 audio 字段",
                detail=ErrorDetail(REASON_DECODE, response.status_code, "missing audio"),
            )
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            # 音频解码失败是数据问题,重试无意义。
            raise FatalError(
                f"后端返回的音频 base64 解码失败: {exc}",
                detail=ErrorDetail(REASON_DECODE, response.status_code, str(exc)),
            ) from exc

        sample_rate = int(data.get("sample_rate") or CANONICAL_FORMAT.sample_rate)
        fmt = AudioFormat(sample_rate=sample_rate, channels=1, bits=16)
        return TTSResult(
            audio=audio,
            format=fmt,
            is_canonical=(fmt == CANONICAL_FORMAT),
        )


def _extract_message(response: httpx.Response) -> str:
    """从错误响应里尽力取出 message;取不到则回退到原始文本。"""
    try:
        data = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(data, dict):
        return str(data.get("message") or data)
    return str(data)
