#!/bin/sh
set -e

echo "[start-gunicorn] waiting for nginx to start..."
WAITED=0
while [ ! -f /run/nginx.pid ]; do
    if [ "$WAITED" -ge 20 ]; then
        echo "[start-gunicorn] timed out waiting for /run/nginx.pid after ${WAITED}s" >&2
        exit 1
    fi
    sleep 0.5
    WAITED=$((WAITED + 1))
done
echo "[start-gunicorn] nginx ready (pid=$(cat /run/nginx.pid)), starting gunicorn"

exec gunicorn \
    --workers=1 \
    --bind=127.0.0.1:5000 \
    --timeout=30 \
    --access-logfile=- \
    --error-logfile=- \
    "app:create_app()"
