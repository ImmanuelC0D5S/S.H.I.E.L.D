"""Loopback-only HTTP shim, for driving the pipeline from curl on a laptop.

``shield-rpi-prompt.md`` asks for a ``--fake-trigger`` mode and a
"curl -X POST /trigger" done-when test. This is that.

**It is bound to 127.0.0.1 and refuses any other interface.** The real sensor
link is the framed TCP protocol in PROTOCOL.md 2. It is not HTTP: no framing, no
HMAC per frame, and no way to answer a ``/trigger`` with a status the node could
not have forged. Exposing an unauthenticated HTTP trigger on the AP would let
anyone on ``shield-net`` synthesise intrusion events at will. So this shim exists
only to make the pipeline testable without the ESP32 present, and it stays on
the loopback interface where only the operator's own shell can reach it.

Endpoints, all unauthenticated *because* they are unreachable off-host:

======  =======================================  ==========================
method  path                                     purpose
======  =======================================  ==========================
POST    /trigger                                 run one pipeline pass
GET     /health                                  liveness and link state
POST    /fake-trigger                            same as /trigger, explicit
GET     /state                                   arm state and queue depth
======  =======================================  ==========================
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOGGER = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    """One request. Bound to loopback by :class:`TriggerShim`."""

    server_version = "shield-shim/1.0"

    @property
    def _shim(self) -> TriggerShim:
        return self.server.shim  # type: ignore[attr-defined]

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._reply(200, self._shim.health())
        elif self.path == "/state":
            self._reply(200, self._shim.state())
        else:
            self._reply(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in ("/trigger", "/fake-trigger"):
            self._reply(404, {"error": "not found", "path": self.path})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._reply(400, {"error": "body is not JSON"})
            return

        try:
            self._reply(200, self._shim.trigger(body))
        except Exception as exc:  # noqa: BLE001 - a failed trigger must 500, not 500 the socket
            LOGGER.exception("trigger failed")
            self._reply(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.debug("shim %s - %s", self.address_string(), fmt % args)


class _Server(ThreadingHTTPServer):
    """Server that carries a reference to its shim."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], shim: TriggerShim) -> None:
        self.shim = shim
        super().__init__(address, _Handler)


class TriggerShim:
    """Serve the trigger endpoints on loopback while the service runs."""

    def __init__(
        self,
        trigger_fn: Callable[[dict[str, Any]], dict[str, Any]],
        health_fn: Callable[[], dict[str, Any]],
        state_fn: Callable[[], dict[str, Any]],
        port: int = 8811,
    ) -> None:
        self._trigger = trigger_fn
        self._health = health_fn
        self._state = state_fn
        self._server = _Server((LOOPBACK, port), self)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="shield-shim", daemon=True
        )
        self._thread.start()
        LOGGER.info("trigger shim listening on %s (loopback only)", self.url)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> TriggerShim:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def trigger(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._trigger(body)

    def health(self) -> dict[str, Any]:
        return self._health()

    def state(self) -> dict[str, Any]:
        return self._state()