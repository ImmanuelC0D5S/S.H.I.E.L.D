"""Tests for the capture-tool geometry helpers.

``capture_dataset`` imports cv2 and face_recognition, both heavy. The padding and
status logic is pure arithmetic, so these tests import the module directly and
exercise only the parts that do not need a camera, a model or a face. That keeps
the rules which govern label quality under test at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from capture_dataset import pad_box, reason_no_face  # noqa: E402

FRAME_W = 1280
FRAME_H = 720


# -- padding grows the box symmetrically -----------------------------------


def test_pad_grows_equally_on_all_sides():
    """A whole-face box must not be biased toward one corner."""
    x, y, w, h = pad_box((300, 200, 120, 120), FRAME_W, FRAME_H, 0.25)
    assert 300 - x == (x + w) - 420
    assert 200 - y == (y + h) - 320


def test_pad_is_proportional_to_box_size():
    """Padding is a fraction of the box, so small faces get small margins.

    Boxes are centred away from the frame edges; a box at the origin would be
    clipped and could not show the true proportional growth.
    """
    small = pad_box((200, 200, 100, 100), 1000, 1000, 0.25)
    large = pad_box((300, 300, 400, 400), 1000, 1000, 0.25)
    assert small[2] == 150  # 100 + 2 * 25
    assert large[2] == 600  # 400 + 2 * 100


def test_pad_zero_is_identity():
    assert pad_box((300, 200, 120, 120), FRAME_W, FRAME_H, 0.0) == (300, 200, 120, 120)


# -- padding must stay on the image ----------------------------------------


def test_pad_clips_at_top_left():
    assert pad_box((0, 0, 100, 100), FRAME_W, FRAME_H, 0.25) == (0, 0, 125, 125)


def test_pad_clips_at_bottom_right():
    x, y, w, h = pad_box((1180, 620, 100, 100), FRAME_W, FRAME_H, 0.25)
    assert x + w <= FRAME_W
    assert y + h <= FRAME_H


@pytest.mark.parametrize(
    "box",
    [(0, 0, 100, 100), (1180, 620, 100, 100), (0, 620, 100, 100), (1180, 0, 100, 100)],
)
def test_padded_box_never_exceeds_frame(box):
    """Normalised YOLO labels would go out of range if a box left the frame."""
    x, y, w, h = pad_box(box, FRAME_W, FRAME_H, 0.25)
    assert x >= 0 and y >= 0
    assert x + w <= FRAME_W
    assert y + h <= FRAME_H
    assert w >= 0 and h >= 0


def test_pad_on_a_face_at_the_edge_still_covers_some_face():
    """A partially-visible face must keep a usable box rather than collapsing."""
    x, y, w, h = pad_box((0, 0, 80, 80), FRAME_W, FRAME_H, 0.5)
    assert w > 80 or x > 0
    assert w > 0 and h > 0


# -- why a frame was skipped must be legible --------------------------------


def test_no_face_is_reported_when_nothing_detected():
    status, _ = reason_no_face([], 60)
    assert "no face" in status.lower()


def test_small_face_is_reported_separately():
    """A frozen counter has to read as 'move closer', not as a mystery."""
    status, _ = reason_no_face([(10, 10, 30, 30)], 60)
    assert "too small" in status.lower()
    assert "30" in status
    assert "60" in status


def test_large_face_is_not_called_small():
    status, _ = reason_no_face([(100, 100, 400, 400)], 60)
    assert "too small" not in status.lower()
    assert "no face" not in status.lower()


def test_best_face_drives_the_reported_size():
    """Several small faces present: report the one nearest acceptance."""
    status, _ = reason_no_face([(0, 0, 40, 40), (0, 0, 70, 90)], 100)
    assert "70" in status


def test_reported_edge_is_the_shorter_one():
    """A long thin box is rejected on its width; that is what must be reported."""
    status, _ = reason_no_face([(0, 0, 40, 300)], 100)
    assert "40" in status