# anginX — Development Guide

## How it works

Three processes run inside one Docker container, managed by supervisord:

```
supervisord
├── nginx          — serves port 80 (API proxy + ACME + HTTP→HTTPS) and port 443 (per-service SSL)
├── gunicorn       — Flask API on 127.0.0.1:5000, waits for nginx PID before starting
└── cert-manager   — acquires Let's Encrypt certs at startup, renews every 12h
```

State lives on two named Docker volumes:
- `anginx_conf_d` → `/etc/nginx/conf.d` — one `.conf` per registered service (source of truth)
- `anginx_certs` → `/etc/nginx/certs` — certbot certs (survive rebuilds)

On startup, `app.py:rebuild_registry()` scans every `.conf` file and reconstructs the in-memory registry from the `# anginx:` header line. No database.

---

## Local dev setup

```bash
git clone https://github.com/teknix/anginX
cd anginX
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Run the Flask app directly (no nginx, no certbot):

```bash
ANGINX_API_KEY=devkey flask --app 'app:create_app()' run --port 5000
```

---

## Running tests

```bash
# Unit tests (no Docker required)
pytest tests/unit/

# Integration tests (requires Docker). pytest runs on the HOST against the
# containers exposed on localhost:18080.
cd tests/integration

# One-time: the stack mounts ./test-certs as nginx's certs. Registration 503s
# without a cert matching the test domain, so generate a self-signed pair
# (gitignored — not in a fresh clone):
mkdir -p test-certs/live/app.test.com
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout test-certs/live/app.test.com/privkey.pem \
  -out test-certs/live/app.test.com/fullchain.pem \
  -subj "/CN=app.test.com"

# Bring up, run, tear down:
docker compose -f docker-compose.test.yml up -d --build
pytest test_integration.py
docker compose -f docker-compose.test.yml down -v
```

If `docker compose ... up --build` fails with `compose build requires buildx
0.17.0 or later`, build the image with plain `docker build` first, then `up`
without `--build` (compose reuses the tagged image):

```bash
docker build -t integration-anginx ../..        # from tests/integration/
docker compose -f docker-compose.test.yml up -d  # no --build
```

Unit tests cover validation, conf header parsing, and registry rebuild. Add a test for any new validation rule or API behaviour before shipping.

---

## Project structure

```
app.py                    # Flask API — all routes, validation, conf generation
nginx.conf                # Master nginx config (baked into image on build)
conf.base/_proxy.conf     # Shared proxy headers included by every service block
entrypoint/
  docker-entrypoint.sh    # exec supervisord
  start-gunicorn.sh       # waits for /run/nginx.pid, then starts gunicorn
  cert-manager.sh         # certbot acquire + 12h renewal loop
supervisord.conf          # process manager config
client/                   # drop-in client helpers (copy into downstream projects)
  anginx_client.py        # Python client library
  register_on_start.py    # threaded startup registration with retry
  register.sh             # bash curl helper
tests/
  unit/                   # pytest, no Docker
  integration/            # Docker Compose test stack
```

---

## Making changes

### Flask API (`app.py`)

The generated nginx conf is a single Python f-string in `register_service()`. When changing the conf template, keep in mind:

- Only a port 443 `server` block per service. The `default_server` on port 80 handles HTTP→HTTPS redirect and ACME challenges for all domains — no per-service port 80 block needed.
- The `# anginx:` header line is the registry source of truth. Its format is parsed by `HEADER_RE`. If you add a field, update both the writer and the regex.
- After writing the conf, the API runs `nginx -t` before reloading. If `-t` fails the new conf is rolled back.

### nginx config (`nginx.conf`)

This file is baked into the image at build time. Changes require a rebuild and container restart. The file controls:
- The API proxy locations (`/new/`, `/services`, `/health`, `/dashboard`) — restricted to localhost + private LAN ranges via `conf.base/_lan_only.conf`; the public internet gets 403 on these. ACME and the HTTPS redirect stay public.
- ACME challenge root
- HTTP→HTTPS redirect for all non-service domains
- Log format

### Adding a new environment variable

1. Add it to `create_app()` in `app.py` with an `os.environ.get()` default.
2. Add it to `docker-compose.yml` under `environment:`.
3. Document it in the table in `README.md`.

---

## Deploying to production

Production server: `192.168.3.236` (bosshog). Same directory layout as local.

### Standard deploy (app.py or conf.base changes)

```bash
# 1. Commit and push locally
git add <files>
git commit -m "..."
git push origin master

# 2. Pull and rebuild on production
ssh 192.168.3.236 "cd ~/dev/anginX && git pull origin master"
ssh 192.168.3.236 "cd ~/dev/anginX && docker compose build --no-cache"
ssh 192.168.3.236 "cd ~/dev/anginX && docker compose up -d"

# 3. Verify
ssh 192.168.3.236 "curl -s http://localhost/health"
# → {"status":"ok","nginx":"running","services":N}
```

### After a deploy that changes the conf template

Existing `.conf` files on the volume still use the old format. Re-register each service to regenerate them:

```bash
# Deregister
curl -s -X DELETE "http://localhost/services/<domain>?key=<key>"

# Re-register (use same domain/port/name/host as before)
curl -s -X POST http://localhost/new/<key> \
  -H 'Content-Type: application/json' \
  -d '{"domain":"...","port":...,"name":"...","host":"..."}'
```

Current registered services and their parameters: `GET http://localhost/services?key=<key>`

### nginx.conf or supervisord.conf changes only

These are baked into the image. Rebuild and restart — no re-registration needed because the volume confs are untouched.

### Watching logs

```bash
# All processes (nginx + gunicorn + cert-manager)
ssh 192.168.3.236 "docker compose logs -f anginx"

# Cert acquisition specifically
ssh 192.168.3.236 "docker compose logs -f anginx | grep cert-manager"

# nginx access log only (inside container)
ssh 192.168.3.236 "docker exec anginx tail -f /var/log/nginx/access.log"
```

---

## Troubleshooting

**Registration returns 503 indefinitely**
Cert not acquired. Check `docker compose logs anginx | grep cert-manager`. Verify the domain is in `ANGINX_DOMAINS`, DNS A record points to this server, and port 80 is reachable from the internet.

**nginx -t fails after registration**
The API rolls back the conf file automatically and returns HTTP 400 with nginx's stderr. Fix the payload causing the bad config.

**Cloudflare 522 (TCP timeout) despite correct pfSense NAT rule**
Check **Firewall → NAT → Port Forward rule ORDER**. pfSense is first-match — a stale rule for another host above the anginX rule silently intercepts all matching traffic. Also check **Firewall → Rules → WAN** for "Easy Rule: Blocked" entries that may have captured a Cloudflare IP from the log UI. Use the pfSense filter reload log (`Diagnostics → Filter Reload`) to spot unexpected hostnames in NAT rule entries.

**Services missing from registry after restart**
The registry is rebuilt from `.conf` files on the volume. If a conf file is missing (e.g. volume was wiped), re-register the service. `docker compose down -v` destroys volumes including certs — avoid unless intentional.

**Container won't start — bad conf on volume**
A malformed `.conf` from a previous run causes `nginx -t` to fail at startup. Remove the offending file:

```bash
docker run --rm -v anginx_conf_d:/conf alpine rm /conf/<bad-file>.conf
docker compose up -d
```

**`sudo` not working on bosshog**
There is a syntax error in `/etc/sudoers` (near line 26). Don't rely on sudo for any deployment steps. Docker commands run fine as `teknix` via group membership.
