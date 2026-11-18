"""Fixed selfcheck observation hook, executed before the approved Python entry."""

import hashlib
import http.client
import json
import os
import signal
import socketserver
import threading
from typing import Any

_release = threading.Event()
signal.signal(signal.SIGUSR2, lambda *args: _release.set())


def _report(value: dict[str, Any]) -> None:
    value["pid"] = os.getpid()
    with open(os.environ["HARNESS_R04_AUDIT"], "a") as stream:
        stream.write(json.dumps(value) + "\n")


_putrequest = http.client.HTTPConnection.putrequest


def _request(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
    self._harness_request_path = url
    return _putrequest(self, method, url, *args, **kwargs)


_connection: Any = http.client.HTTPConnection
_connection.putrequest = _request


_getresponse = http.client.HTTPConnection.getresponse


def _response(self: Any, *args: Any, **kwargs: Any) -> Any:
    response = _getresponse(self, *args, **kwargs)
    read = response.read
    digest = hashlib.sha256()
    sent = False

    def observed_read(*values: Any, **options: Any) -> bytes:
        nonlocal sent
        raw = read(*values, **options)
        digest.update(raw)
        if not sent and (not values or values[0] is None or not raw or response.isclosed()):
            sent = True
            _report(
                {
                    "kind": "http",
                    "host": self.host,
                    "port": self.port,
                    "path": getattr(self, "_harness_request_path", "/"),
                    "status": response.status,
                    "type": response.getheader("Content-Type", ""),
                    "body_hash": digest.hexdigest(),
                }
            )
        return bytes(raw)

    observed: Any = response
    observed.read = observed_read
    return response


_connection.getresponse = _response
_close = socketserver.TCPServer.server_close
_shutdown = socketserver.BaseServer.shutdown


def _hold_for_probe(self: Any) -> None:
    if self.socket.fileno() >= 0 and not getattr(self, "_harness_closing_reported", False):
        self._harness_closing_reported = True
        address = self.server_address
        _report({"kind": "closing", "host": address[0], "port": address[1]})
        _release.wait()


def _server_shutdown(self: Any) -> Any:
    # Keep the serving thread alive until the independent request completes.
    _hold_for_probe(self)
    return _shutdown(self)


def _server_close(self: Any) -> Any:
    _hold_for_probe(self)
    return _close(self)


_server: Any = socketserver.TCPServer
_server.server_close = _server_close
_base_server: Any = socketserver.BaseServer
_base_server.shutdown = _server_shutdown
