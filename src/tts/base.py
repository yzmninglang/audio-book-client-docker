"""TTSProvider 抽象与契约。

本模块是 `tts/` 的公共门面:对外只暴露领域模型 + Provider 契约,
适配器内部实现(`indextts_client.py`)的后端专有细节不得越过这一层。

契约(所有适配器必须遵守):

    * `id: str`                          —— 与 `registry.PROVIDERS` 的键一致
    * `label: str`                       —— 展示名(如 "IndexTTS2"),**须实例可访问**
          - 供设置页下拉与标题使用;由 provider 自己提供,
            API 层不得硬编码 id→名称映射,否则「加后端只改 tts/」的承诺被打破
    * `contract_version: int`            —— 后端报文的契约版本,**须作为实例属性可访问**
          - 编排层把它写进 manifest 的 `provider_contract_version`;
            该值变化意味着报文字段/语义已变,已生成片段需整章重做
    * `capabilities: TTSCapabilities`    —— 能力与参数自描述(见 capabilities.py)
    * `synthesize(req: TTSRequest) -> TTSResult`
          - 只接受字节、只返回字节(不读文件、不碰文件系统,架构 R3)
          - 网络异常/超时/5xx 收敛为 `errors.RetryableError`;
            400 / 数据与参数错误收敛为 `errors.FatalError`
          - 在 `TTSResult.format` 里**声明**输出格式;`is_canonical=True` 表示
            已是标准内部格式(架构 R7,归一化归 `synthesis/`)
    * `probe() -> HealthResult`
          - 后端无健康检查端点时,用一次极短真实合成探测(见 probe.py)

`TTSProvider` 的数据模型(`TTSRequest` / `TTSResult` / `HealthResult` /
`VoiceRef` / `EmotionSpec` / `AudioFormat`)是**全项目冻结的接缝**,定义在
`src/models.py`。此处仅做再导出,避免出现第二份定义而产生漂移。
"""

from __future__ import annotations

from ..models import (
    CANONICAL_FORMAT,
    AudioFormat,
    EmotionSpec,
    HealthResult,
    TTSProvider,
    TTSRequest,
    TTSResult,
    VoiceRef,
)

__all__ = [
    "AudioFormat",
    "CANONICAL_FORMAT",
    "EmotionSpec",
    "HealthResult",
    "TTSProvider",
    "TTSRequest",
    "TTSResult",
    "VoiceRef",
]
