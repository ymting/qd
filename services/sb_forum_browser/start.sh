#!/bin/sh

# 烧饼论坛浏览器助手启动脚本。
# Xvfb/noVNC 是可选的：完整镜像中存在时为用户提供本机回环可访问的可见
# 浏览器；精简测试镜像没有这些命令时，仍可启动内部 HTTP API。
set -eu

export DISPLAY="${DISPLAY:-:99}"
# 容器内必须监听所有接口，宿主机 Compose 端口映射再负责限制到回环地址。
NOVNC_HOST="${NOVNC_HOST:-0.0.0.0}"
NOVNC_PORT="${NOVNC_PORT:-6081}"
VNC_PORT="${VNC_PORT:-5901}"

if command -v Xvfb >/dev/null 2>&1; then
    Xvfb "$DISPLAY" -screen 0 "${XVFB_SCREEN:-1280x900x24}" -ac +extension GLX -noreset \
        >/tmp/sb-forum-xvfb.log 2>&1 &
fi

if command -v x11vnc >/dev/null 2>&1; then
    # -localhost 确保 VNC 原始端口不直接暴露到公网。
    x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -localhost -forever -shared \
        -nopw >/tmp/sb-forum-x11vnc.log 2>&1 &
fi

if command -v websockify >/dev/null 2>&1 && [ -d "${NOVNC_WEB_DIR:-/usr/share/novnc}" ]; then
    websockify --web="${NOVNC_WEB_DIR:-/usr/share/novnc}" \
        "$NOVNC_HOST:$NOVNC_PORT" "127.0.0.1:$VNC_PORT" \
        >/tmp/sb-forum-websockify.log 2>&1 &
elif command -v novnc_proxy >/dev/null 2>&1; then
    novnc_proxy --listen "$NOVNC_HOST:$NOVNC_PORT" --vnc "127.0.0.1:$VNC_PORT" \
        >/tmp/sb-forum-novnc.log 2>&1 &
fi

exec python -m services.sb_forum_browser.server \
    --host "${SB_FORUM_HOST:-0.0.0.0}" \
    --port "${SB_FORUM_PORT:-8766}" \
    --data-dir "${SB_FORUM_DATA_DIR:-/data}" \
    --timeout-ms "${SB_FORUM_TIMEOUT_MS:-15000}" \
    --login-wait-ms "${SB_FORUM_LOGIN_WAIT_MS:-90000}"
