"""Рантайм-исполнение правила №0: воздушный зазор.

install() оборачивает socket.socket.connect / connect_ex: соединения
разрешены только на loopback и хосты из config/perimeter.yaml
(allowed_hosts — внутренние хосты 1С). Всё остальное — NetworkViolation
c записью в аудит-лог. Статический скан (tools/ci/airgap_scan.py) ловит
нарушения до релиза, netguard — последний рубеж в рантайме.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable

from .i18n import t

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_installed = False


class NetworkViolation(ConnectionError):
    pass


def _is_loopback_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _make_checker(allowed_hosts: set[str], on_violation: Callable[[str], None] | None):
    allowed = {h.lower() for h in allowed_hosts}

    def check(address: object) -> None:
        # AF_UNIX (строка/байты) — всегда локально, разрешено.
        if not isinstance(address, tuple) or not address:
            return
        host = str(address[0]).lower().strip("[]")
        if host in ("localhost",) or _is_loopback_ip(host) or host in allowed:
            return
        # IP, в который резолвится разрешённый хост (внутренний DNS).
        try:
            resolved = {ai[4][0] for h in allowed for ai in socket.getaddrinfo(h, None)}
        except OSError:
            resolved = set()
        if host in resolved:
            return
        if on_violation is not None:
            on_violation(host)
        raise NetworkViolation(t("error.host_not_allowed", host=host))

    return check


def install(allowed_hosts: list[str] | set[str], on_violation: Callable[[str], None] | None = None) -> None:
    global _installed
    check = _make_checker(set(allowed_hosts), on_violation)

    def guarded_connect(self: socket.socket, address):  # type: ignore[no-untyped-def]
        check(address)
        return _orig_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address):  # type: ignore[no-untyped-def]
        check(address)
        return _orig_connect_ex(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    _installed = True


def uninstall() -> None:
    global _installed
    socket.socket.connect = _orig_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _orig_connect_ex  # type: ignore[method-assign]
    _installed = False


def is_installed() -> bool:
    return _installed
