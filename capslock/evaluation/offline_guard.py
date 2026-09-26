"""Pytest plugin blocking external network traffic in offline regressions."""

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    original = socket.socket.connect

    def connect(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == "localhost"
            if not local:
                raise RuntimeError("external network disabled in offline regressions")
        return original(connection, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
