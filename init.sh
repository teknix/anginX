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

install_docker() {
    info "Docker not found — installing via get.docker.com..."

    command -v curl >/dev/null 2>&1 || {
        # install curl first
        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get update -qq && sudo apt-get install -y -qq curl
        elif command -v yum >/dev/null 2>&1; then
            sudo yum install -y -q curl
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y -q curl
        else
            die "curl not found and no known package manager to install it"
        fi
    }

    curl -fsSL https://get.docker.com | sudo sh
    sudo systemctl enable --now docker

    # Add current user to docker group so subsequent docker calls work without sudo
    if ! groups "$USER" | grep -q docker; then
        sudo usermod -aG docker "$USER"
        warn "Added $USER to the docker group."
        warn "For non-root docker access in new shells, log out and back in."
        warn "Continuing this session via sudo..."
        DOCKER_CMD="sudo docker"
    fi

    ok "Docker installed: $(docker --version 2>/dev/null || sudo docker --version)"
}

install_compose_plugin() {
    info "Docker Compose v2 plugin not found — installing from GitHub releases..."

    COMPOSE_VERSION=$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest \
        | grep '"tag_name"' | sed 's/.*"tag_name": *"\(.*\)".*/\1/')
    [ -z "$COMPOSE_VERSION" ] && die "Could not determine latest Docker Compose version"

    COMPOSE_BIN="/usr/local/lib/docker/cli-plugins/docker-compose"
    sudo mkdir -p "$(dirname "$COMPOSE_BIN")"
    sudo curl -fsSL \
        "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)" \
        -o "$COMPOSE_BIN"
    sudo chmod +x "$COMPOSE_BIN"

    ok "Docker Compose installed: $(${DOCKER_CMD} compose version)"
}

DOCKER_CMD="docker"

if ! command -v docker >/dev/null 2>&1; then
    install_docker
else
    ok "Docker: $(docker --version)"
fi

if ! ${DOCKER_CMD} compose version >/dev/null 2>&1; then
    install_compose_plugin
else
    ok "Docker Compose: $(${DOCKER_CMD} compose version)"
fi

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

# ── Cert setup — always copies into ./certs/ (mounted as the container volume) ─

# ./certs/<domain>/fullchain.pem + privkey.pem
# docker-compose.yml mounts ./certs → /etc/nginx/certs inside the container
mkdir -p certs

# .cert-sources records where each domain's cert came from so renew-certs.sh
# can re-copy after certbot renewal without user input
CERT_SOURCES_FILE=".cert-sources"

copy_cert_pair() {
    local domain="$1" fc="$2" pk="$3"
    mkdir -p "certs/${domain}"
    cp -L "$fc" "certs/${domain}/fullchain.pem"
    cp -L "$pk" "certs/${domain}/privkey.pem"
    chmod 600 "certs/${domain}/privkey.pem"
    # record source so renew-certs.sh knows where to pull from
    grep -v "^${domain}=" "$CERT_SOURCES_FILE" 2>/dev/null > "${CERT_SOURCES_FILE}.tmp" || true
    echo "${domain}=$(dirname "$fc")" >> "${CERT_SOURCES_FILE}.tmp"
    mv "${CERT_SOURCES_FILE}.tmp" "$CERT_SOURCES_FILE"
    ok "certs/${domain}/ — copied"
}

install_certbot() {
    if command -v certbot >/dev/null 2>&1; then return; fi
    info "certbot not found — installing..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y -qq certbot
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y -q certbot
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y -q certbot
    else
        die "Cannot install certbot automatically — install it manually then re-run init.sh"
    fi
    ok "certbot installed"
}

obtain_cert_standalone() {
    local domain="$1" email="$2"
    info "Obtaining cert for ${domain} via Let's Encrypt standalone..."
    sudo certbot certonly \
        --standalone \
        --non-interactive \
        --agree-tos \
        --email "$email" \
        -d "$domain" \
        --expand
    ok "Cert obtained for ${domain}"
}

# ── Per-mode cert acquisition ──────────────────────────────────────────────────

