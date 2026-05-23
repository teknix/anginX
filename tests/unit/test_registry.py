import json
import os
import pytest
from unittest.mock import patch


def make_ssl(conf_base, slug='1com'):
    os.makedirs(conf_base, exist_ok=True)
    with open(os.path.join(conf_base, f'_ssl_{slug}.conf'), 'w') as f:
        f.write('ssl_certificate /certs/fullchain.pem;\n')
        f.write('ssl_certificate_key /certs/privkey.pem;\n')
    with open(os.path.join(conf_base, '_proxy.conf'), 'w') as f:
        f.write('# proxy\n')


def make_app(tmp_path):
    import sys
    sys.path.insert(0, str(tmp_path.parent.parent))
    from app import create_app
    conf_d    = str(tmp_path / 'conf.d')
    conf_base = str(tmp_path / 'conf.base')
    os.makedirs(conf_d)
    make_ssl(conf_base)
    app = create_app({
        'ANGINX_API_KEY': 'secret',
        'ANGINX_MAX_SERVICES': 3,
        'ANGINX_SSL_MODE': 'baked',
        'CONF_D': conf_d,
        'CONF_BASE': conf_base,
        'CERTS_DIR': str(tmp_path / 'certs'),
        'NGINX_PID': str(tmp_path / 'nginx.pid'),
        'TESTING': True,
    })
    return app.test_client(), app, conf_d


def post_register(client, domain='app.1.com', name='myapp', port=8080):
    return client.post(
        '/new/secret',
        data=json.dumps({'domain': domain, 'port': port, 'name': name}),
        content_type='application/json',
    )


class TestRegister:
    def test_requires_valid_key(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post('/new/wrongkey',
                            data=json.dumps({'domain': 'app.1.com', 'port': 80, 'name': 'x'}),
                            content_type='application/json')
        assert r.status_code == 401

    def test_rejects_bad_domain(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post('/new/secret',
                            data=json.dumps({'domain': 'nodot', 'port': 80, 'name': 'x'}),
                            content_type='application/json')
        assert r.status_code == 400

    def test_rejects_unknown_root_domain(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register(client, domain='app.unknown.net')
        assert r.status_code == 400
        assert 'unknown root domain' in r.get_json()['error']

    def test_success_updates_registry(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register(client)
        assert r.status_code == 200
        body = r.get_json()
        assert body['domain'] == 'app.1.com'
        assert body['name'] == 'myapp'
        assert body['port'] == 8080

        services = client.get('/services').get_json()
        assert len(services) == 1
        assert services[0]['domain'] == 'app.1.com'

    def test_idempotent_overwrite(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client, port=8080)
            r = post_register(client, port=9090)
        assert r.status_code == 200
        services = client.get('/services').get_json()
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
        services = client.get('/services').get_json()
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
        r = client.get('/services')
        assert r.status_code == 200
        assert r.get_json() == []

    def test_no_auth_required(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        r = client.get('/services')
        assert r.status_code == 200
