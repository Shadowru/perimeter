import textwrap

import pytest

from perimeter_core.config import ConfigError, load_config


def write(tmp_path, body):
    p = tmp_path / "perimeter.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_robot_disabled_by_default(tmp_path):
    cfg = load_config(write(tmp_path, "locale: ru"))
    assert cfg.robot.enabled is False


def test_robot_on_loopback_without_token(tmp_path):
    cfg = load_config(write(tmp_path, """
        robot:
          enabled: true
          host: 127.0.0.1
    """))
    assert cfg.robot.enabled and cfg.robot.port == 8092


def test_robot_on_lan_requires_token(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, """
            robot:
              enabled: true
              host: 10.0.0.5
        """))
    assert "token" in str(e.value)


def test_robot_on_lan_with_token_ok(tmp_path):
    cfg = load_config(write(tmp_path, """
        robot:
          enabled: true
          host: 192.168.1.10
          token: secret
    """))
    assert cfg.robot.host == "192.168.1.10"


def test_public_address_forbidden(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, """
            robot:
              enabled: true
              host: 93.184.216.34
              token: secret
        """))
    assert "внутренней сети" in str(e.value)


def test_robot_mode_does_not_require_1c_host_allowlist(tmp_path):
    # При обратном подключении base_url не используется, значит и требование
    # «хост 1С в allowed_hosts» неприменимо.
    cfg = load_config(write(tmp_path, """
        robot:
          enabled: true
          host: 127.0.0.1
        bridge_1c:
          base_url: http://any-1c.corp.local/bp30
    """))
    assert cfg.robot.enabled


def test_gateway_is_shared_between_agents(tmp_path):
    """UI создаёт агента на сессию — шлюз должен подниматься один раз.

    Живое подключение 31.07: второй агент падал с «Address already in use»,
    то есть продукт в рабочем режиме умирал на первом запросе пользователя.
    """
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from perimeter_core import app

    cfg = tmp_path / "perimeter.yaml"
    template = _P("config/perimeter.example.yaml").read_text(encoding="utf-8")
    template = template.replace("enabled: false", "enabled: true", 1)
    template = template.replace('  token: ""', '  token: "t"', 1)
    cfg.write_text(template, encoding="utf-8")

    app._GATEWAYS.clear()
    try:
        first, _ = app.build_agent(cfg, lambda n, a: False)
        second, _ = app.build_agent(cfg, lambda n, a: False)
        assert len(app._GATEWAYS) == 1
        # Оба агента работают через один и тот же шлюз
        assert first.tool_specs and second.tool_specs
    finally:
        for gw in app._GATEWAYS.values():
            gw.stop()
        app._GATEWAYS.clear()
