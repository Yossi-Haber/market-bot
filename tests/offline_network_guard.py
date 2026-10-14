"""Pytest plugin that blocks non-loopback network connections in CI validation."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any


class ExternalNetworkBlocked(RuntimeError):
    """Raised if the offline application attempts an external socket connect."""


_original_connect = None
_original_connect_ex = None
_original_getaddrinfo = None


def _is_loopback(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    if isinstance(host, str) and host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def activate_offline_network_guard() -> None:
    """Reject outbound connects while allowing local test servers."""
    global _original_connect, _original_connect_ex
    global _original_getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    _original_connect = original_connect
    _original_connect_ex = original_connect_ex
    _original_getaddrinfo = original_getaddrinfo

    def guarded_connect(sock, address):
        if not _is_loopback(address):
            raise ExternalNetworkBlocked("OFFLINE_VALIDATION_EXTERNAL_NETWORK_BLOCKED")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_loopback(address):
            raise ExternalNetworkBlocked("OFFLINE_VALIDATION_EXTERNAL_NETWORK_BLOCKED")
        return original_connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise ExternalNetworkBlocked("OFFLINE_VALIDATION_EXTERNAL_DNS_BLOCKED")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_getaddrinfo


def pytest_sessionstart(session) -> None:
    """Install the network guard for the entire pytest process."""
    activate_offline_network_guard()


def pytest_sessionfinish(session, exitstatus) -> None:
    """Restore socket methods before pytest shuts down its plugins."""
    # The pytest process exits immediately after this hook; restoration is useful
    # for plugin integrations that invoke pytest in-process.
    if _original_connect is not None:
        socket.socket.connect = _original_connect
    if _original_connect_ex is not None:
        socket.socket.connect_ex = _original_connect_ex
    if _original_getaddrinfo is not None:
        socket.getaddrinfo = _original_getaddrinfo
