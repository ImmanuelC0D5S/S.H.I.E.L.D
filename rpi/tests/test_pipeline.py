"""Pipeline status-mapping tests. ``docs/PROTOCOL.md`` sections 2.4.3 and 4.1.

The status mapping is the safety-critical logic in the whole system, and the
directions it can fail in are not symmetric. A stranger that reads as ``CLEAR``
is silent. A houseguest that reads as ``ALERT`` is merely annoying. So these
tests are written from the mapping table outwards, and every row of that table
gets a case - including the two that share ``status: 2``.
"""

from __future__ import annotations

import numpy as np
import pytest
from shield_rpi.config import (
    AlertConfig,
    CameraConfig,
    Config,
    DetectorConfig,
    RecognizerConfig,
    SupabaseConfig,
    VerdictConfig,
)
from shield_rpi.detector import Detection, DetectionResult
from shield_rpi.pipeline import (
    SOURCE_LOW_CONF,
    SOURCE_UNKNOWN,
    STATUS_ALERT,
    STATUS_CLEAR,
    STATUS_KNOWN,
    Pipeline,
    annotate_snapshot,
)
from shield_rpi.recognizer import UNKNOWN

FRAME = 64


def make_config(match_distance: float = 0.5, alert_confidence: int = 50) -> Config:
    return Config(
        camera=CameraConfig(device=0, width=FRAME, height=FRAME),
        detector=DetectorConfig(face_model="m.pt", person_model="p.pt", imgsz=640, conf=0.5),
        recognizer=RecognizerConfig(
            faces_dir=".", match_distance=match_distance, alert_confidence=alert_confidence
        ),
        link=None,  # type: ignore[arg-type]  - unused by the pipeline
        supabase=SupabaseConfig(
            url="https://shield.example", key="k", bucket="snapshots", device_prefix="shield-01"
        ),
        alert=AlertConfig(),
        verdict=VerdictConfig(),
    )


class StubDetector:
    """Returns scripted detections per frame index."""

    def __init__(self, faces, people) -> None:
        self._faces = faces
        self._people = people
        self.calls: list[str] = []

    def detect_faces(self, frame) -> DetectionResult:
        index = self.calls.count("face")
        self.calls.append("face")
        return DetectionResult(list(self._faces(index)), 1.0)

    def detect_person(self, frame) -> DetectionResult:
        index = self.calls.count("person")
        self.calls.append("person")
        return DetectionResult(list(self._people(index)), 1.0)


class StubRecogniser:
    def __init__(self, name: str = UNKNOWN, distance: float = 0.6) -> None:
        self.name = name
        self.distance = distance

    def match_rgb(self, crop) -> tuple[str, float]:
        return self.name, self.distance


def frames(count: int = 2) -> list[np.ndarray]:
    return [np.full((FRAME, FRAME, 3), 90, dtype=np.uint8) for _ in range(count)]


def face(confidence: float, box=(10, 10, 20, 20)) -> Detection:
    return Detection(box=box, confidence=confidence)


def person(confidence: float = 0.9, box=(0, 0, 40, 40)) -> Detection:
    return Detection(box=box, confidence=confidence)


def build(detector: StubDetector, recogniser: StubRecogniser | None = None, **kwargs):
    return Pipeline(make_config(**kwargs), detector, recogniser or StubRecogniser())


# -- 4.1 step 5: nothing there -> CLEAR -------------------------------------


def test_no_person_and_no_face_is_clear():
    pipeline = build(StubDetector(lambda i: [], lambda i: []))
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_CLEAR
    assert verdict.confidence == 0
    assert verdict.identity is None
    assert verdict.reason is None
    assert verdict.needs_verdict is False


# -- 4.1 step 7: matched identity -> KNOWN ----------------------------------


def test_a_matched_face_is_known_and_does_not_alert():
    pipeline = build(
        StubDetector(lambda i: [face(0.9)], lambda i: [person()]),
        StubRecogniser("imman", 0.12),
    )
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_KNOWN
    assert verdict.identity == "imman"
    assert verdict.confidence == 88, "(1 - 0.12) * 100"
    assert verdict.needs_verdict is False


def test_a_match_below_the_threshold_reads_as_known():
    pipeline = build(
        StubDetector(lambda i: [face(0.9)], lambda i: [person()]),
        StubRecogniser("imman", 0.49),
    )
    assert pipeline.classify(frames()).status == STATUS_KNOWN


# -- 4.1 step 8: unmatched face, confident -> ALERT/unknown -----------------


def test_an_unmatched_confident_face_is_alert_unknown():
    pipeline = build(
        StubDetector(lambda i: [face(0.92)], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.13),
    )
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_ALERT
    assert verdict.reason == SOURCE_UNKNOWN
    assert verdict.confidence == 87, "the prompt's own example is 87"
    assert verdict.needs_verdict is True


def test_a_face_just_past_the_threshold_is_still_an_alert():
    """0.51 is on the unknown side. This is the case that must not be softened."""
    pipeline = build(
        StubDetector(lambda i: [face(0.9)], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.51),
        match_distance=0.5,
    )
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_ALERT
    assert verdict.reason == SOURCE_UNKNOWN
    assert verdict.confidence == 49


