"""HTTP 层(M5)—— FastAPI 装配、出入参校验、SSE 推送、静态托管。

对应设计:`doc/api/design.md`。

**硬规则 R5**:本包**不含业务逻辑**,只做协议转换 —— 任何判定
(未完成片段、绑定有效性、能力支持)都来自下层 `synthesis/`、`project/`、`voice/`、`tts/`。

    app.py            FastAPI 装配 + 静态托管 src/web/dist + SPA 回落
    schemas.py        Pydantic 出入参(形状严格照 doc/api/design.md §8)
    deps.py           依赖注入:Settings / VoiceLibrary / Workspace / 合成队列
    error_handlers.py 异常 → HTTP 状态映射(§8.8)
    routes/           fs / settings / tts / projects / voices / bindings / jobs
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
