"""Alert publication tests. ``shield-rpi-prompt.md`` and DECISIONS.md D-044.

Two things are under test and they pull in opposite directions.

**The module must not send FCM.** That is a design property, not a behaviour,
so it is asserted structurally: the source is scanned for FCM and service-account
references. A comment saying so is not evidence; the absence of the capability
is.

**It must never lose an alert.** Every failure mode ends in the alert being
retried from disk, because a dropped alert is indistinguishable from a quiet
house.
"""

from __future__ import annotations

import ast
import base64
import json
import time
from pathlib import Path

import pytest
from shield_rpi.alerts import (
    AlertEvent,
    AlertPublisher,
    AlertQueue,
    OfflineError,
    SupabaseClient,
    now_unix,
)
from shield_rpi.config import SupabaseConfig


def make_config(**overrides) -> SupabaseConfig:
    base = {
        "url": "https://shield.example",
        "key": "shield-rpi-token",
        "bucket": "snapshots",
        "device_prefix": "shield-01",
        "timeout_s": 1.0,
        "max_snapshot_bytes": 524288,
        "queue_dir": "",
        "queue_max_attempts": 3,
    }
    base.update(overrides)
    return SupabaseConfig(**base)


def make_event(**overrides) -> AlertEvent:
    base = {
        "event_id": "3f2c0000-0000-4000-8000-000000000000",
        "ts": 1760000000,
        "status": 2,
        "confidence": 87,
        "image_path": "shield-01/2026-10-09/3f2c.jpg",
        "device_id": "shield-01",
    }
    base.update(overrides)
    return AlertEvent(**base)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class RecordingClient(SupabaseClient):
    """Captures calls instead of making them."""

    def __init__(self, config: SupabaseConfig, *, fail_upload: bool = False) -> None:
        super().__init__(config)
        self.fail_upload = fail_upload
        self.uploads: list[tuple[bytes, str]] = []
        self.events: list[AlertEvent] = []

    def upload_snapshot(self, jpeg: bytes, path: str) -> str:
        if self.fail_upload:
            raise OfflineError("simulated network failure")
        self.uploads.append((jpeg, path))
        return path

    def insert_event(self, event: AlertEvent) -> None:
        self.events.append(event)


# -- D-044: alerts.py must not send FCM ------------------------------------


def test_alerts_module_never_sends_fcm():
    """D-044: the Edge Function owns the FCM push; the RPi holds no credential.

    Checked against the parsed AST rather than the file text, because the module
    docstring and comments legitimately *mention* FCM while forbidden to call it.
    Grepping the raw text would either fail on a good file or force the
    documentation to be removed. What is asserted is that no import, no
    identifier and no non-docstring string literal could reach FCM.

    The failure this prevents is silent and severe: a Firebase admin key on a Pi
    that sits on a kitchen counter.
    """
    source = Path(__file__).resolve().parents[1] / "shield_rpi" / "alerts.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    forbidden = ("fcm", "firebase", "service_account", "googleapis")

    imports: list[str] = []
    identifiers: list[str] = []
    literals: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.Attribute | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            identifiers.append(getattr(node, "name", "") or getattr(node, "attr", ""))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            literals.append(node.value.lower())

    assert not [name for name in imports if any(f in name.lower() for f in forbidden)], (
        f"alerts.py must not import anything Firebase-related: {imports}"
    )
    assert not [name for name in identifiers if any(f in name.lower() for f in forbidden)], (
        "alerts.py must not name anything Firebase-related"
    )
    assert not [
        text for text in literals if "fcm.googleapis" in text or "service_account" in text
    ], "alerts.py must not reference the FCM endpoint or a service account"


def test_publisher_has_no_fcm_method():
    assert not [
        name
        for name in dir(AlertPublisher)
        if "fcm" in name.lower() or "push" in name.lower() or "notify" in name.lower()
    ]


# -- schema ----------------------------------------------------------------


def test_event_row_matches_the_events_table():
    row = make_event(identity="imman", trigger_tier="high", latency_ms=420).to_row()
    assert set(row) == {
        "id", "ts", "status", "confidence", "image_path", "needs_verdict",
        "device_id", "identity", "trigger_tier", "latency_ms",
    }
    assert row["id"] == "3f2c0000-0000-4000-8000-000000000000"
    assert row["ts"] == 1760000000
    assert row["status"] == 2
    assert row["needs_verdict"] is True


