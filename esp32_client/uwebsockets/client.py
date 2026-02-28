"""Minimal websocket client for MicroPython (ESP32).

Supports:
- ws:// (no TLS)
- text and binary send
- close

This module is intentionally tiny to match constrained devices.
"""

import usocket as socket
import ubinascii
import urandom


_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebsocketClient:
    def __init__(self, sock):
        self._sock = sock

    def send(self, data):
        if isinstance(data, str):
            payload = data.encode("utf-8")
            opcode = 0x1
        else:
            payload = data
            opcode = 0x2

        self._write_frame(opcode, payload)

    def close(self):
        try:
            self._write_frame(0x8, b"")
        except Exception:
            pass
        self._sock.close()

    def _write_frame(self, opcode, payload):
        if payload is None:
            payload = b""
        length = len(payload)

        mask_key = bytes(
            [
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
            ]
        )

        hdr = bytearray()
        hdr.append(0x80 | (opcode & 0x0F))  # FIN + opcode

        if length < 126:
            hdr.append(0x80 | length)
        elif length < 65536:
            hdr.append(0x80 | 126)
            hdr.append((length >> 8) & 0xFF)
            hdr.append(length & 0xFF)
        else:
            hdr.append(0x80 | 127)
            for shift in (56, 48, 40, 32, 24, 16, 8, 0):
                hdr.append((length >> shift) & 0xFF)

        self._sock.write(hdr)
        self._sock.write(mask_key)

        # Mask in chunks to reduce RAM pressure.
        chunk = bytearray(256)
        i = 0
        while i < length:
            n = min(256, length - i)
            for j in range(n):
                chunk[j] = payload[i + j] ^ mask_key[(i + j) & 3]
            self._sock.write(memoryview(chunk)[:n])
            i += n


def _parse_url(url):
    if not url.startswith("ws://"):
        raise ValueError("only ws:// is supported by this client")

    rest = url[5:]
    slash = rest.find("/")
    if slash == -1:
        host_port = rest
        path = "/"
    else:
        host_port = rest[:slash]
        path = rest[slash:]

    if ":" in host_port:
        host, port_str = host_port.split(":", 1)
        port = int(port_str)
    else:
        host = host_port
        port = 80

    return host, port, path


def _read_http_headers(sock):
    line = sock.readline()
    if not line or b"101" not in line:
        raise OSError("websocket handshake failed")

    headers = {}
    while True:
        line = sock.readline()
        if not line or line == b"\r\n":
            break
        k, v = line.split(b":", 1)
        headers[k.strip().lower()] = v.strip().lower()
    return headers


def connect(url):
    host, port, path = _parse_url(url)
    addr = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
    sock = socket.socket()
    sock.settimeout(8)
    sock.connect(addr)

    nonce = ubinascii.b2a_base64(
        bytes(
            [
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
                urandom.getrandbits(8),
            ]
        )
    ).strip()

    req = (
        b"GET "
        + path.encode("utf-8")
        + b" HTTP/1.1\r\n"
        + b"Host: "
        + host.encode("utf-8")
        + b"\r\n"
        + b"Upgrade: websocket\r\n"
        + b"Connection: Upgrade\r\n"
        + b"Sec-WebSocket-Key: "
        + nonce
        + b"\r\n"
        + b"Sec-WebSocket-Version: 13\r\n\r\n"
    )

    try:
        sock.write(req)
        headers = _read_http_headers(sock)

        if headers.get(b"upgrade") != b"websocket":
            raise OSError("invalid websocket upgrade response")

        sock.settimeout(None)
        return WebsocketClient(sock)
    except Exception:
        sock.close()
        raise
