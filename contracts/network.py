"""Offline upstream boundary that leaves the test database reachable."""

from contextlib import contextmanager
import ipaddress
import socket
from unittest.mock import patch


def local_address(sock, address):
    if sock.family == socket.AF_UNIX:
        return True
    return isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback


@contextmanager
def offline_requests(send):
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect

    def resolve(host, port, *args, **kwargs):
        if host in {"localhost", "127.0.0.1", "::1"}:
            return original_resolve(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    def connect(sock, address):
        if local_address(sock, address):
            return original_connect(sock, address)
        raise AssertionError(f"External network forbidden: {address}")

    with patch("requests.adapters.HTTPAdapter.send", side_effect=send), \
            patch("socket.getaddrinfo", side_effect=resolve), \
            patch("socket.socket.connect", connect):
        yield
