#!/bin/sh
# Write conf.d/ssl/_<slug>.conf for each cert in /etc/nginx/certs/live/.
# Runs at entrypoint before supervisord so nginx starts with valid SSL config.

CERTS_DIR="/etc/nginx/certs"
CONF_SSL="/etc/nginx/conf.d/ssl"
LIVE_DIR="${CERTS_DIR}/live"

mkdir -p "$CONF_SSL"

[ -d "$LIVE_DIR" ] || exit 0

for dir in "$LIVE_DIR"/*/; do
    [ -d "$dir" ] || continue
    domain=$(basename "${dir%/}")
    [ "$domain" = "README" ] && continue

    fc="${dir}fullchain.pem"
    pk="${dir}privkey.pem"
    [ -f "$fc" ] && [ -f "$pk" ] || continue

    slug=$(echo "$domain" | tr -d '.')
    frag="${CONF_SSL}/_${slug}.conf"

    cat > "$frag" <<EOF
ssl_certificate ${fc};
ssl_certificate_key ${pk};
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers HIGH:!aNULL:!MD5;
EOF
    echo "[anginx] SSL fragment ready: $domain"
done
