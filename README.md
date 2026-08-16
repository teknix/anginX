# anginX

Self-registering nginx reverse proxy. Docker apps POST their domain and port; anginX writes a validated nginx server block, tests it, and reloads — no manual config.

---

## How it works

```
your-app  ──POST /new (Bearer key)──▶  anginX  ──writes──▶  conf.d/myapp.email.1.com.conf
                                   │
                               nginx -t
                               nginx -s reload
                                   │
internet ──HTTPS──▶  email.1.com  ─▶  your-app:7805
```

- Port 80: API control plane + ACME challenge + HTTP→HTTPS redirect
- Port 443: dynamically generated per-service HTTPS server blocks
- Certs: acquired automatically via Let's Encrypt (certbot HTTP-01 webroot)
- State: nginx conf files are the source of truth; in-memory registry rebuilt from them on restart

---

## Quick start

```bash
git clone https://github.com/teknix/anginX
cd anginX
./init.sh
```

`init.sh` installs Docker if needed, prompts for API key / domains / email, writes `.env`, builds, and starts the container.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANGINX_API_KEY` | yes | Secret key for all API calls |
| `ANGINX_DOMAINS` | yes | Comma-separated list of exact domains to obtain certs for (e.g. `email.1.com,app.1.com`) |
| `ANGINX_EMAIL` | yes | Email for Let's Encrypt registration |
| `ANGINX_MAX_SERVICES` | no | Max registered services (default: 100) |
| `ANGINX_TTL` | no | Seconds before a non-heartbeating service is reaped (default: 90, `0` disables) |
| `ANGINX_REAP_INTERVAL` | no | Reaper scan interval in seconds (default: 30) |
| `ANGINX_ALLOW_HTTP` | no | `1` lets you register a domain before its cert exists — served over HTTP, auto-upgraded to HTTPS when a cert appears (default: `0`, which returns 503 until the cert is ready) |
| `ANGINX_DNS01_DOMAINS` | no | Comma-separated subset of `ANGINX_DOMAINS` to validate via **DNS-01** instead of HTTP-01. For zones whose WAF/CDN interferes with the HTTP challenge. Requires the `certbot-dns-cloudflare` plugin and credentials |
| `ANGINX_CF_API_TOKEN` | no | Cloudflare API token (`Zone:DNS:Edit` on the zone). Written to the credentials file at startup if that file does not already exist |
| `ANGINX_CF_CREDENTIALS` | no | Path to the `dns_cloudflare_api_token = …` ini (default: `/etc/nginx/certs/cloudflare.ini`, i.e. inside the certs volume so it survives rebuilds) |
| `ANGINX_DNS01_PROPAGATION` | no | Seconds to wait for DNS propagation before validation (default: `30`) |

`.env` example:
```
ANGINX_API_KEY=0vqf1Njp70lNzpp6eWTEhG2t2IMhtFeK
ANGINX_DOMAINS=email.1.com,app.1.com
ANGINX_EMAIL=admin@1.com
ANGINX_MAX_SERVICES=100
```

### When HTTP-01 will not work (DNS-01)

Most domains behind a CDN are fine — the proxy passes `/.well-known/acme-challenge/` through.
But a zone-level WAF, bot-protection or geo/firewall rule can **403 the challenge for some of
Let's Encrypt's validation vantage points while serving it perfectly to others**. LE validates
from multiple perspectives and fails the order if any secondary perspective fails, so this shows
up as an *unreproducible* failure: the challenge URL returns `200` from every machine you own,
yet issuance fails with `During secondary validation: … 403`.

Rather than trying to reverse-engineer the CDN rule, move that one domain to DNS-01 — it never
touches the HTTP edge:

```
ANGINX_DNS01_DOMAINS=stubborn.example.com
ANGINX_CF_API_TOKEN=<token with Zone:DNS:Edit on that zone>
```

Only list domains that need it: HTTP-01 requires no secret, so every domain you add here widens
the blast radius of an API token sitting in the container. Domains not listed keep using HTTP-01.

If the plugin or the credentials are missing, that domain is **skipped with an explicit error and
its existing cert is left intact** — it deliberately does *not* fall back to HTTP-01, since the
only reason to be on this list is that HTTP-01 does not work, and a silent fallback would just
burn Let's Encrypt failed-validation rate limit.

Renewals need no extra config: certbot records the authenticator in the renewal conf, so the 12 h
renew loop reuses DNS-01 (and the credentials path) automatically.

> **Heartbeat required.** With `ANGINX_TTL > 0` (the default), services must re-POST
> to `/new` before the TTL expires or their route is removed. `register_on_start.py`
> heartbeats automatically; if you register by hand or with the synchronous `anginx_client`,
> loop the call yourself or set `ANGINX_TTL=0`.

---

## API reference

### Register a service
```
POST /new
Authorization: Bearer <key>
Content-Type: application/json

