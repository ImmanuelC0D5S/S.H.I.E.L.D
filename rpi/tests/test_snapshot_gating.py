"""A snapshot is published only when the face detector fired.

The request this encodes: *take the photo and send the alert with the photo, but
only send the photo if a face was detected.*

The distinction being defended is narrow and worth stating precisely, because the
naive reading of "only send the photo if a face is detected" would be to stop the
alert too, and that would invert the fail-safe the whole system is built on.
Absence of a face is not absence of an intruder -- it is absence of *evidence*,
and this system treats missing evidence as a threat (PROTOCOL.md 2.8, DECISIONS.md
D-041). So:

    PIR trip + face          -> status 2, snapshot uploaded, push carries image_url
    PIR trip + body, no face -> status 2, NO snapshot, push carries no image_url
    PIR trip + nobody        -> status 0, no event at all

The middle row is the one that had no test. `Service.run_pipeline` encoded a JPEG
for *every* status 2, so a person facing away shipped a photograph of the back of
their head, and `AlertPublisher.publish` then refused to send the alert at all
because it treated `image_path` as mandatory. Neither file had a test that ran
this path: the suite stubs `_start_alert_work` on the instance (which is why the
missing-`image_path` bug in that method survived 420 tests -- see HANDOFF),
and `run_pipeline` itself was only ever stubbed, never called.

`_encode_snapshot` is deliberately NOT stubbed here. It is the code that decides
whether a photo exists, and a test that replaced it would assert only that the
branch was taken, which is a weaker claim than the one that matters.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
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
from shield_rpi.pipeline import STATUS_ALERT, STATUS_CLEAR, Verdict
from shield_rpi.service import Service
from shield_rpi.verdict import Escalation, VerdictResult


def make_config(tmp_path) -> Config:
    return Config(
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


def make_verdict(*, face_detected: bool, status: int = STATUS_ALERT) -> Verdict:
    return Verdict(
        status=status,
        confidence=88 if face_detected else 40,
        identity=None,
        person_detected=True,
        face_detected=face_detected,
        snapshot_index=0,
        reason=None if face_detected else "low_conf",
        latency_ms=12.0,
        match_distance=None,
    )


class FakeCamera:
    """A real BGR array, so _encode_snapshot does genuine work."""

    def __init__(self) -> None:
        self._frame = np.full((480, 640, 3), 90, dtype=np.uint8)

    def burst(self) -> list:
        return [self._frame]


def build_service(tmp_path, monkeypatch, verdict: Verdict) -> Service:
    service = Service(make_config(tmp_path))
    monkeypatch.setattr(service.client, "is_armed", lambda: True)
    monkeypatch.setattr(service.publisher, "in_cooldown", lambda: False)
    monkeypatch.setattr(service, "_require_camera", lambda: FakeCamera())
    monkeypatch.setattr(service.pipeline, "classify", lambda _frames: verdict)
    monkeypatch.setattr(service, "record_event", lambda _record: None)
    return service


def test_a_detected_face_gets_a_snapshot(tmp_path, monkeypatch):
    """The normal path is untouched: a face means a photograph."""
    service = build_service(tmp_path, monkeypatch, make_verdict(face_detected=True))

    reply = service.run_pipeline({"tier": "high"})

    assert reply["status"] == STATUS_ALERT
    assert reply["face_detected"] is True
    assert reply["jpeg"], "a detected face must produce a snapshot"
    assert isinstance(reply["jpeg"], bytes)
    assert reply["jpeg"][:2] == b"\xff\xd8", "snapshot should be a JPEG"
    assert len(reply["jpeg"]) <= 524288, "PROTOCOL.md 3.3 caps a snapshot at 512 KB"


def test_a_body_with_no_usable_face_alarms_but_sends_no_photo(tmp_path, monkeypatch):
    """The behaviour this file exists for.

    status stays 2 -- the buzzer and the push both still happen -- but the JPEG is
    empty, so no snapshot is uploaded and no image_url reaches the phone.
    """
    service = build_service(tmp_path, monkeypatch, make_verdict(face_detected=False))

    reply = service.run_pipeline({"tier": "high"})

    assert reply["status"] == STATUS_ALERT, "absence of a face must not stand the alarm down"
    assert reply["person_detected"] is True
    assert reply["face_detected"] is False
    assert reply["jpeg"] == b"", "no face detected means no photograph is stored or sent"


def test_a_clear_verdict_never_encodes_a_snapshot(tmp_path, monkeypatch):
    service = build_service(tmp_path, monkeypatch, make_verdict(
        face_detected=False, status=STATUS_CLEAR
    ))

    reply = service.run_pipeline()

    assert reply["status"] == STATUS_CLEAR
    assert reply["jpeg"] == b""


@pytest.mark.parametrize("face_detected", [True, False])
def test_image_path_is_only_built_when_there_is_a_photo_to_put_there(
    tmp_path, monkeypatch, face_detected
):
    """The row and the stored object must agree.

    An image_path with nothing behind it yields a push whose signed URL 404s and
    the user sees a broken image instead of a clean alert, so publish() refuses
    that combination outright. The path is therefore derived from the presence of
    the JPEG, not from the status alone.

    This drives _start_alert_work rather than run_pipeline because that is the
    method that builds the path, and it is the method whose missing-image_path bug
    shipped once already (HANDOFF: the suite stubs this method on the instance, so
    420 tests never executed the service -> publisher wiring).
    """
    service = build_service(tmp_path, monkeypatch, make_verdict(face_detected=face_detected))
    captured: dict = {}

    def fake_publish(jpeg, **kwargs):
        captured["jpeg"] = jpeg
        captured.update(kwargs)
        return "event-1"

    monkeypatch.setattr(service.publisher, "publish", fake_publish)
    monkeypatch.setattr(
        service.verdicts,
        "watch",
        lambda *_a, **_k: VerdictResult(
            escalation=Escalation.STOOD_DOWN,
            verdict="known",
            waited_s=1.0,
            polls=1,
            timed_out=False,
        ),
    )

    reply = service.run_pipeline({"tier": "high"})
    service._start_alert_work(reply)
    # The worker nulls self._worker in its own finally, so poll for it to go
    # rather than joining a reference that may already be cleared.
    for _ in range(500):
        if service._worker is None:
            break
        time.sleep(0.01)
    assert service._worker is None, "alert worker did not finish"

    image_path = captured["image_path"]
    if face_detected:
        assert image_path.endswith(".jpg")
        assert image_path.startswith("shield-01/")
        assert captured["jpeg"], "a photo was promised to publish"
    else:
        assert image_path == "", "no photo means no object path"
        assert captured["jpeg"] == b"", "nothing to upload"
    assert captured["status"] == STATUS_ALERT, "the alert itself is unaffected"
    assert captured["face_detected"] is face_detected


@pytest.mark.parametrize("face_detected", [True, False])
def test_the_reply_reports_face_detected_verbatim(tmp_path, monkeypatch, face_detected):
    """The gate reads the pipeline's own flag rather than re-deriving it.

    If run_pipeline inferred "was there a face" some other way the two could
    disagree, which is the D-031/D-033 class of bug: a number that looks fine and
    measures the wrong thing.
    """
    service = build_service(tmp_path, monkeypatch, make_verdict(face_detected=face_detected))

    reply = service.run_pipeline()

    assert reply["face_detected"] is face_detected
    assert bool(reply["jpeg"]) is face_detected