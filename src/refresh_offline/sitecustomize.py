"""Block external networking in refresh build children, including swallowed errors."""
import ipaddress
import os
import socket

if os.environ.get("ADVISOR_REFRESH_OFFLINE") == "1":
    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex
    _getaddrinfo = socket.getaddrinfo

    def _check(host):
        if str(host).lower() == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        marker = os.environ.get("REFRESH_NETWORK_ATTEMPT_FILE")
        if marker:
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write("external_network_denied\n")
        raise OSError("External network access is disabled during refresh builds")

    def connect(self, address):
        _check(address[0])
        return _connect(self, address)

    def connect_ex(self, address):
        _check(address[0])
        return _connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        _check(host)
        return _getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
