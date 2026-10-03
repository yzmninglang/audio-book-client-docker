"""FastAPI 装配 + 静态托管 `src/web/dist` + SPA 回落(design §6)。

单容器单端口:同一进程同时提供 API 与前端静态文件,最适合 NAS 部署。

    /api/**      → 路由
    /assets/**   → Vue3 构建产物
    /**          → 回落到 index.html(SPA 路由)

`src/web/dist` 不存在时**不启动失败** —— 打印提示,仅提供 API。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings
from ..synthesis import SynthesisQueue, get_queue
from .error_handlers import register_error_handlers
from .routes import bindings, fs, jobs, projects, tts, voices
from .routes import settings as settings_routes

logger = logging.getLogger(__name__)

DIST_DIR = Path(__file__).resolve().parents[1] / "web" / "dist"
"""前端构建产物目录(`src/web/dist`)。"""

DEFAULT_SSE_HEARTBEAT_SECONDS = 15.0


def _configure_static(app: FastAPI) -> None:
    dist = DIST_DIR
    if not dist.is_dir():
        logger.warning(
            "前端构建产物不存在(%s);仅提供 API,SPA 路由将返回 404 —— 请先构建 src/web。",
            dist,
        )
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    dist_resolved = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:  # noqa: ANN202
        if full_path.startswith("api"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path:
            candidate = dist / full_path
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = None
            if (
                resolved is not None
                and dist_resolved in resolved.parents
                and candidate.is_file()
            ):
                return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="Not Found")


def create_app(
    settings: Settings | None = None,
    queue: SynthesisQueue | None = None,
) -> FastAPI:
    """装配应用。测试可注入临时 `Settings` 与隔离的队列。"""
    settings = settings or Settings.load()
    queue = queue or get_queue()

    app = FastAPI(title="有声书合成客户端", version="0.1.0")
    app.state.settings = settings
    app.state.queue = queue
    app.state.sse_heartbeat_seconds = DEFAULT_SSE_HEARTBEAT_SECONDS

    register_error_handlers(app)

    for router in (
        fs.router,
        settings_routes.router,
        tts.router,
        projects.router,
        bindings.router,
        voices.router,
        jobs.router,
    ):
        app.include_router(router)

    _configure_static(app)
    return app


app = create_app()
"""`uvicorn src.api.app:app` 的入口(默认设置 + 进程级全局队列单例)。"""


def main() -> None:
    """容器入口:监听 `0.0.0.0`、端口可配、日志写 stdout。"""
    settings = Settings.load()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
