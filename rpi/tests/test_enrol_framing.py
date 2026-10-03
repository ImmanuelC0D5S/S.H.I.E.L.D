"""The enrolment framing gate: a face may be too large, and that breaks the crop.

Regression cover for D-029. The failure this guards is silent and was measured on the
Brio 100: with the face filling the frame, ``pad_box`` clips the 25% margin away and
``crop_rgb`` returns the whole frame, so the enrolled embedding comes from a differently
shaped region than the gate's crop ever will - the exact regression D-026 step 5 fixed
once already.
"""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

import numpy as np  # noqa: E402
from enroll_laptop import (  # noqa: E402
    CONDITION_PROMPTS,
    DEFAULT_MIN_FACE,
    MAX_FACE_FRACTION,
    condition_prompt,
    draw_condition_prompt,
    framing_problem,
)
from recogniser import crop_rgb  # noqa: E402


def _problem(box, w=1920, h=1080, min_face=DEFAULT_MIN_FACE, max_fraction=MAX_FACE_FRACTION):
    return framing_problem(box, w, h, min_face, max_fraction)


def test_measured_brio_framing_is_rejected():
    """The real Brio boxes measured on hardware must be refused.

    (636, 20, 661, 831) is the photo that was actually enrolled from; (15, 11, 994, 1058)
    is a live frame at the same desk. Both are 1080p, both too tight.
    """
    assert _problem((636, 20, 661, 831)) is not None
    assert _problem((15, 11, 994, 1058)) is not None


def test_measured_builtin_framing_is_accepted():
    """The built-in camera's five good photos must still be accepted.

    These are the real boxes from the 2026-10-02 720p enrolment photos, scaled into
    1080p terms. Rejecting these would mean the gate is too tight to be usable.
    """
    for box in [
        (472, 121, 259, 332),
        (631, 222, 172, 214),
        (533, 247, 177, 237),
        (530, 247, 176, 236),
        (540, 245, 180, 243),
    ]:
        # 720p -> 1080p is a uniform 1.5x on both axes.
        scaled = tuple(int(v * 1.5) for v in box)
        assert _problem(scaled) is None, f"good framing wrongly rejected: {box} -> {scaled}"


def test_too_small_is_reported_as_move_closer():
    assert "move closer" in _problem((100, 100, 30, 30))


def test_too_large_is_reported_as_move_back():
    problem = _problem((15, 11, 994, 1058))
    assert problem is not None
    assert "move back" in problem


def test_bound_is_relative_to_the_short_edge():
    """The same face must be judged identically at 720p and 1080p."""
    face = (0, 0, 700, 700)  # 700px short edge
    for w, h in [(1280, 720), (1920, 1080), (3840, 2160)]:
        limit = MAX_FACE_FRACTION * min(w, h)
        expected = None if limit >= 700 else "reject"
        got = _problem(face, w, h)
        if expected is None:
            assert got is None, f"rejected at {w}x{h}: {got}"
        else:
            assert got is not None, f"accepted at {w}x{h} but should be rejected"


def test_gate_prevents_the_degenerate_crop():
    """The point of the gate: a rejected box would degenerate the crop; an accepted one does not.

    This is the property D-026 relies on, asserted rather than described.
    """
    frame = np.zeros((1080, 1920, 3), np.uint8)
    degenerate = (15, 11, 994, 1058)
    good = (700, 200, 400, 500)

    bad_crop = crop_rgb(frame, degenerate)
    assert bad_crop.shape[0] >= 1080, "expected the degenerate crop to span the frame height"

    assert _problem(degenerate) is not None, "degenerate box must be rejected by the gate"

    good_crop = crop_rgb(frame, good)
    assert good_crop.shape[0] < 1080, "expected a real crop, not the whole frame"
    assert _problem(good) is None, "a genuine crop must be accepted"


def test_gate_rejects_exactly_what_pad_box_would_clip():
    """Whatever the gate lets through must keep a real margin around the face."""
    frame_w, frame_h = 1920, 1080
    frame = np.zeros((frame_h, frame_w, 3), np.uint8)
    limit = MAX_FACE_FRACTION * min(frame_w, frame_h)
    box = (int((frame_w - limit) / 2), int((frame_h - limit) / 2), int(limit), int(limit))
    assert _problem(box) is None
    crop = crop_rgb(frame, box)
    # A 25% pad on each side needs 1.5x the box; if that fits, the crop is margin plus face.
    assert crop.shape[0] > box[3], "accepted box lost its vertical margin"
    assert crop.shape[1] > box[2], "accepted box lost its horizontal margin"

# --------------------------------------------------------------------------
# Condition prompts (D-036)
# --------------------------------------------------------------------------


def test_condition_prompts_cover_the_requested_lighting_and_distance():
    """The four conditions the operator explicitly asked for must all be prompted."""
    joined = " | ".join(CONDITION_PROMPTS).lower()
    assert "lamp" in joined, "indoor lamp lighting not prompted"
    assert "window" in joined and "daylight" in joined, "daylight not prompted"
    assert "1 m" in joined, "the ~1 m distance not prompted"
    assert "2 m" in joined, "the ~2 m distance not prompted"


def test_condition_prompts_include_the_measured_pose_failures():
    """Pose is the axis the gate actually failed on, so it must be prompted.

    Every rejected owner frame was a head-turn or a chin-tilt - yaw left 0.589,
    yaw right 0.515, chin up 0.622 - while a far set at a different distance and
    brightness passed 4/4 with max 0.486. A prompt set without pose would leave
    the one measured failure axis uncovered.
    """
    joined = " | ".join(CONDITION_PROMPTS).lower()
    assert "your left" in joined, "no left-yaw prompt"
    assert "your right" in joined, "no right-yaw prompt"
    assert "chin tilted up" in joined, "no chin-up prompt"
    assert "chin tucked down" in joined, "no chin-down prompt"


def test_pose_prompts_are_appended_so_the_first_four_keep_their_order():
    """The requested prompts keep their order; pose was added, not substituted."""
    assert CONDITION_PROMPTS[0].startswith("indoor lamp")
    assert CONDITION_PROMPTS[1].startswith("daylight")
    assert CONDITION_PROMPTS[2].startswith("about 1 m")
    assert CONDITION_PROMPTS[3].startswith("about 2 m")


def test_condition_prompts_cycle_so_any_photo_count_covers_all_axes():
    """A run of len(CONDITION_PROMPTS) covers each exactly once, then repeats."""
    count = len(CONDITION_PROMPTS)
    assert len(set(CONDITION_PROMPTS)) == count
    first = [condition_prompt(i) for i in range(count)]
    assert len(set(first)) == count
    assert condition_prompt(count) == condition_prompt(0)
    assert condition_prompt(count + 3) == condition_prompt(3)


def test_condition_prompt_index_is_zero_based():
    assert condition_prompt(0) == CONDITION_PROMPTS[0]
    assert condition_prompt(1) == CONDITION_PROMPTS[1]


def test_draw_condition_prompt_does_not_crash_and_writes_into_the_frame():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    draw_condition_prompt(frame, condition_prompt(0))
    assert frame.any(), "nothing was drawn"
