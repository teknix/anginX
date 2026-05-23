#!/bin/sh
# Supervised daemon: acquires certs for ANGINX_DOMAINS via certbot HTTP-01
# webroot challenge, then renews every 12h.
# Cloudflare proxy (orange cloud) passes /.well-known/acme-challenge/ through
# to the origin — no API token or special config needed.

set -e

CERTS_DIR="/etc/nginx/certs"
ACME_WEBROOT="/var/www/acme"
RENEW_INTERVAL=43200  # 12 hours

# Wait for nginx to be running
echo "[cert-manager] waiting for nginx..."
ATTEMPTS=0
until [ -f /run/nginx.pid ] && kill -0 "$(cat /run/nginx.pid)" 2>/dev/null; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge 60 ]; then
        echo "[cert-manager] nginx did not start in 60s — giving up" >&2
        exit 1
    fi
    sleep 1
done
echo "[cert-manager] nginx ready (pid=$(cat /run/nginx.pid))"

acquire() {
    local domain="$1"
    local live_dir="${CERTS_DIR}/live/${domain}"

    if [ -f "${live_dir}/fullchain.pem" ]; then
        echo "[cert-manager] cert already exists for ${domain}"
        return 0
    fi

    if [ -z "${ANGINX_EMAIL:-}" ]; then
        echo "[cert-manager] ANGINX_EMAIL not set — cannot acquire cert for ${domain}" >&2
        return 1
    fi

    echo "[cert-manager] acquiring cert for ${domain}..."
    certbot certonly \
        --config-dir "$CERTS_DIR" \
        --work-dir /var/lib/certbot \
        --logs-dir /var/log/certbot \
        --webroot \
        --webroot-path "$ACME_WEBROOT" \
        --non-interactive \
        --agree-tos \
        --email "$ANGINX_EMAIL" \
        -d "$domain" \
        --expand
    echo "[cert-manager] cert acquired for ${domain}"
}

# Initial acquisition for each domain in ANGINX_DOMAINS (comma-separated)
if [ -n "${ANGINX_DOMAINS:-}" ]; then
    old_IFS="$IFS"
    IFS=','
    for domain in $ANGINX_DOMAINS; do
        IFS="$old_IFS"
        domain=$(echo "$domain" | tr -d ' ')
        [ -z "$domain" ] && continue
        acquire "$domain" || true
        IFS=','
    done
    IFS="$old_IFS"
fi

# Renewal loop — certbot renews certs with < 30 days remaining.
# nginx -s reload picks up renewed certs without dropping connections.
while true; do
    sleep "$RENEW_INTERVAL"
    echo "[cert-manager] running certbot renew..."
    certbot renew \
        --config-dir "$CERTS_DIR" \
        --work-dir /var/lib/certbot \
        --logs-dir /var/log/certbot \
        --non-interactive \
        --quiet \
        --deploy-hook "nginx -s reload"
done
