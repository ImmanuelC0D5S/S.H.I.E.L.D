"""Tests for the pretrained-detector labelling and recognition-crop path (D-026).

Host-only: no cv2 model loading beyond small synthetic images, no hardware. These
pin the two things that silently corrupt a dataset or a gate result - the label
field format and the recognition crop geometry - because both already failed once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

from bootstrap_label import (  # noqa: E402
    MAX_ASPECT,
    MIN_ASPECT,
    geometric_problems,
    write_label,
)
from dataset_meta import FACE_CLASS_ID  # noqa: E402
from recogniser import RECOGNITION_PAD, crop_rgb, pad_box  # noqa: E402


def _frame(w: int = 1280, h: int = 720) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


# -- recognition padding ---------------------------------------------------


def test_recognition_pad_is_a_quarter_of_the_box():
    """25% growth on each side is the specified recognition margin (D-026 step 5).

    A 100x100 box must become 150x150: 100 + 25 on each edge. Asserting the exact
    figure catches both an accidental change to the ratio and a "percent of total"
    reading that would give 125 instead of 150.
    """
    assert RECOGNITION_PAD == 0.25
    x, y, w, h = pad_box((100, 100, 100, 100), 1280, 720, RECOGNITION_PAD)
    assert (x, y, w, h) == (75, 75, 150, 150)


def test_recognition_pad_clips_to_the_frame():
    """A face at the frame edge pads inward, never outside the image.

    An unclipped pad would produce a crop the slicer silently truncates, so the
    embedding would come from a different region than the padding intended - the
    exact failure padding exists to prevent.
    """
    x, y, w, h = pad_box((0, 0, 100, 100), 1280, 720, RECOGNITION_PAD)
    assert x == 0 and y == 0
    assert x + w <= 1280 and y + h <= 720
    assert w == 125 and h == 125  # only the inward half applies

    x, y, w, h = pad_box((1180, 620, 100, 100), 1280, 720, RECOGNITION_PAD)
    assert x + w == 1280 and y + h == 720


def test_crop_rgb_returns_rgb_and_padded():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[100:200, 100:200] = (10, 20, 30)  # BGR marker
    crop = crop_rgb(frame, (100, 100, 100, 100))
    assert crop is not None
    # 150x150 after padding: 25 rows/cols of padding on each side.
    assert crop.shape[:2] == (150, 150)
    # Centre pixel is the marker; OpenCV's BGR2RGB means channel 0 is red (30).
    assert int(crop[75, 75, 0]) == 30


def test_crop_rgb_returns_none_for_a_degenerate_box():
    frame = _frame()
    assert crop_rgb(frame, (100, 100, 0, 0)) is None
    assert crop_rgb(frame, (-50, -50, 10, 10)) is None


def test_gate_and_live_share_one_crop_function():
    """Both consumers must call the same padded crop, or the gate lies.

    The gate runs `holdout_test.score_images` and the live feed runs
    `live_test.main`; if either stopped using `crop_rgb`, the gate would no longer
    describe live behaviour. This is a structural check, not a runtime one.
    """
    gate = (TOOLS / "holdout_test.py").read_text(encoding="utf-8")
    live = (TOOLS / "live_test.py").read_text(encoding="utf-8")
    assert "crop_rgb(" in gate, "holdout_test no longer crops via crop_rgb"
    assert "crop_rgb(" in live, "live_test no longer crops via crop_rgb"
    # Neither may build its own crop, bypassing the shared padding.
    assert "face_encodings(" not in gate, "gate re-embeds without going through crop_rgb"
    assert "face_encodings(" not in live, "live feed re-embeds without going through crop_rgb"


def test_enrolment_embeds_the_padded_crop():
    """Enrolment must embed the same padded region inference will embed.

    If enrolment embeds the whole frame while inference embeds a padded crop, the
    two embeddings describe different regions and the enrolled threshold is
    meaningless.
    """
    source = (TOOLS / "enroll_laptop.py").read_text(encoding="utf-8")
    assert "crop_rgb(" in source, "enrolment no longer crops via crop_rgb"
    assert "face_encodings(" in source, "enrolment no longer embeds"


# -- bootstrap labelling ---------------------------------------------------


def test_bootstrap_label_writes_five_fields(tmp_path):
    path = tmp_path / "frame.txt"
    write_label(path, (100, 50, 200, 300), 1280, 720)
    parts = path.read_text(encoding="utf-8").split()
    assert len(parts) == 5
    assert float(parts[0]) == FACE_CLASS_ID
    _, cx, cy, w, h = (float(p) for p in parts)
    # centre = (x + w/2, y + h/2) = (200, 200) in pixels
    assert cx == pytest.approx(200 / 1280, abs=1e-5)
    assert cy == pytest.approx(200 / 720, abs=1e-5)
    assert w == pytest.approx(200 / 1280, abs=1e-5)
    assert h == pytest.approx(300 / 720, abs=1e-5)


def test_bootstrap_label_stays_in_unit_range_at_the_corner(tmp_path):
    path = tmp_path / "frame.txt"
    write_label(path, (0, 0, 80, 80), 1280, 720)
    _, cx, cy, w, h = (float(p) for p in path.read_text(encoding="utf-8").split())
    assert 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0
    assert 0.0 < w <= 1.0 and 0.0 < h <= 1.0


@pytest.mark.parametrize(
    "box, expect_problem",
    [
        ((100, 100, 200, 200), False),          # normal face, aspect 1.0
        ((10, 10, 4, 4), True),                  # 4px: too small
        ((100, 100, 600, 200), True),            # aspect 3.0: not a face
        ((100, 100, 40, 200), True),             # aspect 0.2: too narrow
        ((1300, 700, 200, 200), True),           # runs off the right edge
        ((10, 10, 0, 0), True),                  # empty
    ],
)
def test_geometric_problems_flags_implausible_labels(box, expect_problem):
    assert bool(geometric_problems(box, 1280, 720)) is expect_problem


def test_geometric_problems_accepts_a_typical_face():
    assert geometric_problems((705, 331, 147, 198), 1280, 720) == []


def test_aspect_bounds_bracket_a_real_face():
    """Sanity on the constants themselves: a real face box must pass them."""
    assert MIN_ASPECT < 0.76 < MAX_ASPECT