{
  "domain": "email.1.com",   # FQDN the service is reachable at
  "port":   7805,             # port the upstream listens on
  "name":   "emailbox",       # label — used in conf filename and Docker DNS
  "host":   "192.168.1.50"   # optional: upstream IP or hostname
                               # omit if upstream is a Docker container on the same network
}
```

**Responses**

| Code | Meaning |
|------|---------|
| 200 | Registered. Returns service object. |
| 400 | Validation error (bad domain, port, name). Fix the payload. |
| 401 | Wrong API key. |
| 429 | Max services cap reached. |
| 503 | Cert not yet acquired for this domain. Retry after a few seconds. (Suppressed when `ANGINX_ALLOW_HTTP=1` — the service is served over HTTP instead and auto-upgrades to HTTPS once a cert appears.) |

### Deregister a service
```
DELETE /services/<domain>?key=<key>
```

### List services
```
GET /services?key=<key>
```

### Health check
```
GET /health
```
Returns `{"status":"ok","nginx":"running","services":N}` or 503.

### Dashboard
```
GET /dashboard?key=<key>
```

---

## Adding anginX to a new project

### 1. docker-compose.yml

Connect your app to anginX's network and add it as a dependency:

```yaml
services:
  myapp:
    build: .
    container_name: myapp
    networks:
      - anginx_app_network   # shared network with anginX
    depends_on:
      anginx:
        condition: service_healthy

networks:
  anginx_app_network:
    external: true           # created by anginX's docker-compose.yml
```

### 2. Python — drop-in startup registration

Copy `client/register_on_start.py` into your project and call it at startup:

```python
from register_on_start import register_with_anginx

# In your app factory or __main__:
register_with_anginx(
    anginx_url='http://anginx',
    key='YOUR_ANGINX_API_KEY',
    domain='email.1.com',
    port=7805,
    name='emailbox',
    # host='192.168.1.50',  # only needed for LAN hosts not on Docker network
)
```

Or use the full client library (`client/anginx_client.py`):

```python
from anginx_client import register, deregister, AnginxError

register('http://anginx', key, 'email.1.com', 7805, 'emailbox')
```

### 3. Shell — one-liner registration

```bash
# Docker same-network
curl -s -X POST http://anginx/new \
  -H "Authorization: Bearer $ANGINX_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"domain":"email.1.com","port":7805,"name":"emailbox"}'

# LAN host (service on a different machine)
curl -s -X POST http://YOUR_SERVER_IP/new \
  -H "Authorization: Bearer $ANGINX_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"domain":"email.1.com","port":7805,"name":"emailbox","host":"192.168.1.50"}'
```

Or use `client/register.sh`:

```bash
./client/register.sh email.1.com 7805 emailbox
```

---

## SSL / Cert management

- Certs are acquired automatically at startup for each domain in `ANGINX_DOMAINS`
- Stored in a named Docker volume (`anginx_certs`) — survive container restarts and rebuilds
- Renewed automatically every 12 hours via `certbot renew`
- **Cloudflare proxy (orange cloud) is supported** — Cloudflare passes `.well-known/acme-challenge/` requests through to the origin without caching
- Each service domain needs its own cert. `ANGINX_DOMAINS` must list every subdomain that will receive traffic

Watch cert acquisition:
```bash
docker compose logs -f anginx | grep cert-manager
```

If a cert fails to acquire (DNS not pointing to server, port 80 blocked), restart after fixing:
```bash
docker compose restart anginx
```

---

## Troubleshooting

**Registration returns 503**
Cert not yet acquired. Check `docker compose logs anginx | grep cert-manager`. Verify the domain in `ANGINX_DOMAINS` and that DNS points to this server.

**Registration returns 400**
Validation error. Check the response body — your app should log it:
```python
except urllib.error.HTTPError as e:
    print(e.read().decode())  # shows {"error": "..."}
```

**nginx fails to start**
Check logs: `docker compose logs anginx | grep -i error`. Most common cause: bad conf file from a previous run on the named volume. Remove the conf volume and restart:
```bash
docker compose down -v  # WARNING: removes volumes including certs
docker compose up -d
```

**libexpat / pip crash during build**
Use `FROM alpine:3.21` as the base image, not `nginx:1.27-alpine`. The nginx.org apk repo ships an older libexpat that conflicts with Python 3.12. See Dockerfile.

---

## Architecture

```
/
├── app.py                    # Flask API (create_app factory)
├── nginx.conf                # Master nginx config (baked into image)
├── conf.base/
│   └── _proxy.conf           # Shared proxy headers (baked into image)
├── entrypoint/
│   ├── docker-entrypoint.sh  # Entrypoint → exec supervisord
│   ├── start-gunicorn.sh     # Waits for nginx PID then starts gunicorn
│   └── cert-manager.sh       # Acquires/renews certs, supervised by supervisord
├── supervisord.conf          # Manages: nginx, gunicorn, cert-manager
├── client/
│   ├── anginx_client.py      # Python client library (no dependencies)
│   ├── register_on_start.py  # Drop-in threaded startup registration with retry
│   └── register.sh           # Bash registration helper
├── init.sh                   # First-run setup script
└── docker-compose.yml        # Single-service: anginx only

Docker volumes:
  anginx_conf_d  → /etc/nginx/conf.d   (per-service nginx confs, runtime)
  anginx_certs   → /etc/nginx/certs    (certbot certs, persistent)

Network: anginx_app_network (bridge) — upstream services join this
```

---

## Nginx conf generated per service

```nginx
# anginx: domain=email.1.com port=7805 name=emailbox host=emailbox registered_at=2026-05-23T...
server {
    listen 443 ssl;
    server_name email.1.com;
    ssl_certificate /etc/nginx/certs/live/email.1.com/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/live/email.1.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    include /etc/nginx/conf.base/_proxy.conf;
    resolver 127.0.0.11 valid=10s;
    location / {
        set $upstream http://emailbox:7805;
        proxy_pass $upstream;
    }
}
```

HTTP → HTTPS redirects and ACME challenge handling are in the `default_server` block in `nginx.conf` and apply to all domains automatically — no per-service port 80 block needed.

The `# anginx:` header is the single source of truth — the in-memory registry is rebuilt from conf files on startup.
