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

# ── Domains for SSL ────────────────────────────────────────────────────────────

echo ""
echo "  anginX obtains and renews TLS certificates automatically via Let's Encrypt."
echo "  DNS for these domains must already point to this server."
echo ""

if [ -z "${ANGINX_DOMAINS:-}" ]; then
    read -rp "$(echo -e "${CYAN}Domains${NC} (comma-separated, e.g. app.1.com,api.1.com): ")" ANGINX_DOMAINS
fi

if [ -z "${ANGINX_DOMAINS:-}" ]; then
    warn "No domains provided — anginX will start without TLS certificates."
    warn "Set ANGINX_DOMAINS in .env and rebuild to acquire certificates later."
else
    ok "Domains: $ANGINX_DOMAINS"
fi

# ── Email for Let's Encrypt ────────────────────────────────────────────────────

if [ -z "${ANGINX_EMAIL:-}" ] && [ -n "${ANGINX_DOMAINS:-}" ]; then
    read -rp "$(echo -e "${CYAN}Email${NC} for Let's Encrypt registration: ")" ANGINX_EMAIL
    [ -z "$ANGINX_EMAIL" ] && warn "No email provided — certbot will use --register-unsafely-without-email"
fi
[ -n "${ANGINX_EMAIL:-}" ] && ok "Email: $ANGINX_EMAIL"

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
ANGINX_DOMAINS=${ANGINX_DOMAINS:-}
ANGINX_EMAIL=${ANGINX_EMAIL:-}
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
    if [ "$ATTEMPTS" -ge 60 ]; then
        echo ""
        warn "Health check timed out after 30s. Check logs:"
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
if [ -n "${ANGINX_DOMAINS:-}" ]; then
    echo "  Cert acquisition runs in the background — check progress:"
    echo "    ${DOCKER_CMD} compose logs -f anginx | grep cert-manager"
    echo ""
fi
echo "  Register a service:"
echo "    curl -X POST http://localhost/new/${ANGINX_API_KEY} \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"domain\":\"app.yourdomain.com\",\"port\":7705,\"name\":\"myapp\"}'"
echo ""
echo "  Dashboard: http://localhost/dashboard?key=${ANGINX_API_KEY}"
echo "  Services:  http://localhost/services"
echo "  Logs:      ${DOCKER_CMD} compose logs -f anginx"
echo "  ─────────────────────────────────────────────────"
echo ""
