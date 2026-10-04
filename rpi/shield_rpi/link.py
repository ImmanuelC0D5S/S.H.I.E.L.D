"""Sensor link framing and server. ``docs/PROTOCOL.md`` section 2.

The ESP32 is the TCP client; this module is the RPi server. Nothing here knows
anything about cameras, detection or alerts: it moves authenticated frames and
calls back into whatever handles a ``trigger``.

Wire format, per PROTOCOL.md 2.3::

    +--------+--------+------------------+--------+
    | len(4) | flags  |   body (len B)   | tag(16) |
    +--------+--------+------------------+--------+
       uint32   uint8     JSON, UTF-8      16 B
       big-end.

The tag is ``HMAC_SHA256(key, len || flags || body)[0:16]`` (2.5) and is
verified **before** the body is parsed, so a hostile frame can never reach the
JSON decoder.

Two properties from 2.3 are load-bearing and easy to get wrong:

* There is no magic byte to scan for, so a desynchronised stream **cannot** be
  resynchronised. A bad ``len`` or a timeout closes the connection and the
  sender reconnects (2.7).
* ``len`` above :data:`MAX_BODY` is a protocol violation, not a large message.
  It is dropped and counted as a fault rather than allocated.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 2
MAX_BODY = 8192
FLAG_JSON = 0x01
TAG_BYTES = 16
HEADER_BYTES = 5
FRAME_OVERHEAD = HEADER_BYTES + TAG_BYTES

#: 2.7 step 5: at three faults on one connection, close it.
FAULT_LIMIT = 3

#: 2.8: no frame at all for this long means the sensor is down.
HEARTBEAT_TIMEOUT_S = 6.0


class LinkError(RuntimeError):
    """Base class for link failures."""


class FrameError(LinkError):
    """A frame could not be decoded or its tag did not verify."""


class TagMismatchError(FrameError):
    """The HMAC did not verify. Counted as a fault; the frame is not answered."""


class ProtocolViolationError(FrameError):
    """A structurally invalid frame: bad flags, oversized body, bad version."""


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------
def new_message(message_type: str, **fields: object) -> dict[str, object]:
    """Build the common envelope from PROTOCOL.md 2.4.

    ``id`` is 8 hex characters, unique per sender per boot. ``ts_ms`` is
    monotonic and is *not* wall clock; ``sent_at`` is the wall clock, and only
    exists so the event log can be read by a human.
    """
    return {
        "v": PROTOCOL_VERSION,
        "type": message_type,
        "id": secrets.token_hex(4),
        "ts_ms": int(time.monotonic() * 1000),
        "sent_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        **fields,
    }


def _key_bytes(key: str | bytes) -> bytes:
    if isinstance(key, bytes):
        return key
    return key.encode("utf-8")


def compute_tag(key: str | bytes, prefix: bytes, body: bytes) -> bytes:
    """``HMAC_SHA256(key, len || flags || body)[0:16]`` (PROTOCOL.md 2.5)."""
    return hmac.new(_key_bytes(key), prefix + body, sha256).digest()[:TAG_BYTES]


def encode_frame(message: dict[str, object], key: str | bytes, flags: int = FLAG_JSON) -> bytes:
    """Serialise ``message`` into one wire frame."""
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY:
        raise ProtocolViolationError(f"body is {len(body)} bytes, max is {MAX_BODY}")
    prefix = len(body).to_bytes(4, "big") + bytes([flags])
    return prefix + body + compute_tag(key, prefix, body)


@dataclass
class DecodedFrame:
    """One verified frame."""

    message: dict[str, object]
    flags: int


class FrameDecoder:
    """Incremental frame decoder. Feed bytes, receive verified frames.

    Stateful across feeds so a frame split across TCP reads is reassembled
    rather than dropped, which on a real socket is the common case, not the edge
    case.

    Two kinds of failure are handled differently, and the distinction matters.

    **Recoverable** - a bad tag, bad flags, bad JSON, wrong version. The frame
    boundary is still trustworthy, because ``len`` was range-checked against
    :data:`MAX_BODY` before the tag was examined, so exactly ``total`` bytes can
    be skipped and the next frame can be read. The fault is counted in
    :attr:`faults` and parsing continues. Counting one fault per *chunk* instead
    of per *frame* would let a peer pack a hundred forged frames into one packet
    and be judged as a single attempt against the three-fault limit.

    **Unrecoverable** - a ``len`` above :data:`MAX_BODY`. There is no magic byte
    to resynchronise on, so the stream position after this point is meaningless
    and :meth:`feed` raises. PROTOCOL.md 2.3 requires the receiver to close and
    let the sender reconnect rather than try to recover.
    """

    def __init__(self, key: str | bytes) -> None:
        self._key = _key_bytes(key)
        self._buffer = bytearray()
        self._faults: list[FrameError] = []

    @property
    def pending(self) -> int:
        """Bytes buffered for a frame that has not completed yet."""
        return len(self._buffer)

    @property
    def faults(self) -> list[FrameError]:
        """Recoverable faults seen so far, oldest first. Cleared by the caller
        via :meth:`take_faults` once it has logged them."""
        return list(self._faults)

    @property
    def fault_count(self) -> int:
        return len(self._faults)

    def take_faults(self) -> list[FrameError]:
        """Return the pending faults and clear them."""
        pending, self._faults = self._faults, []
        return pending

    def feed(self, chunk: bytes) -> list[DecodedFrame]:
        """Append ``chunk`` and return every frame that completed.

        Raises :class:`ProtocolViolationError` only when the stream cannot be
        trusted to continue. A single bad frame is counted and skipped: it is
        never returned and never answered.
        """
        self._buffer.extend(chunk)
        frames: list[DecodedFrame] = []

        while True:
            if len(self._buffer) < HEADER_BYTES:
                return frames
            length = int.from_bytes(self._buffer[0:4], "big")
            if length > MAX_BODY:
                raise ProtocolViolationError(f"declared body length {length} exceeds {MAX_BODY}")
            total = HEADER_BYTES + length + TAG_BYTES
            if len(self._buffer) < total:
                return frames

            prefix = bytes(self._buffer[0:HEADER_BYTES])
            body = bytes(self._buffer[HEADER_BYTES : HEADER_BYTES + length])
            tag = bytes(self._buffer[HEADER_BYTES + length : total])
            del self._buffer[:total]

            try:
                message, flags = self._verify(prefix, body, tag)
            except FrameError as exc:
                self._faults.append(exc)
                continue

            frames.append(DecodedFrame(message=message, flags=flags))

    def _verify(self, prefix: bytes, body: bytes, tag: bytes) -> tuple[dict[str, object], int]:
        """Authenticate then parse one frame's body.

        The tag check happens first, on purpose: PROTOCOL.md 2.5 requires that an
        unauthenticated body is never parsed, so a hostile frame cannot reach the
        JSON decoder at all.
        """
        expected = hmac.new(self._key, prefix + body, sha256).digest()[:TAG_BYTES]
        # Constant-time. A byte-by-byte == leaks a prefix match through timing,
        # which is exactly the forgery resistance 2.5 asks for.
        if not hmac.compare_digest(expected, tag):
            raise TagMismatchError("HMAC tag did not verify; frame dropped and not answered")

        flags = prefix[4]
        if not flags & FLAG_JSON:
            raise ProtocolViolationError(f"body is not flagged JSON (flags=0x{flags:02x})")

        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolViolationError(f"body is not valid JSON: {exc}") from exc

        if not isinstance(message, dict):
            raise ProtocolViolationError("body is not a JSON object")
        if message.get("v") != PROTOCOL_VERSION:
            raise ProtocolViolationError(f"unsupported protocol version {message.get('v')!r}")

        return message, flags


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
@dataclass
class LinkHealth:
    """Sensor liveness, per PROTOCOL.md 2.8."""

    last_frame_at: float | None = None
    link_down_logged: bool = False
    #: Counted only on the *down* edge, so this is the number of times the link
    #: was lost, not twice that. Recovery is logged but not counted.
    transitions: int = 0

    def note_frame(self, now: float | None = None) -> None:
        if self.link_down_logged:
            LOGGER.info("sensor link is back")
            self.link_down_logged = False
        self.last_frame_at = now if now is not None else time.monotonic()

    @property
    def alive(self) -> bool:
        if self.last_frame_at is None:
            return False
        return (time.monotonic() - self.last_frame_at) < HEARTBEAT_TIMEOUT_S

    def check(self) -> bool:
        """Log at most once per down transition. Returns liveness."""
        if self.alive:
            return True
        if not self.link_down_logged:
            # 2.8: log WARNING once per transition and do not process triggers.
            LOGGER.warning(
                "sensor link down: no frame for %.0fs; triggers will not be processed",
                HEARTBEAT_TIMEOUT_S,
            )
            self.link_down_logged = True
            self.transitions += 1
        return False


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
FrameHandler = Callable[[dict[str, object], "LinkConnection"], None]


@dataclass
class LinkStats:
    frames_in: int = 0
    faults: int = 0
    connections: int = 0
    rejected: int = 0


class LinkConnection:
    """One accepted sensor connection. Frames in, frames out."""

    def __init__(
        self,
        sock: socket.socket,
        key: str | bytes,
        frame_timeout_s: float,
        send_lock: threading.Lock,
    ) -> None:
        self._sock = sock
        self._key = _key_bytes(key)
        self._decoder = FrameDecoder(self._key)
        self._timeout = frame_timeout_s
        self._send_lock = send_lock
        self._closed = False

    def send(self, message: dict[str, object]) -> None:
        """Send one frame. Serialised: replies and acks can overlap."""
        frame = encode_frame(message, self._key)
        with self._send_lock:
            if self._closed:
                raise LinkError("connection is closed")
            try:
                self._sock.sendall(frame)
            except OSError as exc:
                raise LinkError(f"send failed: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing an already-dead socket is routine, not an error worth raising.
        with contextlib.suppress(OSError):
            self._sock.close()

    def __enter__(self) -> LinkConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def serve_forever(self, handler: FrameHandler, health: LinkHealth, stats: LinkStats) -> None:
        """Read frames until the peer disconnects or the link faults out.

        A dropped connection is normal (RPi reboot, AP restart) and must never
        take the server down, so every failure path here closes this socket and
        returns rather than propagating.
        """
        faults = 0
        try:
            while not self._closed:
                try:
                    chunk = self._sock.recv(4096)
                except TimeoutError:
                    LOGGER.warning("frame read timed out after %.1fs; closing", self._timeout)
                    return
                except OSError as exc:
                    LOGGER.warning("socket read failed: %s", exc)
                    return

                if not chunk:
                    LOGGER.info("sensor closed the connection")
                    return

                try:
                    frames = self._decoder.feed(chunk)
                except ProtocolViolationError as exc:
                    # Unrecoverable: the stream position is no longer trustworthy.
                    LOGGER.warning("protocol violation, closing: %s", exc)
                    stats.faults += 1
                    return

                # Recoverable faults: each bad frame counts individually, so a peer
                # cannot pack many forgeries into one packet and be judged as
                # one attempt against the limit. PROTOCOL.md 2.7 step 5.
                pending = self._decoder.take_faults()
                if pending:
                    faults += len(pending)
                    stats.faults += len(pending)
                    LOGGER.warning(
                        "frame faults %d/%d on this connection (dropped, not answered): %s",
                        faults,
                        FAULT_LIMIT,
                        "; ".join(str(fault) for fault in pending),
                    )
                if faults >= FAULT_LIMIT:
                    LOGGER.warning("fault limit reached; closing connection")
                    return

                for decoded in frames:
                    stats.frames_in += 1
                    health.note_frame()
                    try:
                        handler(decoded.message, self)
                    except Exception:  # noqa: BLE001 - a bad handler must not kill the link
                        LOGGER.exception("handler raised on %s", decoded.message.get("type"))
        finally:
            self.close()


class LinkServer:
    """The RPi side of PROTOCOL.md 2.7.

    One sensor node is expected: a second concurrent connection is rejected and
    closed rather than queued, because two clients competing for one event
    stream means triggers get attributed to whichever node answered first.

    :attr:`reconnect_grace_s` softens that rule for one specific case. A node
    that drops and immediately reconnects arrives while the server is still
    reading the dead socket, so a strict "one at a time" rule refuses a perfectly
    legitimate reconnect and the node has to sit out its backoff. PROTOCOL.md 2.7
    step 3 gives that backoff as 500 ms upward, so the grace window is sized to
    cover a 500 ms retry. A genuinely second node still waits the full window and
    is then refused, so the rule is delayed rather than weakened.
    """

    def __init__(
        self,
        host: str,
        port: int,
        key: str | bytes,
        *,
        frame_timeout_s: float = 5.0,
        backlog: int = 1,
        reconnect_grace_s: float = 1.0,
    ) -> None:
        self.host = host
        self.port = port
        self.key = key
        self.frame_timeout_s = frame_timeout_s
        self.reconnect_grace_s = reconnect_grace_s
        self.health = LinkHealth()
        self.stats = LinkStats()
        self._backlog = backlog
        self._server: socket.socket | None = None
        self._active: LinkConnection | None = None
        self._slot_released = threading.Event()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def sensor_online(self) -> bool:
        return self.health.alive

    def bind(self) -> socket.socket:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(self._backlog)
        self._server = server
        LOGGER.info("link server listening on %s:%d", self.host, self.port)
        return server

    def serve_forever(self, handler: FrameHandler) -> None:
        """Accept forever, servicing the one permitted sensor connection.

        The accept loop and the connection service run concurrently on purpose.
        PROTOCOL.md 2.7 requires that "a second concurrent connection is rejected
        and closed", and a server that services a connection in its own accept
        loop is structurally unable to notice a second one: it is not calling
        ``accept()`` while the first client is connected, so the newcomer sits
        silently in the backlog instead of being refused.

        Never raises on a client-side problem: 2.7 step 6 requires that a
        dropped connection cannot take the server down.
        """
        server = self._server or self.bind()
        server.settimeout(0.5)
        LOGGER.info("waiting for the sensor node to connect")

        while not self._stop.is_set():
            try:
                sock, address = server.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                LOGGER.warning("accept failed: %s", exc)
                continue

            self.stats.connections += 1
            peer = f"{address[0]}:{address[1]}"

            if self._active is not None and not self._await_slot():
                self.stats.rejected += 1
                LOGGER.warning("rejecting second connection from %s; one sensor expected", peer)
                with contextlib.suppress(OSError):
                    sock.close()
                continue

            LOGGER.info("sensor connected from %s", peer)
            sock.settimeout(self.frame_timeout_s)
            connection = LinkConnection(sock, self.key, self.frame_timeout_s, self._send_lock)
            self._active = connection

            worker = threading.Thread(
                target=self._service,
                args=(connection, handler, peer),
                name="shield-link-conn",
                daemon=True,
            )
            worker.start()

    def _await_slot(self) -> bool:
        """Wait briefly for the active connection's slot to free up.

        Called when a connection arrives while one is already being served. If
        the current one is merely finishing - a dropped node reconnecting - the
        slot frees within the grace window and this newcomer is admitted. If the
        current one is a healthy second node, the window expires and the
        newcomer is rejected, which is what PROTOCOL.md 2.7 requires.

        Returns True when the slot is free, False when the grace period expired.
        """
        if self.reconnect_grace_s <= 0:
            return self._active is None

        self._slot_released.clear()
        return self._slot_released.wait(timeout=self.reconnect_grace_s)

    def _service(self, connection: LinkConnection, handler: FrameHandler, peer: str) -> None:
        """Service one connection until it ends, then release the slot."""
        try:
            connection.serve_forever(handler, self.health, self.stats)
        finally:
            # Only clear the slot if it is still ours: a stale worker clearing a
            # newer connection's slot would allow two sensors in at once, which is
            # exactly what the rejection above exists to prevent.
            if self._active is connection:
                self._active = None
                self._slot_released.set()
            LOGGER.info("sensor at %s disconnected", peer)

    def shutdown(self) -> None:
        self._stop.set()
        if self._active is not None:
            self._active.close()
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
            self._server = None

    def __enter__(self) -> LinkServer:
        self.bind()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()