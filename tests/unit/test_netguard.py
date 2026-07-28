import socket
import threading

import pytest

from perimeter_core import netguard


@pytest.fixture
def guard():
    violations = []
    netguard.install(["1c-server.corp.local"], on_violation=violations.append)
    yield violations
    netguard.uninstall()


def test_loopback_allowed(guard):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted = []
    thr = threading.Thread(target=lambda: accepted.append(srv.accept()))
    thr.start()
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass
    thr.join(timeout=5)
    srv.close()
    assert accepted


def test_external_ip_blocked(guard):
    s = socket.socket()
    with pytest.raises(netguard.NetworkViolation):
        s.connect(("93.184.216.34", 80))
    s.close()
    assert guard == ["93.184.216.34"]


def test_external_hostname_blocked(guard):
    s = socket.socket()
    with pytest.raises(netguard.NetworkViolation):
        s.connect(("evil.example.com", 443))
    s.close()


def test_allowed_host_passes_check(guard):
    # Хост из allowlist не должен вызывать NetworkViolation; DNS в тестовой
    # среде его не знает, поэтому ждём обычную сетевую ошибку, но не Violation.
    s = socket.socket()
    s.settimeout(0.5)
    with pytest.raises(OSError) as exc_info:
        s.connect(("1c-server.corp.local", 80))
    s.close()
    assert not isinstance(exc_info.value, netguard.NetworkViolation)


def test_uninstall_restores():
    netguard.install([])
    netguard.uninstall()
    assert not netguard.is_installed()
    assert socket.socket.connect.__qualname__ == "socket.connect"
