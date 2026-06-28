"""
Drop-in threaded startup registration for Docker apps using anginX.

Copy this file into your project and call register_with_anginx() at startup.
No third-party dependencies — stdlib only.

Usage (Docker, same compose network):
    from register_on_start import register_with_anginx
    register_with_anginx(
        anginx_url='http://anginx',
        key='YOUR_ANGINX_API_KEY',
        domain='app.1.com',
        port=7705,
        name='myapp',
    )

Usage (LAN host — service on a different machine):
    register_with_anginx(
        anginx_url='http://192.168.1.10',
        key='YOUR_ANGINX_API_KEY',
        domain='app.1.com',
        port=7705,
        name='myapp',
        host='192.168.1.50',
    )
"""

import json
import threading
import time
import urllib.error
import urllib.request


def register_with_anginx(
    anginx_url,
    key,
    domain,
    port,
    name,
    host=None,
    max_attempts=20,
    retry_delay=5,
    cert_retry_delay=10,
    startup_delay=2,
    heartbeat_interval=30,
):
    """
    Register this service with anginX in a background daemon thread, then
    heartbeat to keep the route alive against anginX's TTL reaper.

    Retries on transient errors and 503 (cert not yet acquired).
    Returns immediately; registration happens in the background.

    anginx_url      -- base URL of anginX, e.g. "http://anginx" or "http://192.168.1.10"
    key             -- ANGINX_API_KEY
    domain          -- FQDN the service is reachable at
    port            -- port this service listens on
    name            -- label / Docker container name
    host            -- upstream IP or hostname (omit for Docker same-network)
    max_attempts    -- give up after this many tries
    retry_delay     -- seconds between retries on network errors
    cert_retry_delay-- seconds to wait when cert isn't ready yet (503)
    startup_delay   -- seconds to wait before first attempt (let app stabilise)
    heartbeat_interval -- seconds between heartbeats; keep < anginX's ANGINX_TTL
                          (default 90). 0 disables heartbeating.
    """
    def _register():
        time.sleep(startup_delay)
        body = {'domain': domain, 'port': port, 'name': name}
        if host is not None:
            body['host'] = host

        endpoint = f"{anginx_url.rstrip('/')}/new/{key}"
        data = json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'}

        def post_once():
            req = urllib.request.Request(endpoint, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        registered = False
        for attempt in range(1, max_attempts + 1):
            try:
                post_once()
                print(f"[anginx] registered {domain} → {name}:{port} (attempt {attempt})")
                registered = True
                break
            except urllib.error.HTTPError as e:
                body_text = e.read().decode('utf-8', errors='replace')
                try:
                    msg = json.loads(body_text).get('error', body_text)
                except Exception:
                    msg = body_text

                if e.code == 503:
                    print(f"[anginx] cert not ready for {domain}, retrying in {cert_retry_delay}s...")
                    time.sleep(cert_retry_delay)
                elif e.code in (400, 401, 429):
                    print(f"[anginx] registration failed (HTTP {e.code}): {msg} — not retrying")
                    return None
                else:
                    print(f"[anginx] HTTP {e.code}: {msg}, retrying...")
                    time.sleep(retry_delay)
            except Exception as e:
                print(f"[anginx] connection error (attempt {attempt}/{max_attempts}): {e}")
                time.sleep(retry_delay)

        if not registered:
            print(f"[anginx] gave up registering {domain} after {max_attempts} attempts")
            return None

        # Heartbeat — re-POST is a no-op on the server (no reload) unless the
        # route was reaped, in which case it re-creates it. Self-healing.
        if heartbeat_interval <= 0:
            return None
        while True:
            time.sleep(heartbeat_interval)
            try:
                post_once()
            except Exception as e:
                print(f"[anginx] heartbeat failed for {domain}: {e}")

    t = threading.Thread(target=_register, daemon=True, name="anginx-register")
    t.start()
    return t
