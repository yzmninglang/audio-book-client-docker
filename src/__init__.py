"""有声书合成客户端。

分层与依赖硬规则见 `doc/architecture.md`:

    api/ ──▶ synthesis/ ──▶ tts/  |  audio/  |  project/ + voice/
                            ▲ R1: tts/ 与 audio/ 互相禁止 import
                            ▲ R7: 异构输出格式的归一化归属 synthesis/
"""
