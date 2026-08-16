#!/bin/sh
# Supervised daemon: acquires certs for ANGINX_DOMAINS, then renews every 12h.
#
# Default challenge is HTTP-01 webroot. Cloudflare proxy (orange cloud) normally
# passes /.well-known/acme-challenge/ through to the origin, so most domains need
# no API token or special config.
#
# Some zones do not cooperate: a WAF/firewall/bot rule can 403 the challenge for
# *some* of Let's Encrypt's validation vantage points while serving it fine to
# others (LE validates from multiple perspectives, and a domain fails if any
# secondary perspective fails). That presents as an unreproducible failure — the
# URL returns 200 from every box you own. For those domains, list them in
# ANGINX_DNS01_DOMAINS to validate over DNS-01 instead, which never touches the
# HTTP edge at all.

set -e

CERTS_DIR="/etc/nginx/certs"
ACME_WEBROOT="/var/www/acme"
RENEW_INTERVAL=43200  # 12 hours

# Domains that must use DNS-01 instead of HTTP-01 (comma-separated subset of
# ANGINX_DOMAINS). Requires the certbot-dns-cloudflare plugin and a credentials
# file. Kept opt-in per domain: HTTP-01 needs no secret, so only domains that
# actually need it should hand this container an API token's blast radius.
ANGINX_DNS01_DOMAINS="${ANGINX_DNS01_DOMAINS:-}"
# Lives in the certs volume so it survives rebuilds alongside the certs it issues.
ANGINX_CF_CREDENTIALS="${ANGINX_CF_CREDENTIALS:-${CERTS_DIR}/cloudflare.ini}"
# DNS propagation grace before certbot asks the CA to validate.
ANGINX_DNS01_PROPAGATION="${ANGINX_DNS01_PROPAGATION:-30}"

# Materialise the credentials file from an env var if one was supplied, so the
# token can come from compose/.env instead of a hand-placed file.
if [ -n "${ANGINX_CF_API_TOKEN:-}" ] && [ ! -f "$ANGINX_CF_CREDENTIALS" ]; then
    mkdir -p "$(dirname "$ANGINX_CF_CREDENTIALS")"
    printf 'dns_cloudflare_api_token = %s\n' "$ANGINX_CF_API_TOKEN" > "$ANGINX_CF_CREDENTIALS"
    echo "[cert-manager] wrote Cloudflare credentials from ANGINX_CF_API_TOKEN"
fi
# certbot refuses a world-readable credentials file.
[ -f "$ANGINX_CF_CREDENTIALS" ] && chmod 600 "$ANGINX_CF_CREDENTIALS"

# Is this domain configured for DNS-01?
uses_dns01() {
    case ",$(echo "$ANGINX_DNS01_DOMAINS" | tr -d ' ')," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

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

    # Pick the challenge. Default HTTP-01; DNS-01 only for domains that opted in.
    if uses_dns01 "$domain"; then
        # Fail loudly rather than silently falling back to HTTP-01: the whole
        # reason a domain is on this list is that HTTP-01 does not work for it,
        # so a silent fallback would just burn LE failed-validation rate limit
        # and reinstate the confusing "works from here, 403 for the CA" symptom.
        if ! certbot plugins --non-interactive 2>/dev/null | grep -q dns-cloudflare; then
            echo "[cert-manager] ${domain}: DNS-01 requested but the certbot-dns-cloudflare plugin is NOT installed — rebuild the image (it is in requirements.txt). Skipping." >&2
            [ -n "$foreign_bak" ] && { rm -rf "$live_dir"; mv "$foreign_bak" "$live_dir"; }
            return 1
        fi
        if [ ! -f "$ANGINX_CF_CREDENTIALS" ]; then
            echo "[cert-manager] ${domain}: DNS-01 requested but no credentials at ${ANGINX_CF_CREDENTIALS} (set ANGINX_CF_API_TOKEN or place the ini). Skipping." >&2
            [ -n "$foreign_bak" ] && { rm -rf "$live_dir"; mv "$foreign_bak" "$live_dir"; }
            return 1
        fi
        echo "[cert-manager] acquiring cert for ${domain} via DNS-01 (cloudflare)..."
        set -- --dns-cloudflare \
               --dns-cloudflare-credentials "$ANGINX_CF_CREDENTIALS" \
               --dns-cloudflare-propagation-seconds "$ANGINX_DNS01_PROPAGATION"
    else
        echo "[cert-manager] acquiring cert for ${domain} via HTTP-01 (webroot)..."
        set -- --webroot --webroot-path "$ACME_WEBROOT"
    fi

    if certbot certonly \
        --config-dir "$CERTS_DIR" \
        --work-dir /var/lib/certbot \
        --logs-dir /var/log/certbot \
        "$@" \
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
