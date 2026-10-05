"""Serial bridge tests. ``shield-serial-bridge-prompt.md`` Task 3.

**Why a fake serial object rather than a pty.** The prompt asks for "a pty /
loopback fake serial". A pty is the right answer on Linux, and this machine is
Windows, which has no ``os.openpty`` and no ``pty`` module. Rather than skip the
test on one platform, :meth:`SerialBridge._open` is the single seam that touches
pyserial, so the fake is injected there.

That is better than a pty for this module anyway. A pty would exercise the OS
line discipline - framing, buffering, CR/LF translation - none of which this
module implements, because it hands that to pyserial. What is worth testing is
the logic *around* the port: which lines are commands, what is discarded while a
classification runs, when silence is the right answer, and whether a dropped port
recovers. A fake covers all of it and runs anywhere.
"""
from __future__ import annotations

import threading
import time

import pytest
from shield_rpi.config import SerialConfig
from shield_rpi.serial_bridge import (
    CMD_MOTION,
    REPLY_ALERT,
    REPLY_SAFE,
    SerialBridge,
    SerialBridgeError,
)
from shield_rpi.service import Service

CLEAR = 0
KNOWN = 1
ALERT = 2


class FakeSerial:
    """The slice of ``serial.Serial`` the bridge uses.

    ``incoming`` is a bytearray the test appends to, standing in for the node
    on the other end. ``in_waiting`` is what pyserial exposes, so the read path
    behaves the same as it does against a real port.
    """

    def __init__(self, initial: bytes = b"", *, fail_after: int | None = None) -> None:
        self.incoming = bytearray(initial)
        self.written = bytearray()
        self.is_open = True
        self.closed = False
        self.resets = 0
        self._fail_after = fail_after
        self.reads = 0

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def read(self, size: int = 1) -> bytes:
        self.reads += 1
        if self._fail_after is not None and self.reads > self._fail_after:
            raise OSError("device disconnected")
        if not self.incoming:
            return b""
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("port closed")
        self.written.extend(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.resets += 1
        self.incoming.clear()

    def close(self) -> None:
        self.closed = True
        self.is_open = False

    def lines(self) -> list[str]:
        return [line for line in self.written.decode("ascii").split("\n") if line]


def make_bridge(
    replies: list[dict] | None = None,
    *,
    serial: FakeSerial | None = None,
    in_cooldown=lambda: False,
    on_status=None,
    **config_kwargs,
) -> tuple[SerialBridge, FakeSerial, list]:
    """A bridge wired to a fake port. Returns the bridge, the port and the calls."""
    port = serial if serial is not None else FakeSerial()
    calls: list = []
    queue = list(replies or [])

    def on_motion() -> dict:
        calls.append("motion")
        return queue.pop(0) if queue else {"status": CLEAR}

    bridge = SerialBridge(
        port="FAKE0",
        on_motion=on_motion,
        in_cooldown=in_cooldown,
        on_status=on_status,
        config=SerialConfig(port="FAKE0", **config_kwargs),
        opener=lambda *_args: port,
    )
    # Attach the port the way run() would. Tests that drive _drain_lines directly
    # skip the open step, and without this every reply is dropped as "no serial
    # port open" - which is correct behaviour and useless in a test.
    bridge._serial = port
    bridge.port_open = True
    return bridge, port, calls


def drive(bridge: SerialBridge, chunk: bytes) -> None:
    """Feed one chunk the way the read loop does, without the loop's waiting."""
    bridge._drain_lines(chunk)


# -- the core contract -------------------------------------------------------


def test_motion_produces_alert():
    bridge, port, calls = make_bridge([{"status": ALERT, "confidence": 87, "reason": "unknown"}])

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [REPLY_ALERT]
    assert calls == ["motion"]
    assert bridge.alerts_sent == 1
    assert bridge.safe_sent == 0


def test_known_produces_safe_and_no_alert():
    bridge, port, calls = make_bridge([{"status": KNOWN, "confidence": 88, "identity": "imman"}])

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [REPLY_SAFE]
    assert bridge.safe_sent == 1
    assert bridge.alerts_sent == 0


def test_low_conf_produces_alert():
    """PROTOCOL.md 2.4.3: status 2 covers both unknown and low_conf, and both
    alarm. Only 0 and 1 are silent."""
    bridge, port, _ = make_bridge([{"status": ALERT, "reason": "low_conf", "confidence": 40}])

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [REPLY_ALERT]


def test_no_person_produces_safe():
    bridge, port, _ = make_bridge([{"status": CLEAR}])

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [REPLY_SAFE]


@pytest.mark.parametrize("status", [CLEAR, KNOWN])
def test_only_status_two_alarms(status):
    """A bug that mapped KNOWN to ALERT would wake the house every time the owner
    came home, which is how an alarm gets switched off."""
    bridge, port, _ = make_bridge([{"status": status}])

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [REPLY_SAFE]


# -- line handling -----------------------------------------------------------


def test_lines_arrive_split_across_reads():
    """A serial read can end mid-command. Acting on a partial line would
    classify on the letter M."""
    bridge, port, calls = make_bridge([{"status": ALERT}], serial=FakeSerial())

    for chunk in (b"MO", b"TION", b"\n"):
        drive(bridge, chunk)

    assert calls == ["motion"]
    assert port.lines() == [REPLY_ALERT]


def test_a_burst_in_one_chunk_yields_exactly_one_reply():
    """The flush, in one assertion.

    A PIR retriggering during a classification puts several MOTION lines in one
    read. They must collapse to one event and one reply - this is the whole
    reason the input buffer is flushed, and a regression here is a queue of
    stale events answered out of order minutes later.
    """
    bridge, port, calls = make_bridge([{"status": ALERT}], serial=FakeSerial())

    drive(bridge, f"{CMD_MOTION}\n{CMD_MOTION}\n{CMD_MOTION}\n".encode())

    assert calls == ["motion"]
    assert port.lines() == [REPLY_ALERT]
    assert bridge.motion_handled == 1


def test_two_motions_in_separate_reads_are_each_answered():
    """The counterpart: a flush must not swallow a genuinely new trigger."""
    bridge, port, calls = make_bridge([{"status": ALERT}, {"status": CLEAR}], serial=FakeSerial())

    drive(bridge, f"{CMD_MOTION}\n".encode())
    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert calls == ["motion", "motion"]
    assert port.lines() == [REPLY_ALERT, REPLY_SAFE]


def test_a_partial_trailing_line_waits():
    bridge, port, calls = make_bridge([{"status": ALERT}], serial=FakeSerial())

    drive(bridge, f"{CMD_MOTION}\nMOTI".encode())

    assert calls == ["motion"], "MOTI is not a command yet"


def test_crlf_and_whitespace_are_tolerated():
    bridge, port, _ = make_bridge([{"status": ALERT}], serial=FakeSerial())

    drive(bridge, b"  MOTION \r\n")

    assert port.lines() == [REPLY_ALERT]


def test_lowercase_is_accepted():
    """The sketch is a hand-edited file; MOTION and motion should not be a
    silently dead link."""
    bridge, port, _ = make_bridge([{"status": ALERT}], serial=FakeSerial())

    drive(bridge, b"motion\n")

    assert port.lines() == [REPLY_ALERT]


def test_unknown_commands_are_ignored_without_classifying():
    bridge, port, calls = make_bridge([], serial=FakeSerial())

    drive(bridge, b"PING\nHELLO\n")

    assert calls == []
    assert port.lines() == [], "an unknown command must not produce a reply"


def test_blank_lines_are_ignored():
    bridge, _, calls = make_bridge([], serial=FakeSerial())

    drive(bridge, b"\n\n\n")

    assert calls == []


# -- discard-while-busy, the reason this module exists ------------------------


def test_motion_during_a_classification_is_discarded():
    """The person who caused the alert still gets an answer; the echoes of it do
    not queue up behind a classification that takes a second."""
    bridge, port, calls = make_bridge([{"status": ALERT}], serial=FakeSerial())

    # Simulate the reader being inside _classify by flipping the busy flag the
    # same way _handle_motion does.
    bridge._busy = True
    drive(bridge, f"{CMD_MOTION}\n".encode())
    bridge._busy = False

    assert calls == [], "a MOTION during a classification must not be handled"
    assert port.lines() == []
    assert bridge.skipped_busy == 1


def test_input_is_flushed_after_every_classification():
    """Without this, a PIR that fired 10 times during one classification replays
    all 10 as separate events."""
    port = FakeSerial(initial=b"MOTION\nMOTION\nMOTION\n")
    bridge, port, _ = make_bridge([{"status": ALERT}], serial=port)

    drive(bridge, b"MOTION\n")

    assert port.resets >= 1, "the port must be flushed after handling"
    assert port.incoming == bytearray(), "queued MOTION lines must be discarded"
    assert port.lines() == [REPLY_ALERT]


def test_the_busy_flag_is_released_even_when_classification_raises():
    """A leaked flag wedges the bridge: every later MOTION is 'busy' and nothing
    is ever answered again."""

    def boom() -> dict:
        raise RuntimeError("pipeline exploded")

    bridge = SerialBridge(
        port="FAKE0",
        on_motion=boom,
        config=SerialConfig(port="FAKE0"),
        opener=lambda *_a: FakeSerial(),
    )

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert bridge._busy is False, "the busy flag must not survive a failure"
    assert bridge.motion_handled == 0, "a failed classification handled nothing"


def test_the_port_is_flushed_even_when_classification_raises():
    bridge = SerialBridge(
        port="FAKE0",
        on_motion=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        config=SerialConfig(port="FAKE0"),
        opener=lambda *_a: FakeSerial(initial=b"MOTION\n"),
    )

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert bridge.motion_handled == 0


def test_a_failed_classification_answers_nothing():
    """An ALERT on a failed run would be a lie; a SAFE would stand a real
    intruder down. Silence is the only honest reply."""
    bridge, port, _ = make_bridge([], serial=FakeSerial())
    bridge._on_motion = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [], "a failed classification must not answer"


# -- cooldown ----------------------------------------------------------------


def test_cooldown_produces_silence_not_safe():
    """SAFE here would chirp the buzzer for an event that was never evaluated -
    reassuring noise exactly when nobody is listening yet."""
    bridge, port, calls = make_bridge([{"status": ALERT}], in_cooldown=lambda: True)

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert calls == [], "cooldown must short-circuit before the pipeline"
    assert port.lines() == []
    assert bridge.skipped_cooldown == 1


def test_cooldown_releases_again():
    state = {"cool": True}
    bridge, port, calls = make_bridge(
        [{"status": ALERT}, {"status": ALERT}], in_cooldown=lambda: state["cool"]
    )

    drive(bridge, f"{CMD_MOTION}\n".encode())
    assert port.lines() == []

    state["cool"] = False
    drive(bridge, f"{CMD_MOTION}\n".encode())
    assert port.lines() == [REPLY_ALERT]


# -- status hook -------------------------------------------------------------


def test_the_status_hook_sees_every_reply():
    seen: list[dict] = []
    bridge, _, _ = make_bridge(
        [{"status": ALERT, "confidence": 87}, {"status": CLEAR}],
        on_status=seen.append,
    )

    # Separate reads, so the second is a new event rather than a flushed echo.
    drive(bridge, f"{CMD_MOTION}\n".encode())
    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert [reply["status"] for reply in seen] == [ALERT, CLEAR]


def test_a_broken_status_hook_does_not_stop_the_answer():
    """The buzzer must be told regardless of whether the event log worked."""
    def explode(_reply: dict) -> None:
        raise RuntimeError("disk full")

    bridge, port, _ = make_bridge([{"status": ALERT}], on_status=explode)

    drive(bridge, f"{CMD_MOTION}\n".encode())

    assert port.lines() == [REPLY_ALERT]


# -- reconnect ---------------------------------------------------------------


def test_a_failing_open_retries_rather_than_dying():
    """A bridge that exits when the USB cable is bumped produces no buzzer and
    no log line, which looks exactly like a system with nothing to report."""
    attempts = {"n": 0}
    stop = threading.Event()

    def opener(*_args):
        attempts["n"] += 1
        if attempts["n"] >= 3:
            stop.set()
        raise SerialBridgeError(f"cannot open FAKE0 (attempt {attempts['n']})")

    bridge = SerialBridge(
        port="FAKE0",
        on_motion=lambda: {"status": CLEAR},
        config=SerialConfig(port="FAKE0", reconnect_s=0.01, read_timeout_s=0.01),
        opener=opener,
        stop_event=stop,
    )

    thread = threading.Thread(target=bridge.run, daemon=True)
    thread.start()
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "run() did not return after the stop event"
    assert attempts["n"] == 3, "must keep retrying until the port opens"


def test_a_read_failure_closes_and_counts_a_reconnect():
    port = FakeSerial(fail_after=1, initial=b"MOTION\n")
    bridge, _, _ = make_bridge([{"status": ALERT}], serial=port)

    # First read succeeds and handles the MOTION.
    drive(bridge, b"MOTION\n")

    assert bridge.alerts_sent == 1
    assert port.lines() == [REPLY_ALERT]


def test_stop_closes_the_port():
    """A port left open after stop() keeps the node talking into nothing."""
    port = FakeSerial()
    stop = threading.Event()
    bridge = SerialBridge(
        port="FAKE0",
        on_motion=lambda: {"status": CLEAR},
        config=SerialConfig(port="FAKE0", read_timeout_s=0.01),
        opener=lambda *_a: port,
        stop_event=stop,
    )

    thread = threading.Thread(target=bridge.run, daemon=True)
    thread.start()

    for _ in range(200):
        if bridge.port_open:
            break
        threading.Event().wait(0.01)
    assert bridge.port_open, "the port never opened"

    stop.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert port.closed is True
    assert bridge.port_open is False


def test_run_returns_promptly_when_already_stopped():
    bridge, port, _ = make_bridge(serial=FakeSerial())
    bridge._stop.set()

    bridge.run()

    assert bridge.port_open is False
    assert port.closed is True, "run() closes the port on the way out, stopped or not"


# -- config ------------------------------------------------------------------


def test_serial_is_off_by_default():
    assert SerialConfig().enabled is False
    assert SerialConfig(port="").enabled is False


@pytest.mark.parametrize("port", ["COM5", "/dev/serial0", "COM17"])
def test_any_port_name_enables_it(port):
    assert SerialConfig(port=port).enabled is True


def test_the_shipped_config_leaves_serial_off(monkeypatch):
    """A deployment must not silently start opening a serial port."""
    from pathlib import Path

    from shield_rpi.config import load_config

    # This test reads the shipped config.yaml, which interpolates ${SHIELD_*}
    # from the environment. Without seeding them it only passed when the
    # developer's shell happened to have .env exported, so it was really
    # asserting on ambient state. Seed them; test_config.py owns the
    # missing-variable behaviour.
    for name, value in (
        ("SHIELD_LINK_KEY", "k" * 64),
        ("SUPABASE_URL", "https://shield.example"),
        ("SUPABASE_RPI_TOKEN", "tok"),
        ("SUPABASE_ANON_KEY", "project-anon-key"),
        ("SHIELD_DEVICE_ID", "shield-01"),
        ("COOLDOWN_S", "30"),
        ("VERDICT_TIMEOUT_S", "60"),
    ):
        monkeypatch.setenv(name, value)

    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "rpi" / "shield_rpi" / "config.yaml")
    assert config.serial.port == ""
    assert config.serial.baud == 115200
    assert config.serial.reconnect_s == 2.0


