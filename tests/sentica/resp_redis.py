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


def _eval(store: dict[str, str], lock: threading.Lock, args: list[str]) -> bytes:
    script = args[1]
    numkeys = int(args[2])
    keys = args[3 : 3 + numkeys]
    argv = args[3 + numkeys :]
    with lock:
        if "sentica-budget-reserve" in script:
            total = int(store.get(keys[0], "0"))
            spent = int(store.get(keys[1], "0"))
            total_limit = int(argv[0])
            purpose_limit = int(argv[1])
            hold = int(argv[2])
            if total_limit >= 0 and total + hold > total_limit:
                return b":0\r\n"
            if purpose_limit >= 0 and spent + hold > purpose_limit:
                return b":0\r\n"
            if hold > 0:
                store[keys[0]] = str(total + hold)
                store[keys[1]] = str(spent + hold)
            return b":1\r\n"
        if "sentica-budget-settle" in script:
            actual = int(argv[0])
            hold = int(argv[1])
            delta = actual - hold
            total = int(store.get(keys[0], "0")) + delta
            spent = int(store.get(keys[1], "0")) + delta
            if total < 0:
                total = 0
            if spent < 0:
                spent = 0
            store[keys[0]] = str(total)
            store[keys[1]] = str(spent)
            return _bulk(str(total))
    return b"-ERR unknown script\r\n"


def _reply(
    store: dict[str, str],
    lock: threading.Lock,
    args: list[str],
    commands: list[str],
) -> bytes:
    command = args[0].upper()
    commands.append(command)
    if command == "EVAL":
        return _eval(store, lock, args)
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
    # The gateway keeps a pooled connection open. Waiting for that handler
    # to finish would deadlock teardown.
    daemon_threads: bool = True
    block_on_close: bool = False
    store: dict[str, str]
    lock: threading.Lock
    commands: list[str]

    def __init__(
        self, store: dict[str, str], lock: threading.Lock, commands: list[str]
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.store = store
        self.lock = lock
        self.commands = commands


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(_Server, self.server)
        store = server.store
        lock = server.lock
        commands = server.commands
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
                self.request.sendall(_reply(store, lock, args, commands))


class RespRedis:
    """Local Redis stand-in bound to 127.0.0.1 on an ephemeral port."""

    store: dict[str, str]
    lock: threading.Lock
    commands: list[str]
    _server: _Server
    _thread: threading.Thread

    def __init__(self) -> None:
        self.store = {}
        self.lock = threading.Lock()
        self.commands = []
        self._server = _Server(self.store, self.lock, self.commands)
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
