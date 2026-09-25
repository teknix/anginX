import os
import re
import hmac
import time
import errno
import threading
import subprocess
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template

DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$')
NAME_RE   = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')
# host: IPv4, LAN hostname, or Docker container name — dots/colons allowed, no shell-unsafe chars
HOST_RE   = re.compile(r'^[a-z0-9][a-z0-9._:-]{0,252}$')
# path: must start with '/'; printable, no whitespace, no '..' traversal (checked separately)
PATH_RE   = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
PORT_RANGE = range(1, 65536)
MAX_PATHS = 8  # per-domain cap on the `paths` array (path-routing registrations)

# Single-location registration (the common case): one header line on the conf file.
# `ws=` was added after `sse=`; its absence on older conf files is fine — the group is optional.
HEADER_RE = re.compile(
    r'^#\s*anginx:\s*domain=(?P<domain>\S+)\s+port=(?P<port>\d+)'
    r'\s+name=(?P<name>\S+)(?:\s+host=(?P<host>\S+))?(?:\s+sse=(?P<sse>\d+))?'
    r'(?:\s+ws=(?P<ws>\d+))?\s+registered_at=(?P<registered_at>\S+)'
)

# Multi-location (path-routing) registration: one header line + one `anginx-path:` line per path.
MULTI_HEADER_RE = re.compile(
    r'^#\s*anginx:\s*domain=(?P<domain>\S+)\s+name=(?P<name>\S+)'
    r'\s+paths=(?P<count>\d+)\s+registered_at=(?P<registered_at>\S+)'
)
PATH_LINE_RE = re.compile(
    r'^#\s*anginx-path:\s*path=(?P<path>\S+)\s+host=(?P<host>\S+)'
    r'\s+port=(?P<port>\d+)\s+ws=(?P<ws>\d+)\s+sse=(?P<sse>\d+)'
)


class ValidationError(Exception):
    pass


def validate_domain(domain):
    if not domain:
        raise ValidationError("domain is required")
    d = domain.lower()
    if not DOMAIN_RE.match(d):
        raise ValidationError("invalid domain characters or length")
    if len(d.split('.')) < 2:
        raise ValidationError("domain must be a fully qualified domain name (e.g. app.1.com)")


def validate_name(name):
    if not name:
        raise ValidationError("name is required")
    if not NAME_RE.match(name.lower()):
        raise ValidationError("invalid container name — lowercase letters, digits, hyphens, underscores only")


def validate_host(host):
    if not host:
        raise ValidationError("host cannot be empty")
    if not HOST_RE.match(host.lower()):
        raise ValidationError("invalid host — use an IP address (192.168.1.5), hostname, or container name")


def validate_port(port):
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValidationError("port must be an integer")
    if p not in PORT_RANGE:
        raise ValidationError("port must be in range 1–65535")
    return p


def validate_path(path):
    if not path:
        raise ValidationError("path is required")
    if not isinstance(path, str) or len(path) > 200:
        raise ValidationError("invalid path — must be a string of at most 200 characters")
    if '..' in path or any(c in path for c in ('\n', '\r', ' ', '\t')):
        raise ValidationError("invalid path characters")
    if not PATH_RE.match(path):
        raise ValidationError("invalid path — must start with '/' and use URL path characters only")


def check_nginx(conf):
    pid_file = conf.get('NGINX_PID', '/run/nginx.pid')
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False



def _parse_conf_lines(lines, filename):
    """Parse one conf file's header (already-read lines) into a registry entry, or None."""
    for i, line in enumerate(lines):
        m = MULTI_HEADER_RE.match(line)
        if m:
            d = m.groupdict()
            domain = d['domain'].lower()
            count = int(d['count'])
            paths = []
            for path_line in lines[i + 1:i + 1 + count]:
                pm = PATH_LINE_RE.match(path_line)
                if not pm:
                    break
                pd = pm.groupdict()
                paths.append({
                    'path': pd['path'],
                    'host': pd['host'],
                    'port': int(pd['port']),
                    'ws': pd['ws'] == '1',
                    'sse': pd['sse'] == '1',
                })
            if len(paths) != count:
                print(f"[anginx] warning: {filename} declared paths={count} but only "
                      f"{len(paths)} parsed — skipping")
                return None
            return domain, {
                'domain': domain,
                'name': d['name'],
                'paths': paths,
                'conf_file': filename,
                'registered_at': d['registered_at'],
            }
        m = HEADER_RE.match(line)
        if m:
            d = m.groupdict()
            domain = d['domain'].lower()
            return domain, {
                'domain': domain,
                'port': int(d['port']),
                'name': d['name'],
                'host': d['host'] or d['name'],  # host absent in old conf files
                'sse': d['sse'] == '1',
                'ws': d['ws'] == '1',
                'conf_file': filename,
                'registered_at': d['registered_at'],
            }
    return None


