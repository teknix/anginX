# anginX — Deferred Work

Items not in v1 scope. Ordered by priority.

## P2 — Do soon after v1 ships

### ~~Heartbeat / TTL auto-deregistration~~ — DONE
Background reaper thread (stdlib, no APScheduler) drops services whose last heartbeat is
older than `ANGINX_TTL` (default 90s); `register_on_start.py` heartbeats every 30s.
`reap_stale()` + `_lock` in `app.py`.

### SIGTERM handler in anginx-client
Auto-deregister when the upstream process receives SIGTERM. Currently apps must call
`deregister()` explicitly in shutdown handlers. Docker stop sends SIGTERM then SIGKILL
after 10s — tricky with gunicorn's signal propagation. Lower priority now that the TTL
reaper self-heals stale routes within `ANGINX_TTL`; SIGTERM just makes cleanup instant.

### ~~Authorization: Bearer header (POST)~~ — DONE
`POST /new` now takes `Authorization: Bearer <key>`; the path key is gone, so the secret
no longer lands in access.log via `$uri`. Breaking change — clients updated in this repo.
DELETE and `/dashboard` still take `?key=`; query strings aren't in the `$uri` log format,
so they don't leak. Move those to the header too only if a real reason appears.

### Gunicorn multi-worker + cross-process lock
Enable >1 worker for higher throughput. The in-process `threading.Lock` added for the
reaper only serializes within one worker; >1 worker needs a file lock (e.g. `flock`)
around the conf write + reload path, plus shared/rebuilt registry + last_seen state.
Do after reload debouncing is in place.

## P3 — Nice to have

### Reload debouncing
Batch registrations within a 1s window into a single `nginx -s reload`. Prevents reload
storms when a fleet of services restarts simultaneously. Single worker already serializes,
so debouncing is purely a performance optimization.

### Auto-SSL via Let's Encrypt
Provision a cert on registration via certbot/ACME. Deferred: wildcard certs already
cover all subdomains per domain. Add when domain proliferation makes manual cert
management painful. Requires certbot, challenge routing, and cert renewal cron.

### PSL library for co.uk-style TLDs
Root domain extraction (last two labels) is wrong for public suffix list TLDs like
`co.uk`, `com.au`, etc. v1 documents this as a known limitation. Add `publicsuffix`
library when a real use case appears.

### Dashboard UI polish
v1 dashboard is a plain monospace table. Future: traffic stats, last-seen timestamps,
service health indicators, dark-mode styling improvements.