# -- the service wiring ------------------------------------------------------
#
# The 30-odd tests above cover the bridge in isolation. These cover the thing
# the prompt actually asked for: "call the same handler the loopback /trigger
# shim uses". That is only true if somebody checks it, because the two paths are
# wired separately and either could be changed alone.


def build_service(tmp_path, monkeypatch, replies: list[dict]) -> Service:
    """A real Service, with only the camera stage replaced.

    Everything else is the production object graph - real Detector, Recogniser,
    Pipeline, publisher, queue - because the point is to prove the wiring, not to
    re-implement it. ``run_pipeline`` is the only thing stubbed, because that is
    where the camera and the models are.
    """
    from shield_rpi.config import (
        AlertConfig,
        CameraConfig,
        Config,
        DetectorConfig,
        LinkConfig,
        RecognizerConfig,
        SupabaseConfig,
        VerdictConfig,
    )

    config = Config(
        camera=CameraConfig(device=0, width=640, height=480),
        detector=DetectorConfig(
            face_model="m.pt", person_model="p.pt", imgsz=640, conf=0.5
        ),
        recognizer=RecognizerConfig(
            faces_dir=str(tmp_path), match_distance=0.5, alert_confidence=50
        ),
        link=LinkConfig(
            host="127.0.0.1",
            port=8765,
            heartbeat_s=2,
            reply_timeout_s=5,
            frame_timeout_s=5,
            max_body=8192,
            hmac_key="k" * 64,
        ),
        supabase=SupabaseConfig(
            url="https://shield.example",
            key="tok",
            anon_key="project-anon-key",
            bucket="snapshots",
            device_prefix="shield-01",
            queue_dir=str(tmp_path / "pending"),
        ),
        alert=AlertConfig(),
        verdict=VerdictConfig(),
    )

    service = Service(config)

    queue = list(replies)

    def fake_run_pipeline(_message=None) -> dict:
        reply = queue.pop(0)
        return {**reply, "jpeg": b"", "event_id": "e1", "latency_ms": 1.0}

    monkeypatch.setattr(service, "run_pipeline", fake_run_pipeline)
    # The alert path would try to publish to Supabase; the bridge's job is the
    # reply, and publishing is covered by test_alerts.py.
    monkeypatch.setattr(service, "_start_alert_work", lambda _reply: None)
    return service


