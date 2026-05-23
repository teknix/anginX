import time
import urllib.request
import urllib.error
import json


class AnginxError(Exception):
    pass


def _request(method, url, key=None, body=None, retries=3):
    delay = 1.0
    last_err = None
    for attempt in range(retries):
        try:
            data = json.dumps(body).encode() if body else None
            headers = {'Content-Type': 'application/json'} if data else {}
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode('utf-8', errors='replace')
            try:
                msg = json.loads(body_text).get('error', body_text)
            except Exception:
                msg = body_text
            last_err = AnginxError(f"HTTP {e.code}: {msg}")
            if e.code in (400, 401, 404, 429):
                raise last_err
        except Exception as e:
            last_err = AnginxError(str(e))
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    raise last_err


def register(url, key, domain, port, name, host=None, retries=3):
    """
    Register a service with anginX.

    url    -- base URL of anginX, e.g. "http://anginx"
    key    -- ANGINX_API_KEY
    domain -- FQDN the service is reachable at, e.g. "app.1.com"
    port   -- port the upstream listens on
    name   -- label used in conf filename; also Docker DNS name if host omitted
    host   -- upstream IP or hostname (omit for Docker containers on same network)
    """
    endpoint = f"{url.rstrip('/')}/new/{key}"
    body = {'domain': domain, 'port': port, 'name': name}
    if host is not None:
        body['host'] = host
    return _request('POST', endpoint, key=key, body=body, retries=retries)


def deregister(url, key, domain, retries=3):
    """Deregister a service from anginX."""
    endpoint = f"{url.rstrip('/')}/services/{domain}?key={key}"
    _request('DELETE', endpoint, key=key, retries=retries)
