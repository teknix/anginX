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
PORT_RANGE = range(1, 65536)

HEADER_RE = re.compile(
    r'^#\s*anginx:\s*domain=(?P<domain>\S+)\s+port=(?P<port>\d+)'
    r'\s+name=(?P<name>\S+)(?:\s+host=(?P<host>\S+))?(?:\s+sse=(?P<sse>\d+))?'
    r'\s+registered_at=(?P<registered_at>\S+)'
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
                for line in f:
                    m = HEADER_RE.match(line)
                    if m:
                        d = m.groupdict()
                        domain = d['domain'].lower()
                        registry[domain] = {
                            'domain': domain,
                            'port': int(d['port']),
                            'name': d['name'],
                            'host': d['host'] or d['name'],  # host absent in old conf files
                            'sse': d['sse'] == '1',
                            'conf_file': filename,
                            'registered_at': d['registered_at'],
                        }
                        break
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

    if config:
        app.config.update(config)

    app.config['_registry'] = rebuild_registry(app.config)
    # last_seen guards the reaper; existing confs get a full grace window at boot.
    app.config['_last_seen'] = {d: time.monotonic() for d in app.config['_registry']}
    app.config['_lock'] = threading.Lock()  # serializes conf write + reload across threads

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
        port   = data.get('port')
        host   = data.get('host', '')  # optional — upstream IP or hostname; defaults to name
        sse    = bool(data.get('sse'))  # streaming endpoint — disable proxy buffering

        try:
            validate_domain(domain)
            validate_name(name)
            port_int = validate_port(port)
            if host:
                validate_host(host)
        except ValidationError as e:
            return jsonify({'error': str(e)}), 400

        domain = domain.lower()
        name   = name.lower()
        host   = host.lower() if host else name  # default upstream = container name

        cert_dir  = os.path.join(app.config['CERTS_DIR'], 'live', domain)
        cert_file = os.path.join(cert_dir, 'fullchain.pem')
        if not os.path.exists(cert_file):
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

        upstream = f"http://{host}:{port_int}"
        # SSE needs unbuffered, keep-alive HTTP/1.1 with a long read timeout
        sse_directives = (
            f"        proxy_buffering off;\n"
            f"        proxy_cache off;\n"
            f"        proxy_http_version 1.1;\n"
            f"        proxy_set_header Connection '';\n"
            f"        proxy_read_timeout 3600s;\n"
        ) if sse else ""
        content = (
            f"# anginx: domain={domain} port={port_int} name={name} host={host}"
            f"{' sse=1' if sse else ''} registered_at={registered_at}\n"
            f"server {{\n"
            f"    listen 443 ssl;\n"
            f"    server_name {domain};\n"
            f"    ssl_certificate {cert_dir}/fullchain.pem;\n"
            f"    ssl_certificate_key {cert_dir}/privkey.pem;\n"
            f"    ssl_protocols TLSv1.2 TLSv1.3;\n"
            f"    ssl_ciphers HIGH:!aNULL:!MD5;\n"
            f"    include {app.config['CONF_BASE']}/_proxy.conf;\n"
            f"    resolver 127.0.0.11 valid=10s;\n"
            f"    location / {{\n"
            f"        set $upstream {upstream};\n"
            f"        proxy_pass $upstream;\n"
            f"{sse_directives}"
            f"    }}\n"
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

            registry[domain] = {
                'domain': domain,
                'port': port_int,
                'name': name,
                'host': host,
                'sse': sse,
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
