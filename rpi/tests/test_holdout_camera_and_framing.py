"""Holdout and capture framing must not quietly measure the wrong thing.

Both cases here produced a gate result that looked like a mediocre score rather
than a broken test: an enrolment and its holdout on different cameras (D-030), and
a holdout captured at a desk distance the Brio crops too tightly to score (D-029).
A number that is wrong in a way nobody can see is worse than a crash, so both are
refused explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from capture_dataset import reason_no_face  # noqa: E402
from holdout_test import holdout_camera_problem  # noqa: E402

# --- camera consistency -------------------------------------------------


def _meta(**per_name):
    return {name: {"device": device} for name, device in per_name.items()}


def test_single_matching_camera_is_accepted():
    meta = _meta(a=1, b=1)
    assert holdout_camera_problem(meta, ["a", "b"], configured_device=1) is None


def test_mixed_camera_holdout_is_refused():
    meta = _meta(a=0, b=1)
    problem = holdout_camera_problem(meta, ["a", "b"], configured_device=1)
    assert problem is not None
    assert "2 cameras" in problem


def test_holdout_from_a_different_camera_than_config_is_refused():
    """The D-030 case: Brio enrolment tested against built-in 720p holdout."""
    meta = _meta(a=0)
    problem = holdout_camera_problem(meta, ["a"], configured_device=1)
    assert problem is not None
    assert "device 0" in problem and "device 1" in problem


def test_missing_camera_metadata_is_advisory_not_fatal():
    meta = {"a": {}}
    assert holdout_camera_problem(meta, ["a"], configured_device=1) is None


def test_unknown_configured_device_skips_the_mismatch_check():
    meta = _meta(a=0)
    assert holdout_camera_problem(meta, ["a"], configured_device=None) is None


def test_device_zero_is_honoured_not_treated_as_absent():
    """device 0 is a real index; a falsy check would silently skip it."""
    meta = _meta(a=0)
    assert holdout_camera_problem(meta, ["a"], configured_device=0) is None


# --- framing upper bound -------------------------------------------------


def test_over_tight_face_is_refused_with_a_move_back_instruction():
    """The Brio at desk distance: 831px on a 1080 short edge is 77%, not 45%."""
    status, _ = reason_no_face([(0, 0, 661, 831)], 60, frame_w=1920, frame_h=1080)
    assert "too large" in status
    assert "move back" in status


def test_face_at_the_upper_bound_is_accepted():
    limit = 0.45 * 1080
    status, _ = reason_no_face([(0, 0, int(limit), int(limit))], 60, 1920, 1080)
    assert "ready" in status


def test_well_framed_face_is_accepted():
    status, _ = reason_no_face([(0, 0, 300, 400)], 60, 1920, 1080)
    assert "ready" in status


def test_too_small_wins_over_too_large_when_both_cannot_apply():
    status, _ = reason_no_face([(0, 0, 30, 30)], 60, 1920, 1080)
    assert "too small" in status


def test_upper_bound_ignored_when_frame_size_not_supplied():
    """Two-arg callers keep the lower-bound-only behaviour."""
    status, _ = reason_no_face([(0, 0, 661, 831)], 60)
    assert "ready" in status


def test_max_face_one_disables_the_upper_bound():
    status, _ = reason_no_face([(0, 0, 661, 831)], 60, 1920, 1080, max_fraction=1.0)
    assert "ready" in status


def test_no_face_still_reported_first():
    status, _ = reason_no_face([], 60, 1920, 1080)
    assert "no face" in status


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
