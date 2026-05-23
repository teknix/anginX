import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app import create_app


@pytest.fixture
def conf_d(tmp_path):
    return str(tmp_path / 'conf.d')


@pytest.fixture
def conf_base(tmp_path):
    d = tmp_path / 'conf.base'
    d.mkdir()
    (d / '_proxy.conf').write_text('# proxy\n')
    return str(d)


@pytest.fixture
def app_client(conf_d, conf_base, tmp_path):
    # Cert for the test domain app.1.com
    cert_dir = tmp_path / 'certs' / 'live' / 'app.1.com'
    cert_dir.mkdir(parents=True)
    (cert_dir / 'fullchain.pem').write_text('# fake cert\n')
    (cert_dir / 'privkey.pem').write_text('# fake key\n')

    os.makedirs(conf_d, exist_ok=True)
    flask_app = create_app({
        'ANGINX_API_KEY': 'testkey',
        'ANGINX_MAX_SERVICES': 100,
        'CONF_D': conf_d,
        'CONF_BASE': conf_base,
        'CERTS_DIR': str(tmp_path / 'certs'),
        'NGINX_PID': str(tmp_path / 'nginx.pid'),
        'TESTING': True,
    })
    with flask_app.test_client() as c:
        yield c, flask_app
