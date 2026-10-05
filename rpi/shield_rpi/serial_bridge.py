"""UART bridge for the bench demo. ``shield-serial-bridge-prompt.md``.

    PIR -> ESP32 -> serial line "MOTION" -> classify -> "ALERT" or "SAFE" -> buzzer

Three design points that are not obvious and that the tests pin:

**The classification runs on the reader thread, and the lines that arrive while
it does are thrown away.** A burst is 5 frames plus two inference passes, so on
this hardware it is tens to hundreds of milliseconds. A person walking past a
PIR with the default trigger retrigger time fires it many times in that window.
Replaying all of those would queue one classification per edge and answer them
out of order minutes later. So :meth:`_handle_motion` sets a busy flag, and the
flush afterwards discards whatever piled up. The person who caused the alert
still gets their answer; the echoes of it do not.

**Cooldown produces silence, not `SAFE`.** ``_handle_motion`` returns before the
pipeline runs when the publisher is inside its cooldown, so nothing is written
back. Sending `SAFE` there would chirp the buzzer for an event that was never
evaluated, which is a reassuring noise in exactly the situation where nobody is
listening yet.

**The port is injected, not looked up.** :meth:`_open` is the single place that
touches pyserial, so the tests supply a fake instead of a real device. That is
also the only way to test this on Windows, which has no pty.

This path is **plaintext line protocol with no authentication**. It is a demo
link between two boards on a bench. PROTOCOL.md 9 says so explicitly. Do not
point it at anything that faces a network.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from shield_rpi.config import SerialConfig

LOGGER = logging.getLogger(__name__)

#: The only word that triggers a classification. Anything else is logged and
#: dropped, so a noisy or misconfigured link cannot cause spurious bursts.
CMD_MOTION = "MOTION"
REPLY_ALERT = "ALERT"
REPLY_SAFE = "SAFE"

#: Statuses that mean "buzzer" (PROTOCOL.md 2.4.3). Kept as the same integers
#: the pipeline and the ESP32 use, so there is one vocabulary.
STATUS_ALERT = 2


class SerialLike(Protocol):
    """The slice of ``serial.Serial`` this module uses.

    Declared rather than relied on so the fake in the tests is checked against
    the same contract by the type checker.
    """

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...
    @property
    def is_open(self) -> bool: ...


class SerialBridgeError(RuntimeError):
    """Raised when the port cannot be opened at all."""


class SerialBridge:
    """Reads ``MOTION`` from a serial port and answers ``ALERT`` / ``SAFE``.

    :param on_motion: called with no arguments; must return the pipeline reply
        dict. This is the *same* callable the loopback ``/trigger`` shim uses
        (``Service._shim_trigger``), which is the point: there is one
        classification path, so the serial demo and the HTTP demo cannot
        disagree about what a face means.
    :param in_cooldown: consulted before classifying. Returns True while the
        publisher is inside ``alert.cooldown_s``.
    :param on_status: optional hook, called with each reply dict. Lets the
        service log and record the event without the bridge knowing anything
        about events.
    """

    def __init__(
        self,
        port: str,
        on_motion: Callable[[], dict[str, Any]],
        *,
        in_cooldown: Callable[[], bool] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        config: SerialConfig | None = None,
        opener: Callable[[str, int, float], SerialLike] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.port = port
        self.config = config or SerialConfig(port=port)
        self._on_motion = on_motion
        self._in_cooldown = in_cooldown or (lambda: False)
        self._on_status = on_status
        self._opener = opener
        self._stop = stop_event or threading.Event()

        self._serial: SerialLike | None = None
        self._rx = bytearray()
        self._busy = False

        #: Counters, so /health and the tests can see what happened.
        self.motion_seen = 0
        self.motion_handled = 0
        self.skipped_busy = 0
        self.skipped_cooldown = 0
        self.alerts_sent = 0
        self.safe_sent = 0
        self.reconnects = 0
        self.port_open = False

    # -- port plumbing ----------------------------------------------------

    def _open(self) -> SerialLike:
        """Open the port. The only place pyserial is touched."""
        if self._opener is not None:
            return self._opener(self.port, self.config.baud, self.config.read_timeout_s)

        try:
            import serial  # noqa: PLC0415 - optional dependency, see the class docstring
        except ImportError as exc:
            raise SerialBridgeError(
                "pyserial is not installed. The UART demo path needs it and "
                "nothing else does: pip install pyserial"
            ) from exc

        try:
            return serial.Serial(
                port=self.port,
                baudrate=self.config.baud,
                timeout=self.config.read_timeout_s,
            )
        except Exception as exc:  # pyserial raises SerialException and OSError
            raise SerialBridgeError(f"cannot open {self.port}: {exc}") from exc

    def _close(self) -> None:
        if self._serial is not None:
            # Closing a port that is already gone is routine on a yanked USB
            # cable, not an error worth propagating.
            with contextlib.suppress(Exception):
                self._serial.close()
        self._serial = None
        self.port_open = False

    # -- io ----------------------------------------------------------------

    def _send(self, line: str) -> None:
        """Write one command, newline terminated."""
        if self._serial is None:
            LOGGER.warning("no serial port open; dropping %r", line)
            return
        payload = (line + "\n").encode("ascii")
        try:
            self._serial.write(payload)
            LOGGER.info("uart out: %s", line)
        except Exception as exc:  # noqa: BLE001
            # A write failure usually means the cable came out mid-write. Raise
            # so the read loop treats it as a dropped port and reconnects, rather
            # than continuing to read a port that cannot answer.
            raise SerialBridgeError(f"write failed on {self.port}: {exc}") from exc

    def _flush_input(self) -> None:
        """Discard everything queued on the port.

        Called after each classification. See the class docstring for why this
        matters more than it looks.
        """
        if self._serial is None:
            return
        try:
            self._serial.reset_input_buffer()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("could not flush %s: %s", self.port, exc)
        self._rx.clear()

    # -- line handling -----------------------------------------------------

    def _handle_line(self, raw: str) -> None:
        line = raw.strip().strip("\x00")
        if not line:
            return
        LOGGER.info("uart in: %s", line)

        # Case-insensitive, because the sketch is a human-edited file and
        # `MOTION` vs `motion` should not be a silent dead link.
        if line.upper() != CMD_MOTION:
            LOGGER.warning("uart: ignoring unrecognised command %r", line)
            return

        self.motion_seen += 1

        if self._busy:
            self.skipped_busy += 1
            LOGGER.info("uart: MOTION ignored, a classification is already running")
            return

        if self._in_cooldown():
            self.skipped_cooldown += 1
            LOGGER.info("uart: MOTION ignored, inside the alert cooldown")
            return

        self._busy = True
        try:
            self._classify()
        finally:
            # Both of these must happen even if classification raised: the flag
            # or the bridge wedges and never answers again.
            self._busy = False
            self._flush_input()

    def _classify(self) -> None:
        try:
            reply = self._on_motion()
        except Exception:  # noqa: BLE001
            # Never answer on a failed classification. An ALERT here would be a
            # lie and a SAFE here would stand a real intruder down; silence is
            # the only honest reply, and the RPi's own escalation owns the rest.
            LOGGER.exception("classification failed; not answering the node")
            return

        self.motion_handled += 1
        if self._on_status is not None:
            try:
                self._on_status(reply)
            except Exception:  # noqa: BLE001
                LOGGER.exception("on_status hook failed")

        status = reply.get("status")
        if status == STATUS_ALERT:
            self.alerts_sent += 1
            LOGGER.info(
                "uart: status 2 (%s, %s%%) -> %s",
                reply.get("reason"),
                reply.get("confidence"),
                REPLY_ALERT,
            )
            self._send(REPLY_ALERT)
        else:
            self.safe_sent += 1
            LOGGER.info("uart: status %s -> %s", status, REPLY_SAFE)
            self._send(REPLY_SAFE)

    def _drain_lines(self, chunk: bytes) -> None:
        """Split a chunk into lines and handle each complete one.

        A partial trailing line stays buffered: a serial read can split
        ``MOTION\n`` anywhere, and acting on a half-line would classify on the
        letter ``M``.
        """
        self._rx.extend(chunk)
        while True:
            index = self._rx.find(b"\n")
            if index < 0:
                return
            line = bytes(self._rx[:index])
            del self._rx[: index + 1]
            self._handle_line(line.decode("utf-8", "replace"))

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        """Read until stopped, reconnecting every ``serial.reconnect_s``.

        Never raises. A demo bridge that dies when the USB cable is bumped is
        worse than useless, because the symptom it produces - no buzzer, no log
        line - looks exactly like a working system that had nothing to report.
        """
        LOGGER.info("serial bridge: opening %s at %d baud", self.port, self.config.baud)

        while not self._stop.is_set():
            if self._serial is None:
                try:
                    self._serial = self._open()
                except SerialBridgeError as exc:
                    LOGGER.warning("serial bridge: %s; retrying in %.1fs", exc, self.config.reconnect_s)
                    self._stop.wait(self.config.reconnect_s)
                    continue
                self.port_open = True
                if self.reconnects:
                    LOGGER.info("serial bridge: reconnected to %s", self.port)
                else:
                    LOGGER.info("serial bridge: connected to %s", self.port)
                self._rx.clear()

            try:
                waiting = getattr(self._serial, "in_waiting", 0) or 0
                chunk = self._serial.read(max(1, waiting))
            except SerialBridgeError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("serial bridge: read failed on %s: %s", self.port, exc)
                self._close()
                self.reconnects += 1
                self._stop.wait(self.config.reconnect_s)
                continue

            if not chunk:
                # Timeout with nothing pending. The normal case: it is what makes
                # the loop a loop rather than a blocking read.
                continue

            self._drain_lines(chunk)

        self._close()
        LOGGER.info("serial bridge: stopped")

    def start(self) -> threading.Thread:
        """Run :meth:`run` on a daemon thread and return it."""
        thread = threading.Thread(target=self.run, name="shield-serial", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "open": self.port_open,
            "motion_seen": self.motion_seen,
            "motion_handled": self.motion_handled,
            "skipped_busy": self.skipped_busy,
            "skipped_cooldown": self.skipped_cooldown,
            "alerts_sent": self.alerts_sent,
            "safe_sent": self.safe_sent,
            "reconnects": self.reconnects,
        }


def list_ports() -> list[str]:
    """Serial port names, or an empty list when pyserial is absent.

    Convenience for the CLI and for writing down what was actually plugged in.
    """
    try:
        from serial.tools import list_ports as lp  # noqa: PLC0415
    except ImportError:
        LOGGER.warning("pyserial is not installed; cannot enumerate ports")
        return []
    return [p.device for p in lp.comports()]


def wait_for_motion(port: str, timeout_s: float = 30.0) -> None:
    """Block until a ``MOTION`` line arrives. For manual bench testing.

    Not used by the service: the bridge consumes the port, and two readers on one
    serial port is a race. This is for the one-off case where someone has the
    bridge stopped and wants to confirm the node is talking at all.
    """
    try:
        import serial  # noqa: PLC0415
    except ImportError as exc:
        raise SerialBridgeError("pyserial is not installed") from exc

    LOGGER.info("waiting for MOTION on %s (%.0fs)", port, timeout_s)
    with serial.Serial(port=port, baudrate=115200, timeout=1.0) as link:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = link.readline()
            if not line:
                continue
            text = line.decode("utf-8", "replace").strip()
            LOGGER.info("uart in: %s", text)
            if text.upper() == CMD_MOTION:
                LOGGER.info("MOTION received")
                return
    raise SerialBridgeError(f"no MOTION within {timeout_s:.0f}s")
