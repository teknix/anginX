import os
import pytest
from app import rebuild_registry


def write_conf(conf_d, filename, content):
    os.makedirs(conf_d, exist_ok=True)
    path = os.path.join(conf_d, filename)
    with open(path, 'w') as f:
        f.write(content)
    return path


def test_parses_valid_header(tmp_path):
    conf_d = str(tmp_path / 'conf.d')
    write_conf(conf_d, 'myapp.app.1.com.conf',
               '# anginx: domain=app.1.com port=8080 name=myapp registered_at=2026-05-22T00:00:00Z\n'
               'server { ... }\n')
    reg = rebuild_registry({'CONF_D': conf_d})
    assert 'app.1.com' in reg
    assert reg['app.1.com']['port'] == 8080
    assert reg['app.1.com']['name'] == 'myapp'
    assert reg['app.1.com']['registered_at'] == '2026-05-22T00:00:00Z'


def test_does_not_recurse_into_ssl_subdir(tmp_path):
    # dynamic SSL fragments live in conf.d/ssl/ — parser only reads conf.d/ direct children
    conf_d = str(tmp_path / 'conf.d')
    ssl_dir = str(tmp_path / 'conf.d' / 'ssl')
    write_conf(ssl_dir, '_1com.conf',
               'ssl_certificate /etc/nginx/certs/1.com/fullchain.pem;\n')
    reg = rebuild_registry({'CONF_D': conf_d})
    assert len(reg) == 0


def test_skips_file_with_no_header(tmp_path):
    conf_d = str(tmp_path / 'conf.d')
    write_conf(conf_d, 'noheader.app.1.com.conf',
               'server { server_name app.1.com; }\n')
    reg = rebuild_registry({'CONF_D': conf_d})
    assert len(reg) == 0


def test_skips_non_conf_files(tmp_path):
    conf_d = str(tmp_path / 'conf.d')
    os.makedirs(conf_d)
    with open(os.path.join(conf_d, 'somefile.txt'), 'w') as f:
        f.write('# anginx: domain=app.1.com port=80 name=x registered_at=2026-01-01T00:00:00Z\n')
    reg = rebuild_registry({'CONF_D': conf_d})
    assert len(reg) == 0


def test_handles_missing_conf_d(tmp_path):
    reg = rebuild_registry({'CONF_D': str(tmp_path / 'nonexistent')})
    assert reg == {}


def test_multiple_services(tmp_path):
    conf_d = str(tmp_path / 'conf.d')
    write_conf(conf_d, 'a.app1.com.conf',
               '# anginx: domain=app1.com port=7000 name=svc1 registered_at=2026-01-01T00:00:00Z\n')
    write_conf(conf_d, 'b.app2.com.conf',
               '# anginx: domain=app2.com port=7001 name=svc2 registered_at=2026-01-02T00:00:00Z\n')
    reg = rebuild_registry({'CONF_D': conf_d})
    assert len(reg) == 2
    assert 'app1.com' in reg
    assert 'app2.com' in reg
