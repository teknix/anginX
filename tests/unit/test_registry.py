import json
import os
import time
import pytest
from unittest.mock import patch


def make_cert(certs_dir, domain='app.1.com'):
    live_dir = os.path.join(certs_dir, 'live', domain)
    os.makedirs(live_dir, exist_ok=True)
    with open(os.path.join(live_dir, 'fullchain.pem'), 'w') as f:
        f.write('# fake cert\n')
    with open(os.path.join(live_dir, 'privkey.pem'), 'w') as f:
        f.write('# fake key\n')


def make_app(tmp_path):
    import sys
    sys.path.insert(0, str(tmp_path.parent.parent))
    from app import create_app
    conf_d    = str(tmp_path / 'conf.d')
    conf_base = str(tmp_path / 'conf.base')
    certs_dir = str(tmp_path / 'certs')
    os.makedirs(conf_d)
    os.makedirs(conf_base, exist_ok=True)
    with open(os.path.join(conf_base, '_proxy.conf'), 'w') as f:
        f.write('# proxy\n')
    for domain in ['app.1.com', 'a.1.com', 'b.1.com', 'c.1.com', 'd.1.com']:
        make_cert(certs_dir, domain)
    app = create_app({
        'ANGINX_API_KEY': 'secret',
        'ANGINX_MAX_SERVICES': 3,
        'CONF_D': conf_d,
        'CONF_BASE': conf_base,
        'CERTS_DIR': certs_dir,
        'NGINX_PID': str(tmp_path / 'nginx.pid'),
        'TESTING': True,
    })
    return app.test_client(), app, conf_d


AUTH = {'Authorization': 'Bearer secret'}


def post_register(client, domain='app.1.com', name='myapp', port=8080):
    return client.post(
        '/new',
        data=json.dumps({'domain': domain, 'port': port, 'name': name}),
        content_type='application/json',
        headers=AUTH,
    )


