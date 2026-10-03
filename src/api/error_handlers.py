"""异常 → HTTP 状态映射(全站统一,`doc/api/design.md` §8.8)。

响应体统一为:

    {"error": {"kind": "...", "message": "...", "detail": ...}}

`kind` 取自 `src/errors.py` 的异常 `kind`。状态映射:
`invalid`→400、`not_found`→404、`config`/`binding`→409、其余→500。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import AudiobookError
from .errors import AlreadyExistsError

logger = logging.getLogger(__name__)

# kind → HTTP 状态(§8.8)
_KIND_TO_STATUS: dict[str, int] = {
    "invalid": 400,
    "not_found": 404,
    "config": 409,
    "binding": 409,
    "conflict": 409,
}
_DEFAULT_STATUS = 500


def status_for_kind(kind: str) -> int:
    return _KIND_TO_STATUS.get(kind, _DEFAULT_STATUS)


def _error_body(kind: str, message: str, detail: object | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"kind": kind, "message": message}
    if detail is not None:
        payload["detail"] = detail
    return {"error": payload}


def _http_status_to_kind(status: int) -> str:
    if status == 404:
        return "not_found"
    if status in (400, 422):
        return "invalid"
    if status == 409:
        return "config"
    return "fatal"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AlreadyExistsError)
    async def _already_exists(_request: Request, exc: AlreadyExistsError) -> JSONResponse:
        # 资源已存在 → 409,但 kind 仍为 invalid(§8.9)。Starlette 按 MRO 取最具体处理器。
        return JSONResponse(
            status_code=409,
            content=_error_body("invalid", exc.message, exc.detail),
        )

    @app.exception_handler(AudiobookError)
    async def _audiobook_error(_request: Request, exc: AudiobookError) -> JSONResponse:
        logger.warning("领域异常: %s", exc.message)
        return JSONResponse(
            status_code=status_for_kind(exc.kind),
            content=_error_body(exc.kind, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 入参不合法 → 400 invalid(而非 FastAPI 默认的 422)。
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
        message = "; ".join(p for p in parts if p) or "请求参数校验失败"
        return JSONResponse(
            status_code=400,
            content=_error_body("invalid", message, exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        kind = _http_status_to_kind(exc.status_code)
        detail = exc.detail if isinstance(exc.detail, str) else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(kind, detail or "请求失败"),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未预期错误: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_error_body("fatal", f"{type(exc).__name__}: {exc}"),
        )
