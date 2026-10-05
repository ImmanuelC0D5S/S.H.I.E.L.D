"""Detector tests: when the models load, and what happens when they cannot.

The distinction this file pins is timing, not inference. Inference quality is
measured by the holdout gate, which lives elsewhere; what breaks silently here
is *when* a weight file is read. Loading on the first frame puts ~5 s inside
PROTOCOL.md 4.3's 1 s reply budget and past the node's 5 s reply timeout (2.9),
and a missing file then surfaces as a failed trigger instead of a failed
start-up.
"""

from __future__ import annotations

import pytest
from shield_rpi.config import DetectorConfig
from shield_rpi.detector import Detector, DetectorError


def _config() -> DetectorConfig:
    return DetectorConfig(face_model="face.pt", person_model="person.pt", imgsz=640, conf=0.5)


def test_warm_loads_both_models_before_anything_can_ask_for_a_verdict(monkeypatch):
    calls: list[str] = []

    def fake_load(self, which, path):  # noqa: ARG001 - paths are the caller's problem
        calls.append(which)
        return f"model:{which}"

    monkeypatch.setattr(Detector, "_load", fake_load)
    detector = Detector(_config())

    detector.warm()

    assert calls == ["face", "person"], "warm must load both, not whichever is asked for first"
    # And the properties must serve what was warmed rather than loading again.
    assert detector.face == "model:face"
    assert detector.person == "model:person"
    assert calls == ["face", "person"]


def test_warm_fails_loudly_when_a_weight_file_is_missing(tmp_path):
    """A missing model must stop start-up, not the first trigger."""
    detector = Detector(
        DetectorConfig(
            face_model=str(tmp_path / "never-downloaded.pt"),
            person_model=str(tmp_path / "yolov8n.pt"),
            imgsz=640,
            conf=0.5,
        )
    )
    with pytest.raises(DetectorError, match="face model not found"):
        detector.warm()
    assert detector._face is None, "a failed warm must not leave a half-loaded detector"
    assert detector._person is None


def test_a_fresh_detector_has_loaded_nothing():
    """Nothing is imported or read until warm() or the first verdict asks."""
    detector = Detector(_config())
    assert detector._face is None
    assert detector._person is None