class TestRegister:
    def test_requires_valid_key(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post('/new',
                            data=json.dumps({'domain': 'app.1.com', 'port': 80, 'name': 'x'}),
                            content_type='application/json',
                            headers={'Authorization': 'Bearer wrongkey'})
        assert r.status_code == 401

    def test_requires_auth_header(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        r = client.post('/new',
                        data=json.dumps({'domain': 'app.1.com', 'port': 80, 'name': 'x'}),
                        content_type='application/json')  # no header
        assert r.status_code == 401

    def test_rejects_bad_domain(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post('/new',
                            data=json.dumps({'domain': 'nodot', 'port': 80, 'name': 'x'}),
                            content_type='application/json', headers=AUTH)
        assert r.status_code == 400

    def test_rejects_missing_cert(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register(client, domain='app.nocert.net')
        assert r.status_code == 503
        assert 'no certificate' in r.get_json()['error']

    def test_http_fallback_when_allowed(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        app.config['ANGINX_ALLOW_HTTP'] = True
        with patch('subprocess.run'):
            r = post_register(client, domain='app.nocert.net', name='nocert')
        assert r.status_code == 200
        conf = open(os.path.join(conf_d, 'nocert.app.nocert.net.conf')).read()
        assert 'listen 80;' in conf
        assert 'acme-challenge' in conf          # cert can still be acquired
        assert 'ssl_certificate' not in conf

    def test_http_upgrades_to_https_when_cert_appears(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        app.config['ANGINX_ALLOW_HTTP'] = True
        with patch('subprocess.run'):
            post_register(client, domain='app.nocert.net', name='nocert')   # HTTP
            make_cert(str(tmp_path / 'certs'), 'app.nocert.net')            # cert arrives
            r = post_register(client, domain='app.nocert.net', name='nocert')  # re-register
        assert r.status_code == 200
        conf = open(os.path.join(conf_d, 'nocert.app.nocert.net.conf')).read()
        assert 'listen 443 ssl;' in conf
        assert 'listen 80;' not in conf

    def test_success_updates_registry(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register(client)
        assert r.status_code == 200
        body = r.get_json()
        assert body['domain'] == 'app.1.com'
        assert body['name'] == 'myapp'
        assert body['port'] == 8080

        services = client.get('/services?key=secret').get_json()
        assert len(services) == 1
        assert services[0]['domain'] == 'app.1.com'

    def test_sse_writes_directives_and_round_trips(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post(
                '/new',
                data=json.dumps({'domain': 'app.1.com', 'port': 8080,
                                 'name': 'myapp', 'sse': True}),
                content_type='application/json', headers=AUTH,
            )
        assert r.status_code == 200

        conf = open(os.path.join(conf_d, 'myapp.app.1.com.conf')).read()
        assert 'proxy_buffering off;' in conf
        assert 'sse=1' in conf  # header marker

        # registry is rebuilt from the conf header, so this proves the round-trip
        assert client.get('/services?key=secret').get_json()[0]['sse'] is True

    def test_non_sse_has_no_directives(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
        conf = open(os.path.join(conf_d, 'myapp.app.1.com.conf')).read()
        assert 'proxy_buffering' not in conf
        assert 'sse=' not in conf
        assert client.get('/services?key=secret').get_json()[0]['sse'] is False

    def test_idempotent_overwrite(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client, port=8080)
            r = post_register(client, port=9090)
        assert r.status_code == 200
        services = client.get('/services?key=secret').get_json()
        assert len(services) == 1
        assert services[0]['port'] == 9090

    def test_max_services_cap(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client, domain='a.1.com', name='a')
            post_register(client, domain='b.1.com', name='b')
            post_register(client, domain='c.1.com', name='c')
            r = post_register(client, domain='d.1.com', name='d')
        assert r.status_code == 429

    def test_max_services_cap_allows_overwrite(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client, domain='a.1.com', name='a')
            post_register(client, domain='b.1.com', name='b')
            post_register(client, domain='c.1.com', name='c')
            # overwrite existing — should not 429
            r = post_register(client, domain='a.1.com', name='a', port=9999)
        assert r.status_code == 200

    def test_writes_conf_file(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
        assert os.path.exists(os.path.join(conf_d, 'myapp.app.1.com.conf'))

    def test_conf_contains_anginx_header(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
        with open(os.path.join(conf_d, 'myapp.app.1.com.conf')) as f:
            content = f.read()
        assert '# anginx:' in content
        assert 'domain=app.1.com' in content


class TestDeregister:
    def test_requires_valid_key(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
            r = client.delete('/services/app.1.com?key=wrongkey')
        assert r.status_code == 401

    def test_returns_404_for_unknown(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        r = client.delete('/services/notregistered.1.com?key=secret')
        assert r.status_code == 404

    def test_removes_from_registry(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
            r = client.delete('/services/app.1.com?key=secret')
        assert r.status_code == 200
        services = client.get('/services?key=secret').get_json()
        assert len(services) == 0

    def test_removes_conf_file(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
            client.delete('/services/app.1.com?key=secret')
        assert not os.path.exists(os.path.join(conf_d, 'myapp.app.1.com.conf'))


class TestServices:
    def test_empty_returns_list(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        r = client.get('/services?key=secret')
        assert r.status_code == 200
        assert r.get_json() == []

    def test_requires_valid_key(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        assert client.get('/services').status_code == 401
        assert client.get('/services?key=wrong').status_code == 401


class TestReaper:
    def test_reaps_stale_service(self, tmp_path):
        from app import reap_stale
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
            app.config['_last_seen']['app.1.com'] = time.monotonic() - 1000
            removed = reap_stale(app)
        assert removed == ['app.1.com']
        assert 'app.1.com' not in app.config['_registry']
        assert not os.path.exists(os.path.join(conf_d, 'myapp.app.1.com.conf'))

    def test_keeps_fresh_service(self, tmp_path):
        from app import reap_stale
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
            removed = reap_stale(app)
        assert removed == []
        assert 'app.1.com' in app.config['_registry']

    def test_ttl_zero_disables(self, tmp_path):
        from app import reap_stale
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client)
            app.config['ANGINX_TTL'] = 0
            app.config['_last_seen']['app.1.com'] = time.monotonic() - 1000
            assert reap_stale(app) == []
        assert 'app.1.com' in app.config['_registry']

    def test_heartbeat_skips_reload(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run') as run:
            post_register(client)           # write: nginx -t + reload = 2 calls
            calls_after_first = run.call_count
            post_register(client)           # identical re-register = heartbeat
        assert run.call_count == calls_after_first  # no extra nginx invocations
        assert app.config['_registry']['app.1.com']['registered_at']
