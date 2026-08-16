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
    local renewal_conf="${CERTS_DIR}/renewal/${domain}.conf"
    local foreign_bak=""

    # A cert FILE existing is not proof of a cert we can actually renew.
    # It is common during a migration to hand-drop a self-signed placeholder
    # into live/<domain>/ so the vhost can serve HTTPS before DNS/NAT points
    # here. Keying only on the file made that placeholder suppress issuance
    # permanently: acquire() returned early on every boot, and `certbot renew`
    # skipped it too (it only renews lineages it owns), so the domain served
    # the placeholder forever with nothing in the log but "cert already exists".
    # certbot's own renewal conf is the authoritative marker of "certbot issued
    # this", so require both before considering the domain done.
    if [ -f "${live_dir}/fullchain.pem" ] && [ -f "$renewal_conf" ]; then
        echo "[cert-manager] cert already exists for ${domain}"
        return 0
    fi

    if [ -z "${ANGINX_EMAIL:-}" ]; then
        echo "[cert-manager] ANGINX_EMAIL not set — cannot acquire cert for ${domain}" >&2
        return 1
    fi

    # Set a foreign cert aside so certbot claims the canonical lineage name.
    # Left in place, certbot would create <domain>-0001, which no vhost
    # references — issuance would "succeed" while nginx kept serving the
    # placeholder.
    if [ -f "${live_dir}/fullchain.pem" ]; then
        foreign_bak="${CERTS_DIR}/foreign-backups/${domain}-$(date +%Y%m%d%H%M%S)"
        echo "[cert-manager] ${domain}: foreign (non-certbot) cert present — setting aside as ${foreign_bak}"
        mkdir -p "${CERTS_DIR}/foreign-backups"
        mv "$live_dir" "$foreign_bak"
    fi

    echo "[cert-manager] acquiring cert for ${domain}..."
    if certbot certonly \
        --config-dir "$CERTS_DIR" \
        --work-dir /var/lib/certbot \
        --logs-dir /var/log/certbot \
        --webroot \
        --webroot-path "$ACME_WEBROOT" \
        --non-interactive \
        --agree-tos \
        --email "$ANGINX_EMAIL" \
        -d "$domain" \
        --expand; then
        echo "[cert-manager] cert acquired for ${domain}"
        [ -n "$foreign_bak" ] && rm -rf "$foreign_bak"
        return 0
    fi

    # Issuance failed (CA unreachable, WAF blocking the challenge, rate limit).
    # If we displaced a cert to try, put it back: the vhost references that
    # path, and without it `nginx -t` fails and no reload can ever succeed.
    echo "[cert-manager] cert acquisition FAILED for ${domain}" >&2
    if [ -n "$foreign_bak" ]; then
        rm -rf "$live_dir"
        mv "$foreign_bak" "$live_dir"
        echo "[cert-manager] ${domain}: restored previous cert after failed issuance" >&2
    fi
    return 1
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
