#!/bin/bash
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

echo ""
echo "  anginX — first-run setup"
echo "  ─────────────────────────"
echo ""

# ── Prerequisites ──────────────────────────────────────────────────────────────

command -v docker >/dev/null 2>&1 || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose (v2) is not installed"
ok "docker and docker compose found"

# ── Load existing .env if present ──────────────────────────────────────────────

ENV_FILE=".env"
if [ -f "$ENV_FILE" ]; then
    warn ".env already exists — values will be used as defaults (delete it to start fresh)"
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
fi

# ── API key ────────────────────────────────────────────────────────────────────

if [ -z "${ANGINX_API_KEY:-}" ]; then
    GENERATED_KEY=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
    read -rp "$(echo -e "${CYAN}API key${NC} [press enter to generate one]: ")" INPUT_KEY
    ANGINX_API_KEY="${INPUT_KEY:-$GENERATED_KEY}"
fi
ok "API key: ${ANGINX_API_KEY:0:6}…"

# ── SSL mode ───────────────────────────────────────────────────────────────────

echo ""
echo "  SSL mode:"
echo "    [1] dynamic  — scan a cert directory at startup (recommended)"
echo "    [2] baked    — cert paths baked into conf.base/ at build time"
echo ""
read -rp "$(echo -e "${CYAN}Choose${NC} [1/2, default 1]: ")" SSL_CHOICE
SSL_CHOICE="${SSL_CHOICE:-1}"

case "$SSL_CHOICE" in
    1|dynamic)   ANGINX_SSL_MODE="dynamic" ;;
    2|baked)     ANGINX_SSL_MODE="baked" ;;
    *)           die "Invalid choice: $SSL_CHOICE" ;;
esac
ok "SSL mode: $ANGINX_SSL_MODE"

# ── Cert directory / domain setup ─────────────────────────────────────────────

if [ "$ANGINX_SSL_MODE" = "dynamic" ]; then

    echo ""
    info "Dynamic mode: certs must be at <cert-dir>/<domain>/fullchain.pem + privkey.pem"
    info "  e.g. /etc/letsencrypt/live/1.com/fullchain.pem"
    echo ""
    DEFAULT_CERTS="${CERTS_DIR:-/etc/nginx/certs}"
    read -rp "$(echo -e "${CYAN}Cert directory${NC} [$DEFAULT_CERTS]: ")" INPUT_CERTS
    CERTS_DIR="${INPUT_CERTS:-$DEFAULT_CERTS}"

    [ -d "$CERTS_DIR" ] || die "Directory not found: $CERTS_DIR"

    FOUND_DOMAINS=()
    while IFS= read -r -d '' dir; do
        domain=$(basename "$dir")
        if [ -f "$dir/fullchain.pem" ] && [ -f "$dir/privkey.pem" ]; then
            FOUND_DOMAINS+=("$domain")
        else
            warn "Skipping $domain — missing fullchain.pem or privkey.pem"
        fi
    done < <(find "$CERTS_DIR" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

    if [ ${#FOUND_DOMAINS[@]} -eq 0 ]; then
        die "No valid cert directories found in $CERTS_DIR"
    fi
    ok "Found certs for: ${FOUND_DOMAINS[*]}"

else

    # Baked mode: create _ssl_<slug>.conf fragments for each domain
    echo ""
    info "Baked mode: enter root domains one at a time (e.g. 1.com), blank to finish"
    BAKED_DOMAINS=()
    while true; do
        read -rp "$(echo -e "${CYAN}Domain${NC} (or blank to finish): ")" DOMAIN
        [ -z "$DOMAIN" ] && break

        SLUG="${DOMAIN//.}"  # strip dots: 1.com → 1com

        DEFAULT_FC="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
        DEFAULT_PK="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"

        read -rp "$(echo -e "${CYAN}  fullchain.pem path${NC} [$DEFAULT_FC]: ")" FC
        FC="${FC:-$DEFAULT_FC}"
        read -rp "$(echo -e "${CYAN}  privkey.pem   path${NC} [$DEFAULT_PK]: ")" PK
        PK="${PK:-$DEFAULT_PK}"

        [ -f "$FC" ] || { warn "File not found: $FC"; continue; }
        [ -f "$PK" ] || { warn "File not found: $PK"; continue; }

        FRAG="conf.base/_ssl_${SLUG}.conf"
        cat > "$FRAG" <<SSLCONF
ssl_certificate ${FC};
ssl_certificate_key ${PK};
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers HIGH:!aNULL:!MD5;
SSLCONF
        ok "Created $FRAG for ${DOMAIN}"
        BAKED_DOMAINS+=("$DOMAIN")
    done

    [ ${#BAKED_DOMAINS[@]} -eq 0 ] && die "No domains configured"
    CERTS_DIR="/etc/nginx/certs"

fi

# ── Max services cap ───────────────────────────────────────────────────────────

ANGINX_MAX_SERVICES="${ANGINX_MAX_SERVICES:-100}"

# ── Check ports 80 and 443 ────────────────────────────────────────────────────

echo ""
for PORT in 80 443; do
    if ss -tlnp 2>/dev/null | grep -q ":${PORT} " || \
       netstat -tlnp 2>/dev/null | grep -q ":${PORT} "; then
        warn "Port ${PORT} is in use — make sure nothing else is binding it (e.g. an existing nginx)"
    else
        ok "Port ${PORT} is free"
    fi
done

# ── Write .env ────────────────────────────────────────────────────────────────

cat > "$ENV_FILE" <<ENV
ANGINX_API_KEY=${ANGINX_API_KEY}
ANGINX_SSL_MODE=${ANGINX_SSL_MODE}
ANGINX_MAX_SERVICES=${ANGINX_MAX_SERVICES}
CERTS_DIR=${CERTS_DIR}
ENV
ok "Wrote $ENV_FILE"

# ── Patch docker-compose.yml cert volume mount ────────────────────────────────

# Replace the placeholder cert path with the real one
if grep -q '/path/to/certs' docker-compose.yml; then
    sed -i "s|/path/to/certs|${CERTS_DIR}|g" docker-compose.yml
    ok "Updated docker-compose.yml cert volume mount → ${CERTS_DIR}"
fi

# ── Build ─────────────────────────────────────────────────────────────────────

echo ""
info "Building Docker image..."
docker compose build --quiet
ok "Image built"

# ── Start ─────────────────────────────────────────────────────────────────────

info "Starting anginX..."
docker compose up -d
ok "Container started"

# ── Health check ──────────────────────────────────────────────────────────────

echo ""
info "Waiting for anginX to become healthy..."
ATTEMPTS=0
until curl -sf http://localhost/health >/dev/null 2>&1; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "$ATTEMPTS" -ge 30 ]; then
        echo ""
        warn "Health check timed out after 15s. Check logs:"
        echo "  docker compose logs anginx"
        exit 1
    fi
    sleep 0.5
done

HEALTH=$(curl -s http://localhost/health)
ok "anginX is healthy: $HEALTH"

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "  ─────────────────────────────────────────────────"
echo -e "  ${GREEN}anginX is running.${NC}"
echo ""
echo "  API key:  ${ANGINX_API_KEY}"
echo ""
echo "  Register a service:"
echo "    curl -X POST http://localhost/new/${ANGINX_API_KEY} \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"domain\":\"app.yourdomain.com\",\"port\":7705,\"name\":\"myapp\"}'"
echo ""
echo "  Dashboard: http://localhost/dashboard?key=${ANGINX_API_KEY}"
echo "  Services:  http://localhost/services"
echo "  Logs:      docker compose logs -f anginx"
echo "  ─────────────────────────────────────────────────"
echo ""
