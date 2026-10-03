"""The preview may detect cheaply, but a stored label may not.

HOG cost scales with pixel count, so at 1920x1080 the capture loop ran at 0.4 fps
and sampled the keyboard twice a second - slow enough to feel broken and fast
enough to swallow the keypress that saves a frame. The preview now detects
downscaled (D-032), which only moves the risk: a box found at a different
resolution must land in the same place, and the label committed on save must come
from the full-resolution pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from capture_dataset import detect_faces, usable_boxes  # noqa: E402

IMAGES = Path(__file__).resolve().parents[1] / "datasets" / "faces" / "images"
PHOTOS = sorted((IMAGES / "holdout").glob("*.jpg")) or sorted((IMAGES / "train").glob("*.jpg"))


def _photo():
    if not PHOTOS:
        pytest.skip("no captured face photos on disk")
    return PHOTOS[0]


# --- framing filter ------------------------------------------------------


def test_well_framed_box_is_kept():
    assert usable_boxes([(0, 0, 300, 400)], 60, 486) == [(0, 0, 300, 400)]


def test_box_below_the_lower_bound_is_dropped():
    assert usable_boxes([(0, 0, 30, 40)], 60, 486) == []


def test_box_above_the_upper_bound_is_dropped():
    """The 77% Brio crop: 661px short edge against a 486px limit."""
    assert usable_boxes([(0, 0, 661, 831)], 60, 486) == []


def test_upper_bound_uses_the_shorter_edge():
    """A tall narrow box is fine; a square one over the limit is not."""
    assert usable_boxes([(0, 0, 100, 400)], 60, 120) == [(0, 0, 100, 400)]
    assert usable_boxes([(0, 0, 130, 130)], 60, 120) == []


def test_both_edges_must_reach_the_lower_bound():
    assert usable_boxes([(0, 0, 200, 30)], 60, 486) == []


def test_filter_keeps_order_and_admits_mixed_qualities():
    boxes = [(0, 0, 300, 400), (0, 0, 20, 20), (0, 0, 661, 831)]
    assert usable_boxes(boxes, 60, 486) == [(0, 0, 300, 400)]


# --- resolution independence --------------------------------------------


def test_downscaled_boxes_land_in_full_resolution_coordinates():
    """A preview box must mean the same pixels regardless of detection width."""
    bgr = cv2.imread(str(_photo()))
    assert bgr is not None
    height, width = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    full = detect_faces(rgb, width, height, 0.25)
    if not full:
        pytest.skip("HOG found no face in the reference photo")
    fx, fy, fw, fh = full[0]

    cheap = detect_faces(rgb, width, height, 0.25, detect_width=640)
    if not cheap:
        pytest.skip("HOG found no face at preview width")
    cx, cy, cw, ch = cheap[0]

    tolerance = width / 20  # 5% of frame width
    assert abs(cx - fx) <= tolerance
    assert abs(cy - fy) <= tolerance
    assert abs(cw - fw) <= tolerance
    assert abs(ch - fh) <= tolerance


def test_native_detection_is_unchanged_by_the_new_argument():
    bgr = cv2.imread(str(_photo()))
    assert bgr is not None
    height, width = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert detect_faces(rgb, width, height, 0.25, 0) == detect_faces(
        rgb, width, height, 0.25
    )


def test_detect_width_larger_than_frame_falls_back_to_native():
    """A no-op width must not upscale and waste time."""
    bgr = cv2.imread(str(_photo()))
    assert bgr is not None
    height, width = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert detect_faces(rgb, width, height, 0.25, detect_width=width * 4) == (
        detect_faces(rgb, width, height, 0.25)
    )


def test_boxes_stay_inside_the_frame_when_downscaled():
    bgr = cv2.imread(str(_photo()))
    assert bgr is not None
    height, width = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for x, y, w, h in detect_faces(rgb, width, height, 0.25, detect_width=320):
        assert x >= 0 and y >= 0
        assert x + w <= width
        assert y + h <= height


def test_preview_detection_is_substantially_cheaper():
    """The whole point: cost scales with pixels, so a smaller frame must be faster."""
    import time

    frame = np.random.default_rng(0).integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
    detect_faces(frame, 1920, 1080, 0.25, detect_width=640)  # warm
    start = time.perf_counter()
    detect_faces(frame, 1920, 1080, 0.25, detect_width=640)
    cheap = time.perf_counter() - start

    start = time.perf_counter()
    detect_faces(frame, 1920, 1080, 0.25)
    full = time.perf_counter() - start

    assert cheap < full, "downscaled detection should not be slower than native"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