collect_domains_and_certs() {
    # shared loop used by both modes: prompts for domains, handles no-cert case
    local mode="$1"   # "dynamic" or "baked"
    COLLECTED_DOMAINS=()

    echo ""
    info "Enter root domains one at a time (e.g. 1.com), blank to finish."
    echo ""

    while true; do
        read -rp "$(echo -e "${CYAN}Domain${NC} (or blank to finish): ")" DOMAIN
        [ -z "$DOMAIN" ] && break

        FC="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
        PK="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"

        # Check if cert already exists at the LE standard path
        if [ -f "$FC" ] && [ -f "$PK" ]; then
            copy_cert_pair "$DOMAIN" "$FC" "$PK"
        else
            echo ""
            warn "No cert found for ${DOMAIN} at ${FC}"
            echo "    [1] Obtain a new cert via Let's Encrypt (HTTP challenge — port 80 must be free)"
            echo "    [2] Provide cert paths manually"
            echo "    [3] Skip this domain"
            read -rp "$(echo -e "${CYAN}  Choice${NC} [1/2/3]: ")" CERT_CHOICE

            case "${CERT_CHOICE:-1}" in
                1)
                    read -rp "$(echo -e "${CYAN}  Email for LE registration${NC}: ")" LE_EMAIL
                    [ -z "$LE_EMAIL" ] && { warn "Email required — skipping ${DOMAIN}"; continue; }
                    install_certbot
                    obtain_cert_standalone "$DOMAIN" "$LE_EMAIL"
                    copy_cert_pair "$DOMAIN" "$FC" "$PK"
                    ;;
                2)
                    read -rp "$(echo -e "${CYAN}  fullchain.pem path${NC}: ")" MANUAL_FC
                    read -rp "$(echo -e "${CYAN}  privkey.pem   path${NC}: ")" MANUAL_PK
                    [ -f "$MANUAL_FC" ] || { warn "File not found: $MANUAL_FC — skipping ${DOMAIN}"; continue; }
                    [ -f "$MANUAL_PK" ] || { warn "File not found: $MANUAL_PK — skipping ${DOMAIN}"; continue; }
                    copy_cert_pair "$DOMAIN" "$MANUAL_FC" "$MANUAL_PK"
                    ;;
                3)
                    warn "Skipping ${DOMAIN}"
                    continue
                    ;;
                *)
                    warn "Invalid choice — skipping ${DOMAIN}"
                    continue
                    ;;
            esac
        fi

        if [ "$mode" = "baked" ]; then
            SLUG="${DOMAIN//.}"
            FRAG="conf.base/_ssl_${SLUG}.conf"
            cat > "$FRAG" <<SSLCONF
ssl_certificate /etc/nginx/certs/${DOMAIN}/fullchain.pem;
ssl_certificate_key /etc/nginx/certs/${DOMAIN}/privkey.pem;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers HIGH:!aNULL:!MD5;
SSLCONF
            ok "conf.base/_ssl_${SLUG}.conf created"
        fi

        COLLECTED_DOMAINS+=("$DOMAIN")
    done

    [ ${#COLLECTED_DOMAINS[@]} -eq 0 ] && die "No domains configured"
    ok "Certs ready for: ${COLLECTED_DOMAINS[*]}"
}

if [ "$ANGINX_SSL_MODE" = "dynamic" ]; then

    echo ""
    echo "  [1] I have existing certs — point me at the directory"
    echo "  [2] Enter domains one by one (obtain or locate each)"
    echo ""
    read -rp "$(echo -e "${CYAN}Choose${NC} [1/2, default 1]: ")" CERT_SRC_CHOICE

    if [ "${CERT_SRC_CHOICE:-1}" = "1" ]; then
        echo ""
        info "Source cert directory — each subdirectory must have fullchain.pem + privkey.pem"
        info "(e.g. /etc/letsencrypt/live)"
        echo ""
        read -rp "$(echo -e "${CYAN}Source cert directory${NC}: ")" SRC_CERTS
        [ -d "$SRC_CERTS" ] || die "Directory not found: $SRC_CERTS"

        FOUND_DOMAINS=()
        while IFS= read -r -d '' dir; do
            domain=$(basename "$dir")
            fc="${dir}/fullchain.pem"
            pk="${dir}/privkey.pem"
            if [ -f "$fc" ] && [ -f "$pk" ]; then
                copy_cert_pair "$domain" "$fc" "$pk"
                FOUND_DOMAINS+=("$domain")
            else
                warn "Skipping ${domain} — missing fullchain.pem or privkey.pem"
            fi
        done < <(find "$SRC_CERTS" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

        [ ${#FOUND_DOMAINS[@]} -eq 0 ] && die "No valid cert directories found in $SRC_CERTS"
        ok "Certs ready for: ${FOUND_DOMAINS[*]}"
    else
        collect_domains_and_certs "dynamic"
    fi

else
    collect_domains_and_certs "baked"
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
ENV
ok "Wrote $ENV_FILE"

# ── Build ─────────────────────────────────────────────────────────────────────

echo ""
info "Building Docker image..."
${DOCKER_CMD} compose build --quiet
ok "Image built"

# ── Start ─────────────────────────────────────────────────────────────────────

info "Starting anginX..."
${DOCKER_CMD} compose up -d
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
        echo "  ${DOCKER_CMD} compose logs anginx"
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
echo "  Logs:      ${DOCKER_CMD} compose logs -f anginx"
echo ""
echo "  Cert renewal — add to cron:"
echo "    0 3 * * * cd $(pwd) && ./renew-certs.sh >> /var/log/anginx-renew.log 2>&1"
echo "  ─────────────────────────────────────────────────"
echo ""
