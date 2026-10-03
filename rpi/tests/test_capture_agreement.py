"""The status line and the save must not be able to disagree.

The reported failure was a preview showing a green box and "ready - press S to
save" while pressing S incremented the "no face" counter and saved nothing
(D-033). Two independent decisions were being made - one for the HUD, one for the
save - and the second could overrule the first after the operator had already acted
on it.

These tests pin the properties that make that impossible: one assessment per frame,
consumed by both paths, on the frame that was displayed.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from capture_dataset import (  # noqa: E402
    DlibFaceDetector,
    FaceDetector,
    YoloFaceDetector,
    assess_frame,
    build_annotator,
    usable_boxes,
    write_rejected_frame,
)

FRAME_W, FRAME_H = 1920, 1080
MIN_FACE = 60
MAX_FRACTION = 0.45


class FixedDetector(FaceDetector):
    """Returns a fixed box, and counts how many times it was asked."""

    def __init__(self, boxes, description="fixed"):
        self._boxes = boxes
        self.description = description
        self.calls = 0

    def boxes_for(self, rgb, frame_w, frame_h):
        self.calls += 1
        return list(self._boxes)


def frame(luma=120):
    return np.full((FRAME_H, FRAME_W, 3), luma, dtype=np.uint8)


def ready_box():
    """A box comfortably inside both bounds: 300x400 on a 1080 short edge."""
    return (700, 300, 300, 400)


# --- the core agreement -------------------------------------------------


def test_ready_status_and_save_acceptance_agree():
    """The exact reported bug: HUD says ready, save must accept."""
    detector = FixedDetector([ready_box()])
    view = assess_frame(frame(), detector, MIN_FACE, MAX_FRACTION, 0.0)
    assert "ready" in view.reason
    assert view.accepted is True
    assert view.best is not None


def test_hud_reason_is_ready_exactly_when_save_would_accept():
    """`ready` in the status line iff the save path takes the frame."""
    cases = {
        "no boxes": [],
        "too small": [(700, 300, 30, 40)],
        "too large": [(600, 100, 661, 831)],
        "ready": [ready_box()],
    }
    for name, boxes in cases.items():
        view = assess_frame(frame(), FixedDetector(boxes), MIN_FACE, MAX_FRACTION, 0.0)
        says_ready = view.reason.startswith("ready")
        assert says_ready == view.accepted, f"{name}: {view.reason!r}"


def test_one_assessment_calls_the_detector_exactly_once():
    """Two passes over one frame is what let the save overrule the HUD."""
    detector = FixedDetector([ready_box()])
    assess_frame(frame(), detector, MIN_FACE, MAX_FRACTION, 0.0)
    assert detector.calls == 1


def test_repeated_assessment_of_one_frame_is_stable():
    """No state can drift between the moment it is shown and the moment it is saved."""
    detector = FixedDetector([ready_box()])
    source = frame()
    first = assess_frame(source, detector, MIN_FACE, MAX_FRACTION, 0.0)
    for _ in range(5):
        again = assess_frame(source, detector, MIN_FACE, MAX_FRACTION, 0.0)
        assert again.reason == first.reason
        assert again.best == first.best


# --- same object displayed and saved ------------------------------------


def test_assessment_carries_the_exact_frame_object_it_was_given():
    """Not a copy, not a re-read: the displayed pixels are the saved pixels."""
    source = frame(luma=77)
    view = assess_frame(source, FixedDetector([ready_box()]), MIN_FACE, MAX_FRACTION, 0.0)
    assert view.frame is source


def test_saved_frame_and_displayed_frame_are_the_same_bytes():
    source = frame(luma=201)
    view = assess_frame(source, FixedDetector([ready_box()]), MIN_FACE, MAX_FRACTION, 0.0)
    assert np.array_equal(view.frame, source)
    assert view.luma == 201


def test_luma_tracks_the_frame():
    assert assess_frame(frame(10), FixedDetector([]), MIN_FACE, MAX_FRACTION, 0.0).luma == 10
    assert assess_frame(frame(240), FixedDetector([]), MIN_FACE, MAX_FRACTION, 0.0).luma == 240


# --- the label box is the box that was shown ----------------------------


def test_label_box_is_the_drawn_box():
    detector = FixedDetector([(100, 100, 120, 130), ready_box()])
    view = assess_frame(frame(), detector, MIN_FACE, MAX_FRACTION, 0.0)
    assert view.best in view.usable
    assert view.best == max(view.usable, key=lambda b: b[2] * b[3])


def test_every_usable_box_is_drawn_not_just_the_best():
    boxes = [(100, 100, 120, 130), ready_box()]
    view = assess_frame(frame(), FixedDetector(boxes), MIN_FACE, MAX_FRACTION, 0.0)
    assert set(view.usable) == set(boxes)


# --- rejection evidence -------------------------------------------------


def test_rejected_frame_is_written_with_reason_box_and_luma(tmp_path):
    view = assess_frame(frame(luma=88), FixedDetector([]), MIN_FACE, MAX_FRACTION, 0.0)
    path = write_rejected_frame(view, tmp_path, 1)
    assert path is not None and path.is_file()
    name = path.name
    assert "no_face" in name
    assert "luma88" in name
    assert "nobox" in name


def test_rejected_frame_records_the_offending_box(tmp_path):
    view = assess_frame(frame(luma=200), FixedDetector([(600, 100, 661, 831)]), MIN_FACE, MAX_FRACTION, 0.0)
    assert view.accepted is False
    path = write_rejected_frame(view, tmp_path, 2)
    assert "too_large" in path.name
    assert "600x100_661x831" in path.name
    assert "luma200" in path.name


def test_rejected_frame_pixels_round_trip(tmp_path):
    source = frame(luma=123)
    view = assess_frame(source, FixedDetector([]), MIN_FACE, MAX_FRACTION, 0.0)
    path = write_rejected_frame(view, tmp_path, 1)
    assert np.array_equal(cv2.imread(str(path)), source)


def test_reject_directory_is_created_on_demand(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    view = assess_frame(frame(), FixedDetector([]), MIN_FACE, MAX_FRACTION, 0.0)
    assert write_rejected_frame(view, target, 1) is not None
    assert target.is_dir()


def test_reject_sequence_numbers_do_not_collide(tmp_path):
    names = set()
    for index in range(1, 4):
        view = assess_frame(frame(), FixedDetector([]), MIN_FACE, MAX_FRACTION, 0.0)
        names.add(write_rejected_frame(view, tmp_path, index).name)
    assert len(names) == 3


# --- filter consistency --------------------------------------------------


def test_assessment_usable_matches_the_shared_filter():
    boxes = [(0, 0, 30, 40), (700, 300, 300, 400), (600, 100, 661, 831)]
    view = assess_frame(frame(), FixedDetector(boxes), MIN_FACE, MAX_FRACTION, 0.0)
    limit = MAX_FRACTION * min(FRAME_W, FRAME_H)
    assert set(view.usable) == set(usable_boxes(boxes, MIN_FACE, limit))


# --- annotator selection --------------------------------------------------


class Args:
    def __init__(self, **kw):
        self.annotator = "dlib"
        self.detect_width = 0
        self.face_model = str(Path("rpi") / "models" / "face_pretrained.pt")
        self.imgsz = 640
        for key, value in kw.items():
            setattr(self, key, value)


def test_dlib_annotator_is_selectable_and_labelled_as_such():
    detector = build_annotator("dlib", Args())
    assert "dlib" in detector.description
    assert "HOG" in detector.description


def test_dlib_description_reports_native_when_no_downscale():
    assert "native" in build_annotator("dlib", Args()).description


def test_dlib_description_reports_the_downscale_width():
    assert "detect_width=320" in build_annotator("dlib", Args(detect_width=320)).description


def test_yolo_annotator_names_the_model_and_input_size():
    detector = build_annotator("yolo", Args(annotator="yolo"))
    assert "face_pretrained.pt" in detector.description
    assert "imgsz=640" in detector.description


def test_yolo_annotator_reports_the_requested_input_size():
    detector = build_annotator("yolo", Args(annotator="yolo", imgsz=960))
    assert "imgsz=960" in detector.description


def test_yolo_is_not_dlib():
    """Requirement: dlib must not be silently in the save path."""
    assert "dlib" not in build_annotator("yolo", Args(annotator="yolo")).description


def test_missing_model_fails_loudly_rather_than_falling_back_to_dlib():
    with pytest.raises(SystemExit):
        build_annotator("yolo", Args(annotator="yolo", face_model="no/such/model.pt"))


# --- colour space --------------------------------------------------------


def test_yolo_receives_bgr_not_rgb():
    """A swapped channel does not raise - it returns a worse box.

    Feeding ultralytics RGB where it expects BGR still yields detections, so the
    mistake is invisible in the output and only shows up as oddly shaped boxes
    (D-033). Pinned against a direct BGR call on a real photo.
    """
    photos = sorted(
        (Path(__file__).resolve().parents[1] / "datasets" / "faces" / "images" / "holdout").glob(
            "*.jpg"
        )
    )
    if not photos:
        pytest.skip("no captured photos on disk")
    frame_bgr = cv2.imread(str(photos[0]))
    if frame_bgr is None:
        pytest.skip("photo unreadable")
    height, width = frame_bgr.shape[:2]

    detector = build_annotator("yolo", Args(annotator="yolo"))
    assert detector.boxes_for(frame_bgr, width, height) == YoloFaceDetector(
        detector._model, 640, "x"
    ).boxes_for(frame_bgr, width, height)

    from ultralytics import YOLO

    direct = YOLO("rpi/models/face_pretrained.pt").predict(
        frame_bgr, imgsz=640, verbose=False
    )[0]
    expected = [
        (
            max(0, min(int(round(x1)), width)),
            max(0, min(int(round(y1)), height)),
            max(0, min(int(round(x2)), width)) - max(0, min(int(round(x1)), width)),
            max(0, min(int(round(y2)), height)) - max(0, min(int(round(y1)), height)),
        )
        for x1, y1, x2, y2 in direct.boxes.xyxy.cpu().tolist()
    ]
    assert detector.boxes_for(frame_bgr, width, height) == expected


def test_yolo_boxes_are_not_transposed():
    """xyxy is (x1, y1, x2, y2); any other unpack still yields four plausible
    numbers, so a wrong order produces a garbled rectangle instead of an error
    (D-033). A real face box is taller than it is wide, not wider than tall."""
    photos = sorted(
        (Path(__file__).resolve().parents[1] / "datasets" / "faces" / "images" / "holdout").glob(
            "*.jpg"
        )
    )
    if not photos:
        pytest.skip("no captured photos on disk")
    frame_bgr = cv2.imread(str(photos[0]))
    if frame_bgr is None:
        pytest.skip("photo unreadable")
    height, width = frame_bgr.shape[:2]

    detector = build_annotator("yolo", Args(annotator="yolo"))
    boxes = detector.boxes_for(frame_bgr, width, height)
    if not boxes:
        pytest.skip("no face in the reference photo")
    biggest = max(boxes, key=lambda b: b[2] * b[3])
    assert biggest[3] >= biggest[2], f"face box should be portrait, got {biggest}"


def test_dlib_receives_rgb_not_bgr():
    """The mirror image: dlib needs RGB, so it must convert before calling."""
    assert "BGR2RGB" in inspect.getsource(DlibFaceDetector.boxes_for)
    assert "BGR2RGB" not in inspect.getsource(YoloFaceDetector.boxes_for)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
