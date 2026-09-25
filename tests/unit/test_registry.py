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

    def test_ws_writes_directives_and_round_trips(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post(
                '/new',
                data=json.dumps({'domain': 'app.1.com', 'port': 8080,
                                 'name': 'myapp', 'ws': True}),
                content_type='application/json', headers=AUTH,
            )
        assert r.status_code == 200

        conf = open(os.path.join(conf_d, 'myapp.app.1.com.conf')).read()
        assert 'proxy_set_header Upgrade $http_upgrade;' in conf
        assert 'proxy_set_header Connection $connection_upgrade;' in conf
        assert 'ws=1' in conf

        assert client.get('/services?key=secret').get_json()[0]['ws'] is True

    def test_ws_and_sse_together_rejected(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = client.post(
                '/new',
                data=json.dumps({'domain': 'app.1.com', 'port': 8080,
                                 'name': 'myapp', 'ws': True, 'sse': True}),
                content_type='application/json', headers=AUTH,
            )
        assert r.status_code == 400
        assert 'mutually exclusive' in r.get_json()['error']

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

    def test_lan_only_domain_file_enforced_without_client_flag(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with open(tmp_path / 'certs' / 'lan-only-domains', 'w') as f:
            f.write('app.1.com\n')
        with patch('subprocess.run'):
            r = post_register(client)  # heartbeat sends no lan_only flag
        assert r.status_code == 200
        conf = open(os.path.join(conf_d, 'myapp.app.1.com.conf')).read()
        assert '_lan_only.conf;' in conf.split('location / {')[1]

    def test_lan_only_client_flag_and_default_off(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register(client, domain='a.1.com', name='a')
            client.post('/new', data=json.dumps({'domain': 'b.1.com', 'port': 8080, 'name': 'b',
                                                 'lan_only': True}),
                        content_type='application/json', headers=AUTH)
        assert '_lan_only.conf;' not in open(os.path.join(conf_d, 'a.a.1.com.conf')).read()
        assert '_lan_only.conf;' in open(os.path.join(conf_d, 'b.b.1.com.conf')).read()


def post_register_paths(client, domain='app.1.com', name='voicecom', paths=None, lan_only=None):
    if paths is None:
        paths = [{'path': '/', 'host': 'flask', 'port': 5010},
                 {'path': '/rtc', 'host': 'livekit', 'port': 7880, 'ws': True}]
    body = {'domain': domain, 'name': name, 'paths': paths}
    if lan_only is not None:
        body['lan_only'] = lan_only
    return client.post('/new', data=json.dumps(body), content_type='application/json', headers=AUTH)


class TestPathRouting:
    def test_registers_multiple_locations(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client)
        assert r.status_code == 200
        conf = open(os.path.join(conf_d, 'voicecom.app.1.com.conf')).read()
        assert 'location / {' in conf
        assert 'location /rtc {' in conf
        assert 'http://flask:5010' in conf
        assert 'http://livekit:7880' in conf
        assert 'proxy_set_header Upgrade $http_upgrade;' in conf  # /rtc's ws flag

    def test_round_trips_through_registry(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register_paths(client)
        services = client.get('/services?key=secret').get_json()
        assert len(services) == 1
        assert services[0]['name'] == 'voicecom'
        paths = {p['path']: p for p in services[0]['paths']}
        assert paths['/']['port'] == 5010
        assert paths['/rtc']['ws'] is True

    def test_defaults_missing_path_host_to_name(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client, name='voicecom',
                                     paths=[{'path': '/', 'port': 5010}])
        assert r.status_code == 200
        conf = open(os.path.join(conf_d, 'voicecom.app.1.com.conf')).read()
        assert 'http://voicecom:5010' in conf

    def test_rejects_duplicate_paths(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client, paths=[{'path': '/', 'host': 'a', 'port': 1},
                                                    {'path': '/', 'host': 'b', 'port': 2}])
        assert r.status_code == 400
        assert 'duplicate' in r.get_json()['error']

    def test_rejects_empty_paths_array(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client, paths=[])
        assert r.status_code == 400

    def test_rejects_too_many_paths(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        too_many = [{'path': f'/p{i}', 'host': 'a', 'port': 1000 + i} for i in range(9)]
        with patch('subprocess.run'):
            r = post_register_paths(client, paths=too_many)
        assert r.status_code == 400
        assert 'too many' in r.get_json()['error'].lower()

    def test_rejects_ws_and_sse_on_same_path(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client, paths=[{'path': '/', 'host': 'a', 'port': 1,
                                                      'ws': True, 'sse': True}])
        assert r.status_code == 400
        assert 'mutually exclusive' in r.get_json()['error']

    def test_rejects_bad_path(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client, paths=[{'path': 'no-leading-slash', 'host': 'a', 'port': 1}])
        assert r.status_code == 400

    def test_lan_only_applies_to_whole_domain(self, tmp_path):
        client, app, conf_d = make_app(tmp_path)
        with patch('subprocess.run'):
            r = post_register_paths(client, lan_only=True)
        assert r.status_code == 200
        conf = open(os.path.join(conf_d, 'voicecom.app.1.com.conf')).read()
        assert conf.count('_lan_only.conf;') == 2  # once per location

    def test_heartbeat_skips_reload(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run') as run:
            post_register_paths(client)
            calls_after_first = run.call_count
            post_register_paths(client)  # identical re-register = heartbeat
        assert run.call_count == calls_after_first

    def test_max_services_cap_counts_domains_not_paths(self, tmp_path):
        client, app, _ = make_app(tmp_path)
        with patch('subprocess.run'):
            post_register_paths(client, domain='a.1.com', name='a')
            post_register_paths(client, domain='b.1.com', name='b')
            post_register_paths(client, domain='c.1.com', name='c')
            r = post_register_paths(client, domain='d.1.com', name='d')
        assert r.status_code == 429