def test_unmeasured_fields_are_omitted_not_nulled():
    """A null where a NOT NULL later appears is a silently-wrong value."""
    row = make_event().to_row()
    assert "identity" not in row
    assert "latency_ms" not in row
    assert "trigger_tier" not in row


def test_confidence_is_clamped():
    assert make_event(confidence=140).to_row()["confidence"] == 100
    assert make_event(confidence=-5).to_row()["confidence"] == 0


def test_now_unix_is_utc_seconds():
    assert abs(now_unix() - int(time.time())) < 5


# -- snapshot cap ----------------------------------------------------------


def test_snapshot_over_the_protocol_cap_is_refused():
    """PROTOCOL.md 3.3 caps a snapshot at 512 KB.

    Checked in the client so the failure names the byte count and the encoder,
    instead of arriving as an opaque 413 from the storage API.
    """
    client = SupabaseClient(make_config())
    with pytest.raises(Exception, match="over the"):
        client.upload_snapshot(b"\xff" * 524289, "shield-01/a.jpg")


# -- offline queue ---------------------------------------------------------


def test_failed_publish_is_queued_and_the_id_is_still_returned(tmp_path):
    """A late push must not skip the buzzer escalation, which needs the id."""
    client = RecordingClient(make_config(), fail_upload=True)
    queue = AlertQueue(tmp_path / "pending", client)
    publisher = AlertPublisher(client=client, queue=queue, cooldown_s=0.0)

    event_id = publisher.publish(b"jpeg-bytes", status=2, confidence=87, device_id="shield-01",
                                image_path="shield-01/a.jpg")

    assert event_id
    assert len(queue) == 1
    assert client.events == []


def test_queue_round_trips_the_jpeg_and_the_row(tmp_path):
    """The event row and the image must not be separable: a row pointing at a
    snapshot that was never uploaded is worse than no event."""
    client = RecordingClient(make_config())
    queue = AlertQueue(tmp_path / "pending", client)
    event = make_event(identity="imman")
    payload = b"\xff\xd8\xff\xe0 not really a jpeg"

    path = queue.put(event, payload)
    (loaded,) = [queue._load(path)]

    assert loaded.event.event_id == event.event_id
    assert loaded.event.image_path == event.image_path
    assert loaded.event.identity == "imman"
    assert loaded.jpeg == payload


def test_drain_publishes_and_removes_a_queued_alert(tmp_path):
    offline = RecordingClient(make_config(), fail_upload=True)
    queue = AlertQueue(tmp_path / "pending", offline)
    event = make_event()
    queue.put(event, b"jpeg")

    online = RecordingClient(make_config())
    online_queue = AlertQueue(tmp_path / "pending", online)
    assert online_queue.drain() == {"sent": 1, "failed": 0, "expired": 0}
    assert len(online_queue) == 0
    assert [e.event_id for e in online.events] == [event.event_id]


def test_drain_retries_with_backoff_after_a_failure(tmp_path):
    client = RecordingClient(make_config(), fail_upload=True)
    queue = AlertQueue(tmp_path / "pending", client, max_attempts=5, backoff_s=10.0)
    now = [0.0]
    queue._clock = lambda: now[0]

    queue.put(make_event(), b"jpeg")
    assert queue.drain() == {"sent": 0, "failed": 1, "expired": 0}

    # Still inside the backoff window: not retried.
    now[0] = 5.0
    assert queue.drain() == {"sent": 0, "failed": 0, "expired": 0}
    assert len(queue) == 1

    # Past the window: retried.
    now[0] = 11.0
    assert queue.drain() == {"sent": 0, "failed": 1, "expired": 0}
    assert (tmp_path / "pending").exists()


def test_backoff_grows_and_then_caps(tmp_path):
    client = RecordingClient(make_config(), fail_upload=True)
    queue = AlertQueue(tmp_path / "pending", client, max_attempts=10, backoff_s=5.0,
                       backoff_cap_s=40.0)
    now = [0.0]
    queue._clock = lambda: now[0]
    path = queue.put(make_event(), b"jpeg")

    delays = []
    for _ in range(5):
        queue.drain()
        queued = queue._load(path)
        delays.append(round(queued.next_attempt_at - now[0]))
        now[0] = queued.next_attempt_at

    assert delays[:3] == [5, 10, 20]
    assert max(delays) == 40, "backoff must cap"


