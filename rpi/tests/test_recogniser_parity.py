"""Parity between the live recogniser and the Phase 1 gate.

``shield_rpi/recognizer.py`` and ``tools/recogniser.py`` hold two copies of the
same cropping logic. That duplication is deliberate and temporary - see the
module docstring of ``shield_rpi/recognizer.py`` and DECISIONS.md D-010 - and
this file is the thing that makes it safe.

The Phase 1 gate (``tools/holdout_test.py``) is what decides whether the detector
and the enrolment are good enough. It is currently failing at 55% identification
accuracy with a diagnosed cross-camera enrolment mismatch (D-030). If the live
feed cropped differently from the gate, the gate would stop describing what the
camera actually sees, and the failure would look like a model problem instead of
the cropping problem it is. That is the exact trap HANDOFF.md warns about.

So the two implementations are pinned together here, over geometries chosen to
hit every branch of ``pad_box``: interior boxes, boxes flush to each edge, boxes
entirely outside the frame, and degenerate ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recogniser import RECOGNITION_PAD as GATE_PAD  # noqa: E402
from recogniser import crop_rgb as gate_crop_rgb  # noqa: E402
from recogniser import pad_box as gate_pad_box  # noqa: E402
from shield_rpi.recognizer import RECOGNITION_PAD as LIVE_PAD  # noqa: E402
from shield_rpi.recognizer import crop_rgb as live_crop_rgb  # noqa: E402
from shield_rpi.recognizer import pad_box as live_pad_box  # noqa: E402

FRAME_W, FRAME_H = 1920, 1080

#: Boxes chosen to hit every branch of pad_box and crop_rgb.
CASES = [
    ("interior", (400, 300, 200, 200)),
    ("flush left", (0, 300, 200, 200)),
    ("flush top", (400, 0, 200, 200)),
    ("flush right", (FRAME_W - 200, 300, 200, 200)),
    ("flush bottom", (400, FRAME_H - 200, 200, 200)),
    ("corner", (0, 0, 200, 200)),
    ("all edges", (0, 0, FRAME_W, FRAME_H)),
    ("crossing right edge", (FRAME_W - 50, 300, 200, 200)),
    ("crossing bottom edge", (400, FRAME_H - 50, 200, 200)),
    ("negative origin", (-30, -30, 200, 200)),
    ("fully outside top-left", (-500, -500, 100, 100)),
    ("fully outside bottom-right", (FRAME_W + 100, FRAME_H + 100, 100, 100)),
    ("one pixel", (10, 10, 1, 1)),
    ("zero width", (10, 10, 0, 200)),
    ("zero height", (10, 10, 200, 0)),
    ("tall and thin", (900, 100, 40, 900)),
    ("wide and flat", (100, 500, 900, 40)),
    ("negative size", (100, 100, -50, -50)),
]


def test_the_padding_ratio_is_the_same():
    """D-020/D-026 step 5. Changing one and not the other is a silent accuracy
    change on the live feed only."""
    assert LIVE_PAD == GATE_PAD


@pytest.mark.parametrize(("name", "box"), CASES, ids=[case[0] for case in CASES])
def test_pad_box_agrees(name, box):
    assert live_pad_box(box, FRAME_W, FRAME_H, LIVE_PAD) == gate_pad_box(
        box, FRAME_W, FRAME_H, GATE_PAD
    )


def test_the_cases_actually_exercise_different_branches():
    """Guards the table above: if every case collapsed to the same output the
    parity tests would pass while proving nothing."""
    outputs = {live_pad_box(box, FRAME_W, FRAME_H, LIVE_PAD) for _, box in CASES}
    assert len(outputs) >= 8, f"only {len(outputs)} distinct outcomes; widen CASES"


def test_the_box_stays_inside_the_frame():
    """An out-of-frame box produces an empty crop instead of a detection."""
    for name, box in CASES:
        x, y, w, h = live_pad_box(box, FRAME_W, FRAME_H, LIVE_PAD)
        assert x >= 0 and y >= 0, name
        assert x + w <= FRAME_W, name
        assert y + h <= FRAME_H, name


@pytest.mark.parametrize(("name", "box"), CASES, ids=[case[0] for case in CASES])
def test_crop_rgb_agrees(name, box):
    frame = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    live = live_crop_rgb(frame, box)
    gate = gate_crop_rgb(frame, box)
    if live is None or gate is None:
        assert live is gate, f"{name}: one returned None and the other did not"
        return
    assert live.shape == gate.shape, name
    assert np.array_equal(live, gate), name


def test_a_degenerate_box_yields_none_rather_than_raising():
    """A bad frame must not take down the trigger."""
    frame = np.zeros((64, 64, 3), dtype=np.uint8)
    assert live_crop_rgb(frame, (10, 10, 0, 0)) is None
    assert live_crop_rgb(frame, (-500, -500, 0, 0)) is None


def test_crop_is_rgb_and_three_channel():
    """face_recognition takes RGB. Handing it BGR silently degrades every match."""
    bgr = np.zeros((120, 160, 3), dtype=np.uint8)
    bgr[..., 0] = 255  # blue channel only
    crop = live_crop_rgb(bgr, (20, 20, 120, 120))
    assert crop is not None
    assert crop[0, 0, 2] == 255, "the blue channel must survive at index 2 (RGB)"


def test_padding_actually_widens_the_crop():
    """Padding is the whole point of RECOGNISION_PAD: a tight box omits the
    forehead, jawline and hairline that distinguish one person from another."""
    frame = np.zeros((400, 400, 3), dtype=np.uint8)
    box = (100, 100, 200, 200)
    tight = live_crop_rgb(frame, box, pad_ratio=0.0)
    padded = live_crop_rgb(frame, box, pad_ratio=LIVE_PAD)
    assert tight is not None and padded is not None
    assert padded.shape[1] > tight.shape[1]
    assert padded.shape[0] > tight.shape[0]