def rebuild_registry(conf):
    conf_d = conf.get('CONF_D', '/etc/nginx/conf.d')
    registry = {}
    if not os.path.isdir(conf_d):
        return registry
    for filename in sorted(os.listdir(conf_d)):
        if not filename.endswith('.conf'):
            continue
        if filename.startswith('_ssl_'):
            continue
        filepath = os.path.join(conf_d, filename)
        try:
            with open(filepath) as f:
                lines = f.readlines()
            parsed = _parse_conf_lines(lines, filename)
            if parsed:
                domain, entry = parsed
                registry[domain] = entry
        except Exception as e:
            print(f"[anginx] warning: could not parse {filename}: {e}")
    return registry


def reap_stale(app):
    """Remove services whose last heartbeat is older than ANGINX_TTL.

    Reloads nginx once for the whole batch. Returns the reaped domains.
    Takes the same lock as register/deregister so writes never interleave.
    """
    ttl = app.config['ANGINX_TTL']
    if ttl <= 0:
        return []
    now = time.monotonic()
    registry  = app.config['_registry']
    last_seen = app.config['_last_seen']
    removed = []
    with app.config['_lock']:
        stale = [d for d in list(registry)
                 if now - last_seen.get(d, now) > ttl]
        for domain in stale:
            conf_path = os.path.join(app.config['CONF_D'], registry[domain]['conf_file'])
            try:
                if os.path.exists(conf_path):
                    os.remove(conf_path)
            except OSError as e:
                print(f"[anginx] reaper: could not remove {conf_path}: {e}")
                continue
            registry.pop(domain, None)
            last_seen.pop(domain, None)
            removed.append(domain)
        if removed:
            try:
                subprocess.run(['nginx', '-s', 'reload'],
                               check=True, capture_output=True, timeout=5)
            except Exception as e:
                print(f"[anginx] reaper: nginx reload failed: {e}")
    return removed


def _render_location(conf_base, path, host, port, ws, sse, lan):
    """Render one `location <path> { ... }` block."""
    upstream = f"http://{host}:{port}"
    lan_directive = f"        include {conf_base}/_lan_only.conf;\n" if lan else ""
    ws_directives = (
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Upgrade $http_upgrade;\n"
        f"        proxy_set_header Connection $connection_upgrade;\n"
    ) if ws else ""
    # SSE needs unbuffered, keep-alive HTTP/1.1 with a long read timeout.
    # (ws and sse are mutually exclusive — validated by the caller — so this never
    # doubles up proxy_http_version / stomps ws's Connection header.)
    sse_directives = (
        f"        proxy_buffering off;\n"
        f"        proxy_cache off;\n"
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Connection '';\n"
        f"        proxy_read_timeout 3600s;\n"
    ) if sse else ""
    return (
        f"    location {path} {{\n"
        f"{lan_directive}"
        f"        set $upstream {upstream};\n"
        f"        proxy_pass $upstream;\n"
        f"{ws_directives}"
        f"{sse_directives}"
        f"    }}\n"
    )


def _parse_paths_field(paths_data):
    """Validate the `paths` array for a path-routing registration. Returns a normalized,
    path-sorted list of dicts, or raises ValidationError."""
    if not isinstance(paths_data, list) or not paths_data:
        raise ValidationError("paths must be a non-empty array")
    if len(paths_data) > MAX_PATHS:
        raise ValidationError(f"too many paths — max {MAX_PATHS} per domain")

    seen = set()
    parsed = []
    for entry in paths_data:
        if not isinstance(entry, dict):
            raise ValidationError("each path entry must be an object")
        path = entry.get('path', '')
        host = entry.get('host', '')
        port = entry.get('port')
        ws   = bool(entry.get('ws'))
        sse  = bool(entry.get('sse'))

        validate_path(path)
        port_int = validate_port(port)
        if host:
            validate_host(host)
        if ws and sse:
            raise ValidationError(f"path {path}: ws and sse are mutually exclusive")
        if path in seen:
            raise ValidationError(f"duplicate path: {path}")
        seen.add(path)
        parsed.append({
            'path': path,
            'host': host.lower() if host else None,  # filled in with the domain's `name` by the caller
            'port': port_int,
            'ws': ws,
            'sse': sse,
        })
    parsed.sort(key=lambda p: p['path'])
    return parsed