def test_the_bridge_calls_the_shim_handler_and_answers(tmp_path, monkeypatch):
    """The prompt's requirement, asserted end to end.

    An alert from the pipeline must become ALERT on the wire, by way of the same
    `_shim_trigger` the loopback HTTP shim uses. If a future change gave serial
    its own status mapping, this fails - which is the whole reason the mapping is
    not duplicated.
    """
    service = build_service(tmp_path, monkeypatch, [{"status": ALERT, "confidence": 87}])
    port = FakeSerial()
    bridge = SerialBridge(
        port="FAKE0",
        on_motion=service._serial_motion,
        config=SerialConfig(port="FAKE0"),
        opener=lambda *_a: port,
    )
    bridge._serial = port

    drive(bridge, b"MOTION\n")

    assert port.lines() == [REPLY_ALERT]


def test_a_known_face_over_serial_answers_safe(tmp_path, monkeypatch):
    service = build_service(tmp_path, monkeypatch, [{"status": KNOWN, "identity": "imman"}])
    port = FakeSerial()
    bridge = SerialBridge(
        port="FAKE0",
        on_motion=service._serial_motion,
        config=SerialConfig(port="FAKE0"),
        opener=lambda *_a: port,
    )
    bridge._serial = port

    drive(bridge, b"MOTION\n")

    assert port.lines() == [REPLY_SAFE]


