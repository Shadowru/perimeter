import textwrap

import pytest

from perimeter_core.config import ConfigError, load_config


def write(tmp_path, body):
    p = tmp_path / "perimeter.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_defaults(tmp_path):
    cfg = load_config(write(tmp_path, """
        allowed_hosts:
          - 1c-server.corp.local
        bridge_1c:
          base_url: http://1c-server.corp.local/bp30
          username: robot
    """))
    assert cfg.allowed_hosts == ["1c-server.corp.local"]
    assert cfg.inference.base_url == "http://127.0.0.1:8090"
    assert cfg.bridge_1c.host == "1c-server.corp.local"
    assert cfg.locale == "ru"


def test_bridge_host_must_be_allowed(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, """
            allowed_hosts: []
            bridge_1c:
              base_url: http://rogue.example.com/bp30
        """))


def test_localhost_bridge_needs_no_allowlist(tmp_path):
    cfg = load_config(write(tmp_path, """
        bridge_1c:
          base_url: http://127.0.0.1/bp30
    """))
    assert cfg.bridge_1c.host == "127.0.0.1"


def test_inference_must_be_loopback(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, """
            inference:
              host: 0.0.0.0
        """))


def test_password_in_config_rejected(tmp_path):
    cfg = load_config(write(tmp_path, """
        bridge_1c:
          base_url: http://127.0.0.1/bp30
          password: hunter2
    """))
    with pytest.raises(ConfigError):
        cfg.bridge_1c.resolve_password()


def test_password_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PERIMETER_1C_PASSWORD", "s3cret")
    cfg = load_config(write(tmp_path, "bridge_1c: {base_url: 'http://127.0.0.1/bp30'}"))
    assert cfg.bridge_1c.resolve_password() == "s3cret"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
