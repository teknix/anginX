"""
Integration tests — require the anginX container to be running.

Run via:
    cd tests/integration
    docker compose -f docker-compose.test.yml up -d --build
    pytest test_integration.py
    docker compose -f docker-compose.test.yml down -v
"""
import json
import time
import urllib.request
import urllib.error
import pytest

BASE   = 'http://localhost:18080'
KEY    = 'integrationkey'
DOMAIN = 'app.test.com'
NAME   = 'upstream'
PORT   = 7705


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return r.status, json.loads(r.read())


def post(path, body, expect_error=False, key=KEY):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {key}'}, method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        if expect_error:
            return e.code, json.loads(e.read())
        raise


def delete(path, expect_error=False):
    req = urllib.request.Request(f"{BASE}{path}", method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        if expect_error:
            return e.code, json.loads(e.read())
        raise


@pytest.fixture(scope='module', autouse=True)
def wait_for_anginx():
    for _ in range(30):
        try:
            status, body = get('/health')
            if status == 200 and body.get('nginx') == 'running':
                return
        except Exception:
            pass
        time.sleep(2)
    pytest.fail("anginX did not become healthy within 60s")


def test_health():
    status, body = get('/health')
    assert status == 200
    assert body['nginx'] == 'running'


def test_services_empty_at_start():
    status, body = get(f'/services?key={KEY}')
    assert status == 200
    assert isinstance(body, list)


def test_register_service():
    status, body = post('/new',
                        {'domain': DOMAIN, 'port': PORT, 'name': NAME})
    assert status == 200
    assert body['domain'] == DOMAIN
    assert body['port'] == PORT


def test_service_appears_in_list():
    status, body = get(f'/services?key={KEY}')
    assert status == 200
    domains = [s['domain'] for s in body]
    assert DOMAIN in domains


def test_health_shows_service_count():
    status, body = get('/health')
    assert body['services'] >= 1


def test_idempotent_reregister():
    status, body = post('/new',
                        {'domain': DOMAIN, 'port': PORT + 1, 'name': NAME})
    assert status == 200
    status, services = get(f'/services?key={KEY}')
    match = [s for s in services if s['domain'] == DOMAIN]
    assert len(match) == 1
    assert match[0]['port'] == PORT + 1


def test_invalid_api_key_rejected():
    code, body = post('/new',
                      {'domain': DOMAIN, 'port': PORT, 'name': NAME},
                      expect_error=True, key='wrongkey')
    assert code == 401


def test_single_label_domain_rejected():
    code, body = post('/new',
                      {'domain': 'nodot', 'port': PORT, 'name': NAME},
                      expect_error=True)
    assert code == 400


def test_deregister_service():
    status, body = delete(f'/services/{DOMAIN}?key={KEY}')
    assert status == 200
    assert body['removed'] is True


def test_service_gone_after_deregister():
    status, body = get(f'/services?key={KEY}')
    assert DOMAIN not in [s['domain'] for s in body]


def test_deregister_unknown_returns_404():
    code, body = delete(f'/services/nothere.com?key={KEY}', expect_error=True)
    assert code == 404
