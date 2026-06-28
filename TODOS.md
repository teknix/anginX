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

### Authorization: Bearer header
Replace URL/query-param key with `Authorization: Bearer <key>` header on POST and
DELETE. Breaking API change — requires major version bump. Do after v1 API stabilizes.
The `$uri` log format keeps the query-param key out of access.log, but the path key in
`POST /new/<key>` IS captured by `$uri` and written to access.log — that's the real
remaining leak this fixes. (Control plane is LAN-only, so logs are local, but still.)

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