def test_serial_and_http_shim_paths_share_one_handler(tmp_path, monkeypatch):
    """One handler, two callers — checked by spying rather than by identity.

    ``_serial_motion`` is a thin delegating method, not the same function
    object, so an identity check would fail for the right design. What matters is
    that it routes through ``_shim_trigger``, so a change to the HTTP path
    cannot leave the serial path answering differently.
    """
    service = build_service(tmp_path, monkeypatch, [{"status": ALERT}])
    calls: list[dict] = []
    original = service._shim_trigger

    def spy(body):
        calls.append(body)
        return original(body)

    monkeypatch.setattr(service, "_shim_trigger", spy)

    assert service._serial_motion()["status"] == ALERT
    assert len(calls) == 1, "_serial_motion must go through _shim_trigger"


def test_the_bridge_respects_the_services_cooldown(tmp_path, monkeypatch):
    """The real publisher's cooldown, not a stubbed one."""
    service = build_service(tmp_path, monkeypatch, [{"status": ALERT}, {"status": ALERT}])
    # Force the publisher into cooldown as a successful publish would.
    service.publisher._clock = lambda: 1000.0
    service.publisher._last_alert_at = 1000.0

    port = FakeSerial()
    bridge = SerialBridge(
        port="FAKE0",
        on_motion=service._serial_motion,
        in_cooldown=service.publisher.in_cooldown,
        config=SerialConfig(port="FAKE0", read_timeout_s=0.01),
        opener=lambda *_a: port,
    )
    bridge._serial = port

    drive(bridge, b"MOTION\n")

    assert port.lines() == [], "cooldown must produce silence over serial too"
    assert bridge.skipped_cooldown == 1


