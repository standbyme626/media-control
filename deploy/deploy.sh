#!/usr/bin/env bash
# 把仓库里的代码同步到运行目录并重启服务。用法：
#   ./deploy/deploy.sh            # 部署到 /opt/media-web
#   DEST=/srv/media-web ./deploy/deploy.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DEST:-/opt/media-web}"
UNIT="${UNIT:-media-web}"

echo "==> 同步 $SRC -> $DEST"
install -D -m 0644 "$SRC/server/app.py" "$DEST/app.py"
install -D -m 0600 "$SRC/bridge/dodaios-bridge.py" "$DEST/bridge/dodaios-bridge.py"

echo "==> 语法检查"
python3 -m py_compile "$DEST/app.py"

echo "==> 重启 $UNIT"
systemctl restart "$UNIT"
sleep 1
systemctl is-active --quiet "$UNIT" || { echo "!! $UNIT 未启动"; systemctl status "$UNIT" --no-pager -n 20; exit 1; }

# 健康检查：有 /etc/media-web.env 就带上 Basic 认证
PORT="${PORT:-8097}"
if [ -r /etc/media-web.env ]; then
  # shellcheck disable=SC1091
  set -a; . /etc/media-web.env; set +a
fi
if [ -n "${MEDIA_WEB_USER:-}" ] && [ -n "${MEDIA_WEB_PASSWORD:-}" ]; then
  curl -fsS --max-time 5 -u "$MEDIA_WEB_USER:$MEDIA_WEB_PASSWORD" "http://127.0.0.1:$PORT/api/health" && echo
else
  curl -fsS --max-time 5 -o /dev/null "http://127.0.0.1:$PORT/" && echo "ok (no auth env)"
fi
echo "==> 完成"
