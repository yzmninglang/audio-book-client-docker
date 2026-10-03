#!/bin/sh
# ============================================================================
# 容器入口脚本
#
# 职责(对应 doc/deploy/deployment.md §4.2「NAS 部署第一坑」):
#   1. 按 PUID / PGID 调整运行用户(uid/gid 与 NAS 共享文件夹所有者对齐)
#   2. 需要时修正三个卷(以及 /app)的所有者,否则写 process_audio/、audio/ 失败
#   3. 降权 exec 应用(不保留 root),让进程收到 SIGTERM 可优雅退出
#
# 以 root 启动才做上述调整;若用户已通过 `docker run --user` 指定非 root,
# 则原样放行(此时卷权限需用户自行保证)。
# ============================================================================
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER="${APP_USER:-app}"
APP_HOME="/app"

log() { printf '%s [entrypoint] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# 未以 root 启动 → 无从 chown/usermod,直接放行。
if [ "$(id -u)" != "0" ]; then
    log "非 root(id=$(id -u)),跳过 PUID/PGID 调整,按当前用户启动"
    exec "$@"
fi

# --- 1) 组:优先复用已有 GID,否则改名现有 app 组,最后才新建 ---------------
if getent group "$PGID" >/dev/null 2>&1; then
    RUN_GROUP="$(getent group "$PGID" | cut -d: -f1)"
elif getent group "$APP_USER" >/dev/null 2>&1; then
    groupmod -o -g "$PGID" "$APP_USER"
    RUN_GROUP="$APP_USER"
else
    groupadd -o -g "$PGID" "$APP_USER"
    RUN_GROUP="$APP_USER"
fi

# --- 2) 用户:优先复用已有 UID,否则改名现有 app 用户,最后才新建 -----------
if getent passwd "$PUID" >/dev/null 2>&1; then
    RUN_USER="$(getent passwd "$PUID" | cut -d: -f1)"
elif getent passwd "$APP_USER" >/dev/null 2>&1; then
    usermod -o -u "$PUID" -g "$RUN_GROUP" "$APP_USER"
    RUN_USER="$APP_USER"
else
    useradd -o -u "$PUID" -g "$RUN_GROUP" -M -s /usr/sbin/nologin "$APP_USER"
    RUN_USER="$APP_USER"
fi

log "运行用户:${RUN_USER}(uid=${PUID},gid=${PGID},组=${RUN_GROUP})"

# --- 3) 修正卷所有者(缺卷则补建,避免挂载点未就绪时直接崩) ------------------
for vol in /workspace /voices /config; do
    if [ -d "$vol" ]; then
        chown -R "$PUID:$PGID" "$vol" 2>/dev/null \
            || log "警告:chown $vol 失败(请检查 NAS 共享文件夹权限)"
    else
        mkdir -p "$vol" 2>/dev/null && chown "$PUID:$PGID" "$vol" 2>/dev/null \
            || log "警告:无法创建/授权 $vol"
    fi
done

# 应用目录归运行用户,避免任何写入失败(仅几 MB)
[ -d "$APP_HOME" ] && chown -R "$PUID:$PGID" "$APP_HOME" 2>/dev/null || true

# --- 4) 默认命令:host 固定 0.0.0.0,端口来自 AUDIOBOOK_PORT(§deployment) ---
if [ "$#" -eq 0 ]; then
    set -- uvicorn src.api.app:app \
        --host "${AUDIOBOOK_HOST:-0.0.0.0}" \
        --port "${AUDIOBOOK_PORT:-8000}"
fi

# --- 5) 降权 exec(优先 setpriv:util-linux,debian slim 自带) --------------
if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
elif command -v gosu >/dev/null 2>&1; then
    exec gosu "$RUN_USER" "$@"
elif command -v su >/dev/null 2>&1; then
    # 兜底:参数已知(uvicorn + 主机/端口),拼接后交给 su
    exec su -s /bin/sh -c "$(printf '%s ' "$@")" "$RUN_USER"
else
    log "警告:未找到 setpriv/gosu/su,保持 root 运行"
    exec "$@"
fi
