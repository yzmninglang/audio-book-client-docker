# syntax=docker/dockerfile:1
# ============================================================================
# 有声书合成客户端 —— 多阶段镜像
#
# 规格来源:doc/deploy/deployment.md §3(镜像设计)、§4(compose)
#   stage 1  node:20-alpine   构建 Vue3 前端 → dist/
#   stage 2  python:3.11-slim 运行时(零算力、无 FFmpeg、依赖全为 pip wheel)
#
# 目标架构:x86_64(amd64,无需 multi-arch);无 GPU 需求。
# 体积预估:~150MB(不含 FFmpeg)。
# ============================================================================

# ---------------------------------------------------------------------------
# stage 1:前端构建(node:20-alpine)—— deployment.md §3
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /build

# 先只拷依赖清单,让 npm ci 层在「锁文件未变」时命中缓存
COPY src/web/package.json src/web/package-lock.json ./
RUN npm ci --no-audit --no-fund

# 再拷前端源码(npm run build = vue-tsc --noEmit && vite build)
# .dockerignore 已排除 node_modules/dist,不会污染已装好的依赖
COPY src/web/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# stage 2:运行时(python:3.11-slim)—— deployment.md §3
# 客户端零算力 → 不需要 torch;核心路径无需 FFmpeg(audio/design.md §3.5)。
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# ── 必须:LANG / LC_ALL = C.UTF-8 ───────────────────────────────────────────
# deployment.md §3:音色目录 37 个**全中文名**,locale 非 UTF-8 会直接乱码。
# 附带:PYTHONUNBUFFERED 让日志实时进 stdout(容器惯例,约束 #5);
#       PYTHONDONTWRITEBYTECODE 避免非 root 用户在只读的 /app 写 __pycache__。
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Python 依赖:全部 pip wheel,**无系统包、无 FFmpeg** ─────────────────────
# soundfile 自带 libsndfile >=1.1;numpy / soxr 均有 manylinux wheel。
# 依赖集严格对应 pyproject.toml 的 dependencies(不含 dev 组)。
RUN pip install --no-cache-dir \
        fastapi \
        "uvicorn[standard]" \
        pydantic \
        httpx \
        soundfile \
        numpy \
        soxr \
        python-multipart

# ── 非 root 运行用户(默认 uid/gid=1000;entrypoint 按 PUID/PGID 覆盖)──────
RUN groupadd -g 1000 app \
    && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin app

WORKDIR /app

# ── 后端源码(不拷 tests/、web 源码、.venv —— 由 .dockerignore 兜底)────────
COPY pyproject.toml ./
COPY src/__init__.py src/config.py src/errors.py src/models.py ./src/
COPY src/api ./src/api
COPY src/audio ./src/audio
COPY src/project ./src/project
COPY src/synthesis ./src/synthesis
COPY src/tts ./src/tts
COPY src/voice ./src/voice

# ── 前端构建产物(来自 stage 1;app.py 从 src/web/dist 静态托管 + SPA 回落)─
COPY --from=frontend /build/dist ./src/web/dist

# ── 入口脚本:按 PUID/PGID 调整运行用户后降权 exec uvicorn ─────────────────
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 容器内单端口(host 侧端口由 compose 映射;AUDIOBOOK_PORT 可覆盖)
EXPOSE 8000

# 默认命令由 entrypoint 组装(host=0.0.0.0,port=$AUDIOBOOK_PORT 或 8000)
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