def test_serial_status_is_recorded_in_the_event_log(tmp_path, monkeypatch, tmp_path_factory):
    """A UART decision should be visible in events.jsonl next to a link one."""
    service = build_service(tmp_path, monkeypatch, [{"status": ALERT, "confidence": 87}])
    recorded: list[dict] = []
    monkeypatch.setattr(service, "record_event", recorded.append)

    service._serial_status({"status": ALERT, "confidence": 87})

    assert recorded and recorded[0]["type"] == "uart_verdict"
    assert recorded[0]["status"] == ALERT



# -- regression: the alert must actually reach publish() ---------------------


def _await_alert_worker(service, timeout: float = 5.0) -> None:
    """Block until _start_alert_work's worker has finished.

    The worker sets ``self._worker = None`` in its ``finally``, so a worker that
    fails fast is already gone by the time ``_start_alert_work`` returns.
    Capturing the thread and joining it therefore races, and asserted on the
    wrong object. Poll on liveness instead, and fail loudly on a real hang.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = service._worker
        if worker is None or not worker.is_alive():
            return
        time.sleep(0.01)
    raise AssertionError(f"alert worker still running after {timeout}s")


def test_alert_work_passes_an_image_path_to_publish(tmp_path, monkeypatch):
    service = build_service(tmp_path, monkeypatch, [])

    calls: list[dict] = []

    class RecordingPublisher:
        def in_cooldown(self) -> bool:
            return False

        def publish(self, jpeg, **kwargs):
            calls.append({"jpeg": jpeg, **kwargs})
            return kwargs.get("event_id", "published-id")

    monkeypatch.setattr(service, "publisher", RecordingPublisher())
    monkeypatch.setattr(service.verdicts, "watch", lambda _id, _active: None)

    reply = {
        "status": ALERT,
        "confidence": 87,
        "identity": None,
        "person_detected": True,
        "face_detected": True,
        "latency_ms": 812.0,
        "event_id": "3f2c9ab1-0000-4000-8000-000000000000",
        "jpeg": b"\xff\xd8\xff",
    }
    # build_service() replaces _start_alert_work with a no-op on the instance, so
    # `service._start_alert_work(...)` would call the stub and this test would
    # pass without ever reaching publish(). Go through the class to get the real
    # method -- which is exactly the wiring that shipped broken.
    Service._start_alert_work(service, reply)
    _await_alert_worker(service)

    assert calls, "publish() was never called, so no alert can ever be pushed"
    sent = calls[0]
    assert sent["jpeg"] == b"\xff\xd8\xff"
    # Must be a real object path: "<device>/<YYYY-MM-DD>/<event prefix>.jpg".
    path = sent["image_path"]
    assert path, "image_path is required by publish(); omitting it kills every alert"
    assert path.startswith("shield-01/"), path
    assert path.endswith(".jpg"), path
    assert path.split("/")[-1] == "3f2c9ab1.jpg", path
    assert len(path.split("/")) == 3, path
    assert sent["event_id"] == reply["event_id"]


def test_alert_work_survives_a_publisher_failure(tmp_path, monkeypatch):
    """A Supabase outage must not take the link thread down with it."""
    service = build_service(tmp_path, monkeypatch, [])

    class BrokenPublisher:
        def in_cooldown(self) -> bool:
            return False

        def publish(self, _jpeg, **_kwargs):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(service, "publisher", BrokenPublisher())

    reply = {
        "status": ALERT,
        "confidence": 87,
        "identity": None,
        "person_detected": True,
        "face_detected": True,
        "latency_ms": 812.0,
        "event_id": "3f2c9ab1-0000-4000-8000-000000000000",
"jpeg": b"\xff\xd8\xff",
    }
    Service._start_alert_work(service, reply)
    _await_alert_worker(service)

    assert service._worker is None or not service._worker.is_alive()