def test_an_alert_is_abandoned_after_max_attempts_not_deleted(tmp_path):
    """Abandoned, not removed: it may be the only record that anything happened."""
    client = RecordingClient(make_config(), fail_upload=True)
    queue = AlertQueue(tmp_path / "pending", client, max_attempts=2, backoff_s=1.0)
    now = [0.0]
    queue._clock = lambda: now[0]
    queue.put(make_event(), b"jpeg")

    for _ in range(3):
        queue.drain()
        now[0] += 100

    assert list((tmp_path / "pending").glob("*.abandoned"))
    assert len(queue) == 0


def test_an_unreadable_queue_file_is_quarantined_not_deleted(tmp_path):
    corrupt = tmp_path / "pending" / "2026-01-01-broken.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json", encoding="utf-8")

    client = RecordingClient(make_config())
    queue = AlertQueue(corrupt.parent, client)

    assert queue.drain()["expired"] == 1
    assert list(corrupt.parent.glob("*.corrupt-*"))
    assert not corrupt.exists()


# -- cooldown --------------------------------------------------------------


def test_cooldown_suppresses_a_second_alert():
    client = RecordingClient(make_config())
    queue_client = client
    now = [0.0]
    publisher = AlertPublisher(client=queue_client, queue=AlertQueue(Path("."), client),
                                cooldown_s=30.0)
    publisher._clock = lambda: now[0]

    publisher.publish(b"j", status=2, confidence=50, device_id="d", image_path="p")
    assert publisher.in_cooldown() is True

    now[0] = 31.0
    assert publisher.in_cooldown() is False


def test_cooldown_is_only_set_by_a_successful_publish(tmp_path):
    """A queued alert did not reach the phone, so it must not start the clock."""
    client = RecordingClient(make_config(), fail_upload=True)
    publisher = AlertPublisher(client=client, queue=AlertQueue(tmp_path, client),
                                cooldown_s=30.0)
    publisher.publish(b"j", status=2, confidence=50, device_id="d", image_path="p")
    assert publisher.in_cooldown() is False


def test_publish_requires_an_image_path(tmp_path):
    client = RecordingClient(make_config())
    publisher = AlertPublisher(client=client, queue=AlertQueue(tmp_path, client))
    with pytest.raises(Exception, match="image_path is required"):
        publisher.publish(b"j", status=2, confidence=50, device_id="d")


# -- snapshot path ---------------------------------------------------------


def test_snapshot_path_is_date_partitioned_per_device(tmp_path):
    client = RecordingClient(make_config())
    publisher = AlertPublisher(client=client, queue=AlertQueue(tmp_path, client))
    # 1760000000 is 2025-10-09T13:33:20Z, not 2026: the epoch value is fixed by
    # shield-app-prompt.md's own example payload.
    path = publisher.snapshot_path("3f2c", 1760000000)
    assert path == "shield-01/2025-10-09/3f2c.jpg"
    assert path.startswith(f"{client.config.device_prefix}/")


# -- transport classification ----------------------------------------------


def test_a_4xx_is_a_bug_and_a_5xx_is_transient():
    """The queue's retry budget must not be spent on requests that cannot work."""

    def call(code: int) -> Exception:
        import urllib.request

        client = SupabaseClient(make_config())

        def boom(*_args, **_kwargs):
            raise urllib.error.HTTPError("u", code, "msg", {}, None)  # type: ignore[arg-type]

        original = urllib.request.urlopen
        urllib.request.urlopen = boom  # type: ignore[assignment]
        try:
            client.upload_snapshot(b"j", "p.jpg")
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            urllib.request.urlopen = original  # type: ignore[assignment]
        raise AssertionError(f"HTTP {code} should have raised")

    # 503 is transient, so it must be retryable.
    assert isinstance(call(503), OfflineError)
    # 429 is rate limiting: also transient.
    assert isinstance(call(429), OfflineError)
    # 401 means the RPi token is wrong. Retrying cannot fix it.
    assert not isinstance(call(401), OfflineError)
    assert not isinstance(call(413), OfflineError)


def test_queue_file_is_readable_json(tmp_path):
    client = RecordingClient(make_config())
    queue = AlertQueue(tmp_path / "pending", client)
    path = queue.put(make_event(), b"jpeg")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["event"]["id"] == make_event().event_id
    assert base64.b64decode(payload["jpeg_b64"]) == b"jpeg"