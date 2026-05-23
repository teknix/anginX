#!/bin/bash
# Run this from the anginX project directory.
# Re-copies certs from their source locations into ./certs/ and reloads the container.
#
# Set up automatic renewal (runs daily at 03:00):
#   crontab -e
#   0 3 * * * cd /path/to/anginX && ./renew-certs.sh >> /var/log/anginx-renew.log 2>&1
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${GREEN}✓${NC} $*"; }
info() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${CYAN}→${NC} $*"; }
warn() { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${YELLOW}!${NC} $*"; }
die()  { echo -e "$(date '+%Y-%m-%d %H:%M:%S') ${RED}✗${NC} $*" >&2; exit 1; }

CERT_SOURCES_FILE=".cert-sources"
DOCKER_CMD="docker"
command -v docker >/dev/null 2>&1 || DOCKER_CMD="sudo docker"

# ── Verify we're in the right directory ───────────────────────────────────────

[ -f "docker-compose.yml" ] || die "Run this script from the anginX project directory"
[ -f "$CERT_SOURCES_FILE" ] || die "$CERT_SOURCES_FILE not found — run init.sh first"

# ── Run certbot renew ─────────────────────────────────────────────────────────

if command -v certbot >/dev/null 2>&1; then
    info "Running certbot renew..."
    sudo certbot renew --quiet --no-random-sleep-on-renew
    ok "certbot renew complete"
else
    warn "certbot not found — skipping renewal, will re-copy existing certs"
fi

# ── Re-copy certs from source locations into ./certs/ ─────────────────────────

RENEWED=0
SKIPPED=0

while IFS='=' read -r domain src_dir; do
    [ -z "$domain" ] || [ -z "$src_dir" ] && continue
    # strip comments
    [[ "$domain" =~ ^# ]] && continue

    fc="${src_dir}/fullchain.pem"
    pk="${src_dir}/privkey.pem"

    if [ ! -f "$fc" ] || [ ! -f "$pk" ]; then
        warn "${domain}: cert files not found at ${src_dir} — skipping"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    mkdir -p "certs/${domain}"
    cp -L "$fc" "certs/${domain}/fullchain.pem"
    cp -L "$pk" "certs/${domain}/privkey.pem"
    chmod 600 "certs/${domain}/privkey.pem"
    ok "${domain}: cert updated from ${src_dir}"
    RENEWED=$((RENEWED + 1))
done < "$CERT_SOURCES_FILE"

[ "$RENEWED" -eq 0 ] && [ "$SKIPPED" -gt 0 ] && die "No certs were updated — check source paths in $CERT_SOURCES_FILE"
[ "$RENEWED" -eq 0 ] && die "$CERT_SOURCES_FILE is empty or has no valid entries"

# ── Signal nginx to reload (graceful — no dropped connections) ────────────────
# Certs are on a bind mount so nginx sees the new files immediately after copy.
# nginx -s reload (SIGHUP to master) re-reads config + certs without restarting
# the container or dropping existing connections.

if ${DOCKER_CMD} compose ps anginx 2>/dev/null | grep -q "running\|Up"; then
    info "Sending nginx reload signal..."
    ${DOCKER_CMD} compose exec -T anginx nginx -s reload
    ok "nginx reloaded — new certs active"
else
    warn "anginX container is not running — start it with: docker compose up -d"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
ok "Renewal complete — ${RENEWED} domain(s) updated, ${SKIPPED} skipped"
