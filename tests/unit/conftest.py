import os
import sys
import tempfile
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
    ssl_dir = tmp_path / 'conf.d' / 'ssl'
    ssl_dir.mkdir(parents=True, exist_ok=True)
    (ssl_dir / '_1com.conf').write_text(
        'ssl_certificate /etc/nginx/certs/live/1.com/fullchain.pem;\n'
        'ssl_certificate_key /etc/nginx/certs/live/1.com/privkey.pem;\n'
    )
    flask_app = create_app({
        'ANGINX_API_KEY': 'testkey',
        'ANGINX_MAX_SERVICES': 100,
        'CONF_D': conf_d,
        'CONF_BASE': conf_base,
        'NGINX_PID': str(tmp_path / 'nginx.pid'),
        'TESTING': True,
    })
    with flask_app.test_client() as c:
        yield c, flask_app