# -- 4.1 step 9: person but no usable face -> ALERT/low_conf ----------------


def test_person_with_no_face_is_alert_low_conf():
    pipeline = build(StubDetector(lambda i: [], lambda i: [person(0.8)]))
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_ALERT
    assert verdict.reason == SOURCE_LOW_CONF
    assert verdict.person_detected is True
    assert verdict.face_detected is False
    assert verdict.confidence == 80
    assert verdict.needs_verdict is True


def test_detector_confidence_below_the_band_is_low_conf_not_unknown():
    """PROTOCOL.md 2.4.3: a face the detector is unsure about asks the user
    "recognise this?" rather than asserting an unfamiliar face."""
    pipeline = build(
        StubDetector(lambda i: [face(0.40)], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.55),
        alert_confidence=50,
    )
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_ALERT
    assert verdict.reason == SOURCE_LOW_CONF
    assert verdict.confidence == 40


def test_a_degenerate_face_box_is_low_conf_not_a_crash():
    pipeline = build(
        StubDetector(lambda i: [face(0.9, box=(0, 0, 0, 0))], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.4),
    )
    verdict = pipeline.classify(frames())
    assert verdict.status == STATUS_ALERT
    assert verdict.reason == SOURCE_LOW_CONF


# -- burst behaviour --------------------------------------------------------


def test_the_best_face_in_the_burst_wins():
    """A person mid-turn can be undetectable in frame 1 and clear in frame 4."""
    confidences = iter([0.30, 0.95, 0.60])
    pipeline = build(
        StubDetector(lambda i: [face(next(confidences))], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.2),
    )
    verdict = pipeline.classify(frames(3))
    assert verdict.snapshot_index == 1
    assert verdict.status == STATUS_ALERT


def test_every_frame_in_the_burst_is_examined():
    detector = StubDetector(lambda i: [], lambda i: [])
    build(detector).classify(frames(5))
    assert detector.calls.count("face") == 5


def test_the_snapshot_frame_is_the_one_with_the_largest_person():
    """For a low_conf event the snapshot is the only evidence the user gets, so
    it should be the frame where the intruder fills the most of the frame.

    The stub is called once while scanning the burst and once more per frame in
    ``_best_person_frame``, so the scripted boxes are indexed by total call
    count, not by frame.
    """
    boxes = [(0, 0, 10, 10), (0, 0, 10, 10), (0, 0, 30, 30), (0, 0, 12, 12)]
    pipeline = build(StubDetector(lambda i: [], lambda i: [person(0.9, box=boxes[i])]))
    assert pipeline.classify(frames(3)).snapshot_index == 1


def test_the_person_detector_is_not_run_twice_on_the_same_frame():
    """PROTOCOL.md 4.3 budgets 1 s and the Pi's 1080p figure is unmeasured, so a
    redundant inference pass is a real cost, not a style issue."""
    detector = StubDetector(lambda i: [], lambda i: [person(0.9)])
    build(detector).classify(frames(3))
    # One pass over the burst plus one scan to pick the snapshot frame.
    assert detector.calls.count("person") == 1 + 3


def test_classify_needs_at_least_one_frame():
    with pytest.raises(ValueError, match="at least one frame"):
        build(StubDetector(lambda i: [], lambda i: [])).classify([])


# -- reported confidence ----------------------------------------------------


def test_confidence_is_clamped_to_0_100():
    assert Pipeline(make_config(), StubDetector(lambda i: [], lambda i: []), StubRecogniser())
    # distance -0.2 would give 120 without clamping.
    pipeline = build(
        StubDetector(lambda i: [face(0.9)], lambda i: [person()]),
        StubRecogniser("imman", -0.2),
    )
    assert pipeline.classify(frames()).confidence == 100


def test_a_near_miss_keeps_its_distance_for_the_event_log():
    """A failing gate shows up as a run of near-misses at 0.45-0.49. Collapsing
    them all to "unknown" would destroy that evidence (DECISIONS.md D-030)."""
    pipeline = build(
        StubDetector(lambda i: [face(0.9)], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.47),
    )
    verdict = pipeline.classify(frames())
    assert verdict.match_distance == pytest.approx(0.47)
    assert verdict.status == STATUS_ALERT


# -- snapshot annotation ----------------------------------------------------


def test_the_snapshot_is_labelled_with_the_verdict():
    verdict = build(
        StubDetector(lambda i: [face(0.9)], lambda i: [person()]),
        StubRecogniser(UNKNOWN, 0.13),
    ).classify(frames())
    annotated = annotate_snapshot(np.full((480, 640, 3), 40, dtype=np.uint8), verdict)
    assert annotated.shape == (480, 640, 3)
    assert not np.array_equal(annotated, np.full((480, 640, 3), 40, dtype=np.uint8)), (
        "an unlabelled snapshot is indistinguishable from any other frame later"
    )


def test_the_input_frame_is_not_mutated_by_annotation():
    frame = np.full((240, 320, 3), 40, dtype=np.uint8)
    original = frame.copy()
    verdict = build(StubDetector(lambda i: [], lambda i: [])).classify(frames(1))
    annotate_snapshot(frame, verdict)
    assert np.array_equal(frame, original)