def _reaper_loop(app):
    interval = app.config['ANGINX_REAP_INTERVAL']
    while True:
        time.sleep(interval)
        try:
            for domain in reap_stale(app):
                print(f"[anginx] reaped stale service: {domain}")
        except Exception as e:
            print(f"[anginx] reaper error: {e}")


def create_app(config=None):
    app = Flask(__name__)

    app.config['ANGINX_API_KEY']      = os.environ.get('ANGINX_API_KEY', '')
    app.config['ANGINX_MAX_SERVICES'] = int(os.environ.get('ANGINX_MAX_SERVICES', '100'))
    app.config['CONF_D']              = os.environ.get('CONF_D', '/etc/nginx/conf.d')
    app.config['CONF_BASE']           = os.environ.get('CONF_BASE', '/etc/nginx/conf.base')
    app.config['CERTS_DIR']           = os.environ.get('CERTS_DIR', '/etc/nginx/certs')
    app.config['NGINX_PID']           = os.environ.get('NGINX_PID', '/run/nginx.pid')
    # TTL reaper: drop services that stop heartbeating. 0 disables.
    app.config['ANGINX_TTL']           = int(os.environ.get('ANGINX_TTL', '90'))
    app.config['ANGINX_REAP_INTERVAL'] = int(os.environ.get('ANGINX_REAP_INTERVAL', '30'))
    # Allow registering a domain before its cert exists: serve it over plain HTTP
    # (keeping the ACME path open) and auto-upgrade to HTTPS once a cert appears.
    app.config['ANGINX_ALLOW_HTTP']    = os.environ.get('ANGINX_ALLOW_HTTP', '0') == '1'
    # Domains only LAN/WireGuard clients may reach (conf.base/_lan_only.conf). Enforced here, not
    # by the client, so a heartbeat that omits the flag can't open a LAN service to the internet.
    # Env list plus <CERTS_DIR>/lan-only-domains, re-read on every registration (no restart).
    app.config['ANGINX_LAN_ONLY_DOMAINS'] = os.environ.get('ANGINX_LAN_ONLY_DOMAINS', '')

    if config:
        app.config.update(config)

    app.config['_registry'] = rebuild_registry(app.config)
    # last_seen guards the reaper; existing confs get a full grace window at boot.
    app.config['_last_seen'] = {d: time.monotonic() for d in app.config['_registry']}
    app.config['_lock'] = threading.Lock()  # serializes conf write + reload across threads

    def lan_only_domains():
        names = app.config['ANGINX_LAN_ONLY_DOMAINS'].replace(',', ' ').split()
        path = os.path.join(app.config['CERTS_DIR'], 'lan-only-domains')
        try:
            with open(path) as f:
                names += f.read().replace(',', ' ').split()
        except OSError:
            pass
        return {n.strip().lower() for n in names if n.strip()}

    def key_ok(provided):
        return hmac.compare_digest(provided or '', app.config['ANGINX_API_KEY'])

    def bearer_key():
        auth = request.headers.get('Authorization', '')
        return auth[7:] if auth.startswith('Bearer ') else ''

    @app.route('/new', methods=['POST'])
    def register_service():
        # Key travels in the Authorization header, not the URL — the path key
        # used to land in access.log via $uri.
        if not key_ok(bearer_key()):
            return jsonify({'error': 'invalid API key'}), 401

        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'request body must be JSON'}), 400

        domain = data.get('domain', '')
        name   = data.get('name', '')
        lan    = bool(data.get('lan_only'))  # client may ask; lan_only_domains() is checked below
        multi  = 'paths' in data  # path-routing registration — see _parse_paths_field

        try:
            validate_domain(domain)
            validate_name(name)
            if multi:
                paths = _parse_paths_field(data.get('paths'))
            else:
                port = data.get('port')
                host = data.get('host', '')  # optional — upstream IP or hostname; defaults to name
                sse  = bool(data.get('sse'))  # streaming endpoint — disable proxy buffering
                ws   = bool(data.get('ws'))   # WebSocket endpoint — upgrade headers
                port_int = validate_port(port)
                if host:
                    validate_host(host)
                if ws and sse:
                    raise ValidationError("ws and sse are mutually exclusive")
        except ValidationError as e:
            return jsonify({'error': str(e)}), 400

        domain = domain.lower()
        name   = name.lower()
        if multi:
            for p in paths:
                if p['host'] is None:
                    p['host'] = name  # default upstream = container name, per path
        else:
            host = host.lower() if host else name  # default upstream = container name

        cert_dir  = os.path.join(app.config['CERTS_DIR'], 'live', domain)
        cert_file = os.path.join(cert_dir, 'fullchain.pem')
        has_cert  = os.path.exists(cert_file)
        if not has_cert and not app.config['ANGINX_ALLOW_HTTP']:
            return jsonify({'error': f"no certificate for {domain} — cert-manager may still be acquiring it"}), 503

        registry  = app.config['_registry']
        last_seen = app.config['_last_seen']
        if domain not in registry and len(registry) >= app.config['ANGINX_MAX_SERVICES']:
            return jsonify({'error': 'max services cap reached'}), 429

        conf_filename = f"{name}.{domain}.conf"
        conf_path = os.path.join(app.config['CONF_D'], conf_filename)
        tmp_path  = conf_path + '.tmp'

        # Preserve the original registration time so an unchanged heartbeat
        # produces byte-identical conf and can skip the reload.
        existing = registry.get(domain)
        registered_at = existing['registered_at'] if existing else \
            datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        conf_base = app.config['CONF_BASE']
        lan = lan or domain in lan_only_domains()

        if multi:
            header = (
                f"# anginx: domain={domain} name={name} paths={len(paths)} "
                f"registered_at={registered_at}\n"
            )
            for p in paths:
                header += (
                    f"# anginx-path: path={p['path']} host={p['host']} port={p['port']} "
                    f"ws={'1' if p['ws'] else '0'} sse={'1' if p['sse'] else '0'}\n"
                )
            proxy_location = (
                f"    include {conf_base}/_proxy.conf;\n"
                f"    resolver 127.0.0.11 valid=10s;\n"
            ) + "".join(
                _render_location(conf_base, p['path'], p['host'], p['port'], p['ws'], p['sse'], lan)
                for p in paths
            )
        else:
            header = (
                f"# anginx: domain={domain} port={port_int} name={name} host={host}"
                f"{' sse=1' if sse else ''}{' ws=1' if ws else ''} registered_at={registered_at}\n"
            )
            proxy_location = (
                f"    include {conf_base}/_proxy.conf;\n"
                f"    resolver 127.0.0.11 valid=10s;\n"
            ) + _render_location(conf_base, '/', host, port_int, ws, sse, lan)

        if has_cert:
            content = (
                f"{header}"
                f"server {{\n"
                f"    listen 443 ssl;\n"
                f"    server_name {domain};\n"
                f"    ssl_certificate {cert_dir}/fullchain.pem;\n"
                f"    ssl_certificate_key {cert_dir}/privkey.pem;\n"
                f"    ssl_protocols TLSv1.2 TLSv1.3;\n"
                f"    ssl_ciphers HIGH:!aNULL:!MD5;\n"
                f"{proxy_location}"
                f"}}\n"
            )
        else:
            # No cert yet — serve over HTTP. The ACME location stays open so a cert
            # can still be acquired; the next re-register auto-upgrades this to 443.
            content = (
                f"{header}"
                f"server {{\n"
                f"    listen 80;\n"
                f"    server_name {domain};\n"
                f"    location /.well-known/acme-challenge/ {{ root /var/www/acme; }}\n"
                f"{proxy_location}"
                f"}}\n"
            )

        # Read original for rollback / heartbeat short-circuit
        original = None
        if os.path.exists(conf_path):
            try:
                with open(conf_path) as f:
                    original = f.read()
            except Exception:
                pass

        # Heartbeat: an unchanged re-registration just refreshes last_seen — no reload.
        if existing and original == content:
            last_seen[domain] = time.monotonic()
            return jsonify(existing), 200

        with app.config['_lock']:
            try:
                with open(tmp_path, 'w') as f:
                    f.write(content)
                os.rename(tmp_path, conf_path)
            except PermissionError:
                return jsonify({'error': 'conf.d not writable — check volume mount'}), 500
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    return jsonify({'error': 'disk full'}), 500
                return jsonify({'error': str(e)}), 500

            def rollback():
                if original is not None:
                    try:
                        with open(conf_path, 'w') as f:
                            f.write(original)
                    except Exception:
                        pass
                elif os.path.exists(conf_path):
                    try:
                        os.remove(conf_path)
                    except Exception:
                        pass

            try:
                subprocess.run(
                    ['nginx', '-t'], check=True, capture_output=True, timeout=5
                )
            except subprocess.TimeoutExpired:
                rollback()
                return jsonify({'error': 'nginx -t timed out'}), 500
            except subprocess.CalledProcessError as e:
                rollback()
                stderr = e.stderr.decode('utf-8', errors='replace')
                return jsonify({'error': f"nginx config invalid: {stderr}"}), 400

            try:
                subprocess.run(
                    ['nginx', '-s', 'reload'], check=True, capture_output=True, timeout=5
                )
            except Exception as e:
                rollback()  # keep conf.d in sync with the in-memory registry
                return jsonify({'error': f"nginx reload failed: {e}"}), 500

            if multi:
                registry[domain] = {
                    'domain': domain,
                    'name': name,
                    'paths': paths,
                    'conf_file': conf_filename,
                    'registered_at': registered_at,
                }
            else:
                registry[domain] = {
                    'domain': domain,
                    'port': port_int,
                    'name': name,
                    'host': host,
                    'sse': sse,
                    'ws': ws,
                    'conf_file': conf_filename,
                    'registered_at': registered_at,
                }
            last_seen[domain] = time.monotonic()

        return jsonify(registry[domain]), 200

    @app.route('/services', methods=['GET'])
    def get_services():
        if not key_ok(request.args.get('key', '')):
            return jsonify({'error': 'invalid API key'}), 401
        return jsonify(list(app.config['_registry'].values())), 200

    @app.route('/services/<domain>', methods=['DELETE'])
    def deregister_service(domain):
        if not key_ok(request.args.get('key', '')):
            return jsonify({'error': 'invalid API key'}), 401

        domain = domain.lower()
        registry = app.config['_registry']
        if domain not in registry:
            return jsonify({'error': f"'{domain}' is not registered"}), 404

        service  = registry[domain]
        conf_path = os.path.join(app.config['CONF_D'], service['conf_file'])

        with app.config['_lock']:
            original = None
            if os.path.exists(conf_path):
                try:
                    with open(conf_path) as f:
                        original = f.read()
                except Exception:
                    pass

            try:
                if os.path.exists(conf_path):
                    os.remove(conf_path)
            except PermissionError:
                return jsonify({'error': 'conf.d not writable — check volume mount'}), 500
            except OSError as e:
                return jsonify({'error': str(e)}), 500

            def restore():
                if original is not None:
                    try:
                        with open(conf_path, 'w') as f:
                            f.write(original)
                    except Exception:
                        pass

            try:
                subprocess.run(
                    ['nginx', '-t'], check=True, capture_output=True, timeout=5
                )
            except subprocess.TimeoutExpired:
                restore()
                return jsonify({'error': 'nginx -t timed out'}), 500
            except subprocess.CalledProcessError as e:
                restore()
                stderr = e.stderr.decode('utf-8', errors='replace')
                return jsonify({'error': f"nginx config invalid after delete: {stderr}"}), 500

            try:
                subprocess.run(
                    ['nginx', '-s', 'reload'], check=True, capture_output=True, timeout=5
                )
            except Exception as e:
                restore()
                return jsonify({'error': f"nginx reload failed: {e}"}), 500

            registry.pop(domain, None)
            app.config['_last_seen'].pop(domain, None)
        return jsonify({'domain': domain, 'removed': True}), 200

    @app.route('/health', methods=['GET'])
    def health():
        if check_nginx(app.config):
            return jsonify({
                'status': 'ok',
                'nginx': 'running',
                'services': len(app.config['_registry']),
            }), 200
        return jsonify({'status': 'degraded', 'nginx': 'not running'}), 503

    @app.route('/dashboard', methods=['GET'])
    def dashboard():
        if not key_ok(request.args.get('key', '')):
            return 'invalid API key', 401
        services = list(app.config['_registry'].values())
        return render_template('dashboard.html', services=services)

    # Background reaper — skipped under tests (call reap_stale directly instead).
    if app.config['ANGINX_TTL'] > 0 and not app.config.get('TESTING'):
        threading.Thread(target=_reaper_loop, args=(app,), daemon=True).start()

    return app
