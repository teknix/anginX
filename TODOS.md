# anginX — Deferred Work

Items not in v1 scope. Ordered by priority.

## P2 — Do soon after v1 ships

### Heartbeat / TTL auto-deregistration
Apps re-POST to `/new/<key>` every 30s. If a domain hasn't been seen in 90s, anginX
deregisters it automatically. Requires APScheduler (or equivalent). Implement after
manual deregistration has been in prod for a cycle.

### SIGTERM handler in anginx-client
Auto-deregister when the upstream process receives SIGTERM. Currently apps must call
`deregister()` explicitly in shutdown handlers. Docker stop sends SIGTERM then SIGKILL
after 10s — tricky with gunicorn's signal propagation. Pin this to the heartbeat TTL
milestone so stale routes self-heal regardless.

### Authorization: Bearer header
Replace URL/query-param key with `Authorization: Bearer <key>` header on POST and
DELETE. Breaking API change — requires major version bump. Do after v1 API stabilizes.
Baked-in nginx log format ($uri not $request_uri) already mitigates query-param leakage;
URL path key in `/new/<key>` is the remaining gap.

### Gunicorn multi-worker + file lock
Enable >1 worker for higher throughput. Requires a file lock around the conf write +
reload path to prevent concurrent reload races. Do after reload debouncing is in place.

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
