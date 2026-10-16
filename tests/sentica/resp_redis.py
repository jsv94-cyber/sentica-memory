"""A tiny RESP2 server for the gateway budget tests.

It implements the commands redis-py sends on connect and the commands the
budget counter uses. It is not a Redis replacement.
"""

from __future__ import annotations

import fnmatch
import socketserver
import threading
from typing import cast


def _line(buf: bytes) -> tuple[bytes | None, bytes]:
    end = buf.find(b"\r\n")
    if end < 0:
        return None, buf
    return buf[:end], buf[end + 2 :]


def _parse_one(buf: bytes) -> tuple[list[str] | None, bytes]:
    if not buf.startswith(b"*"):
        return None, buf
    header, rest = _line(buf)
    if header is None:
        return None, buf
    count = int(header[1:])
    args: list[str] = []
    for _ in range(count):
        if not rest.startswith(b"$"):
            return None, buf
        size_line, after = _line(rest)
        if size_line is None:
            return None, buf
        size = int(size_line[1:])
        if len(after) < size + 2:
            return None, buf
        args.append(after[:size].decode())
        rest = after[size + 2 :]
    return args, rest


def _bulk(value: str) -> bytes:
    raw = value.encode()
    return b"$" + str(len(raw)).encode() + b"\r\n" + raw + b"\r\n"


def _reply(store: dict[str, str], lock: threading.Lock, args: list[str]) -> bytes:
    command = args[0].upper()
    if command == "HELLO":
        return b"%1\r\n$5\r\nproto\r\n:2\r\n"
    if command in {"AUTH", "CLIENT", "SELECT", "PING"}:
        if command == "PING":
            return b"+PONG\r\n"
        return b"+OK\r\n"
    if command == "COMMAND":
        return b"*0\r\n"
    if command == "INCRBY":
        key = args[1]
        delta = int(args[2])
        with lock:
            value = int(store.get(key, "0")) + delta
            store[key] = str(value)
        return b":" + str(value).encode() + b"\r\n"
    if command == "GET":
        with lock:
            value = store.get(args[1])
        if value is None:
            return b"$-1\r\n"
        return _bulk(value)
    if command == "EXPIRE":
        return b":1\r\n"
    if command == "DEL":
        removed = 0
        with lock:
            for key in args[1:]:
                if store.pop(key, None) is not None:
                    removed += 1
        return b":" + str(removed).encode() + b"\r\n"
    if command == "SCAN":
        pattern = "*"
        upper = [item.upper() for item in args]
        if "MATCH" in upper:
            pattern = args[upper.index("MATCH") + 1]
        with lock:
            keys = [key for key in store if fnmatch.fnmatch(key, pattern)]
        body = b"*2\r\n$1\r\n0\r\n*" + str(len(keys)).encode() + b"\r\n"
        for key in keys:
            body += _bulk(key)
        return body
    if command == "KEYS":
        pattern = args[1] if len(args) > 1 else "*"
        with lock:
            keys = [key for key in store if fnmatch.fnmatch(key, pattern)]
        body = b"*" + str(len(keys)).encode() + b"\r\n"
        for key in keys:
            body += _bulk(key)
        return body
    return b"-ERR unknown command\r\n"


class _Server(socketserver.ThreadingTCPServer):
    store: dict[str, str]
    lock: threading.Lock

    def __init__(self, store: dict[str, str], lock: threading.Lock) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.store = store
        self.lock = lock


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(_Server, self.server)
        store = server.store
        lock = server.lock
        buf = b""
        while True:
            chunk = self.request.recv(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                args, buf = _parse_one(buf)
                if args is None:
                    break
                self.request.sendall(_reply(store, lock, args))


class RespRedis:
    """Local Redis stand-in bound to 127.0.0.1 on an ephemeral port."""

    store: dict[str, str]
    lock: threading.Lock
    _server: _Server
    _thread: threading.Thread

    def __init__(self) -> None:
        self.store = {}
        self.lock = threading.Lock()
        self._server = _Server(self.store, self.lock)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        address = self._server.server_address
        host = address[0]
        port = address[1]
        return "redis://" + str(host) + ":" + str(port) + "/0"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = ["RespRedis"]
