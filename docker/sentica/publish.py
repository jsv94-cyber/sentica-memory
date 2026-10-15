"""Forward the local proof ports onto an internal network.

Docker does not publish a port from an internal network. This process is the
only one attached to a network that can publish, and it does not hold
certificates or the Redis password.
"""

from __future__ import annotations

import selectors
import socket
import sys
import threading


def _pipe(left: socket.socket, right: socket.socket) -> None:
    poll = selectors.DefaultSelector()
    poll.register(left, selectors.EVENT_READ)
    poll.register(right, selectors.EVENT_READ)
    try:
        open_sockets = 2
        while open_sockets:
            for key, _event in poll.select():
                source = key.fileobj
                if not isinstance(source, socket.socket):
                    continue
                target = right if source is left else left
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    poll.unregister(source)
                    open_sockets -= 1
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        return
                    continue
                try:
                    target.sendall(data)
                except OSError:
                    return
    finally:
        poll.close()
        left.close()
        right.close()


def _accept(listen: socket.socket, host: str, port: int) -> None:
    while True:
        client, _address = listen.accept()
        try:
            remote = socket.create_connection((host, port), timeout=10)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, remote), daemon=True).start()


def main(argv: list[str]) -> None:
    threads: list[threading.Thread] = []
    for spec in argv:
        listen_text, target = spec.split("=", 1)
        host, port_text = target.rsplit(":", 1)
        listen = socket.create_server(("0.0.0.0", int(listen_text)))
        thread = threading.Thread(
            target=_accept,
            args=(listen, host, int(port_text)),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    if not threads:
        raise SystemExit("publish.py needs listen=host:port arguments")
    threads[0].join()


if __name__ == "__main__":
    main(sys.argv[1:])
