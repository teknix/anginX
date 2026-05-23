#!/bin/bash
# register.sh — register a service with anginX
#
# Usage:
#   ./register.sh <domain> <port> <name> [host]
#
# Reads ANGINX_KEY and ANGINX_URL from environment (with sensible defaults).
#
# Examples:
#   # Docker same-network (anginx resolves container by name):
#   ANGINX_KEY=mykey ./register.sh app.1.com 7705 myapp
#
#   # LAN host (service on a different machine):
#   ANGINX_KEY=mykey ANGINX_URL=http://192.168.1.10 \
#     ./register.sh app.1.com 7705 myapp 192.168.1.50

set -euo pipefail

DOMAIN="${1:-}"
PORT="${2:-}"
NAME="${3:-}"
HOST="${4:-}"

ANGINX_URL="${ANGINX_URL:-http://anginx}"
ANGINX_KEY="${ANGINX_KEY:-}"

# ── Validation ─────────────────────────────────────────────────────────────────

if [ -z "$DOMAIN" ] || [ -z "$PORT" ] || [ -z "$NAME" ]; then
    echo "Usage: $0 <domain> <port> <name> [host]" >&2
    echo "       ANGINX_KEY and ANGINX_URL must be set in environment" >&2
    exit 1
fi

if [ -z "$ANGINX_KEY" ]; then
    echo "Error: ANGINX_KEY is not set" >&2
    exit 1
fi

# ── Build payload ──────────────────────────────────────────────────────────────

if [ -n "$HOST" ]; then
    PAYLOAD=$(printf '{"domain":"%s","port":%s,"name":"%s","host":"%s"}' \
        "$DOMAIN" "$PORT" "$NAME" "$HOST")
else
    PAYLOAD=$(printf '{"domain":"%s","port":%s,"name":"%s"}' \
        "$DOMAIN" "$PORT" "$NAME")
fi

# ── Register with retry on 503 ─────────────────────────────────────────────────

MAX_ATTEMPTS=20
ATTEMPT=0

while [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; do
    ATTEMPT=$((ATTEMPT + 1))

    HTTP_CODE=$(curl -s -o /tmp/anginx_resp.json -w "%{http_code}" \
        -X POST "${ANGINX_URL}/new/${ANGINX_KEY}" \
        -H 'Content-Type: application/json' \
        -d "$PAYLOAD")

    BODY=$(cat /tmp/anginx_resp.json 2>/dev/null || echo "")

    if [ "$HTTP_CODE" = "200" ]; then
        echo "[anginx] registered: $BODY"
        exit 0
    elif [ "$HTTP_CODE" = "503" ]; then
        echo "[anginx] cert not ready for $DOMAIN, retrying in 10s... (attempt $ATTEMPT/$MAX_ATTEMPTS)"
        sleep 10
    elif [ "$HTTP_CODE" = "000" ]; then
        echo "[anginx] connection failed, retrying in 5s... (attempt $ATTEMPT/$MAX_ATTEMPTS)"
        sleep 5
    else
        echo "[anginx] registration failed (HTTP $HTTP_CODE): $BODY" >&2
        exit 1
    fi
done

echo "[anginx] gave up registering $DOMAIN after $MAX_ATTEMPTS attempts" >&2
exit 1
