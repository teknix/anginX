import os
import re
import errno
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
    r'\s+name=(?P<name>\S+)(?:\s+host=(?P<host>\S+))?\s+registered_at=(?P<registered_at>\S+)'
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


def extract_root_domain(domain):
    parts = domain.lower().split('.')
    slug = ''.join(parts[-2:])
    return '.'.join(parts[-2:]), slug


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
                            'conf_file': filename,
                            'registered_at': d['registered_at'],
                        }
                        break
        except Exception as e:
            print(f"[anginx] warning: could not parse {filename}: {e}")
    return registry


def create_app(config=None):
    app = Flask(__name__)

    app.config['ANGINX_API_KEY']      = os.environ.get('ANGINX_API_KEY', '')
    app.config['ANGINX_MAX_SERVICES'] = int(os.environ.get('ANGINX_MAX_SERVICES', '100'))
    app.config['CONF_D']              = os.environ.get('CONF_D', '/etc/nginx/conf.d')
    app.config['CONF_BASE']           = os.environ.get('CONF_BASE', '/etc/nginx/conf.base')
    app.config['CERTS_DIR']           = os.environ.get('CERTS_DIR', '/etc/nginx/certs')
    app.config['NGINX_PID']           = os.environ.get('NGINX_PID', '/run/nginx.pid')

    if config:
        app.config.update(config)

    app.config['_registry'] = rebuild_registry(app.config)

    @app.route('/new/<key>', methods=['POST'])
    def register_service(key):
        if key != app.config['ANGINX_API_KEY']:
            return jsonify({'error': 'invalid API key'}), 401

        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'request body must be JSON'}), 400

        domain = data.get('domain', '')
        name   = data.get('name', '')
        port   = data.get('port')
        host   = data.get('host', '')  # optional — upstream IP or hostname; defaults to name

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

        registry = app.config['_registry']
        if domain not in registry and len(registry) >= app.config['ANGINX_MAX_SERVICES']:
            return jsonify({'error': 'max services cap reached'}), 429

        conf_filename = f"{name}.{domain}.conf"
        conf_path = os.path.join(app.config['CONF_D'], conf_filename)
        tmp_path  = conf_path + '.tmp'

        registered_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        upstream = f"http://{host}:{port_int}"
        proxy_block = (
            f"    include {app.config['CONF_BASE']}/_proxy.conf;\n"
            f"    resolver 127.0.0.11 valid=10s;\n"
            f"    location / {{\n"
            f"        set $upstream {upstream};\n"
            f"        proxy_pass $upstream;\n"
            f"    }}\n"
        )
        content = (
            f"# anginx: domain={domain} port={port_int} name={name} host={host}"
            f" registered_at={registered_at}\n"
            f"server {{\n"
            f"    listen 80;\n"
            f"    server_name {domain};\n"
            f"    location /.well-known/acme-challenge/ {{\n"
            f"        root /var/www/acme;\n"
            f"    }}\n"
            f"{proxy_block}"
            f"}}\n"
            f"server {{\n"
            f"    listen 443 ssl;\n"
            f"    server_name {domain};\n"
            f"    ssl_certificate {cert_dir}/fullchain.pem;\n"
            f"    ssl_certificate_key {cert_dir}/privkey.pem;\n"
            f"    ssl_protocols TLSv1.2 TLSv1.3;\n"
            f"    ssl_ciphers HIGH:!aNULL:!MD5;\n"
            f"{proxy_block}"
            f"}}\n"
        )

        # Read original for rollback if overwriting
        original = None
        if os.path.exists(conf_path):
            try:
                with open(conf_path) as f:
                    original = f.read()
            except Exception:
                pass

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
            return jsonify({'error': f"nginx reload failed: {e}"}), 500

        registry[domain] = {
            'domain': domain,
            'port': port_int,
            'name': name,
            'host': host,
            'conf_file': conf_filename,
            'registered_at': registered_at,
        }

        return jsonify(registry[domain]), 200

    @app.route('/services', methods=['GET'])
    def get_services():
        return jsonify(list(app.config['_registry'].values())), 200

    @app.route('/services/<domain>', methods=['DELETE'])
    def deregister_service(domain):
        key = request.args.get('key', '')
        if key != app.config['ANGINX_API_KEY']:
            return jsonify({'error': 'invalid API key'}), 401

        domain = domain.lower()
        registry = app.config['_registry']
        if domain not in registry:
            return jsonify({'error': f"'{domain}' is not registered"}), 404

        service  = registry[domain]
        conf_path = os.path.join(app.config['CONF_D'], service['conf_file'])

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
        key = request.args.get('key', '')
        if key != app.config['ANGINX_API_KEY']:
            return 'invalid API key', 401
        services = list(app.config['_registry'].values())
        return render_template('dashboard.html', services=services)

    return app
