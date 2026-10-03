"""Held-out split separation: enrolment must never read a gate split, and the gate
must report each split separately instead of pooling them.

Both rules exist because the failure they prevent is silent and passing: a
holdout leaked into the enrolment centroid makes the gate report a number that
looks great and means nothing, and a pooled report hides which condition failed
until long after it is fixed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from dataset_meta import (  # noqa: E402
    CAPTUREABLE_SPLITS,
    ENVELOPE_KEY,
    ENVELOPE_OUT,
    ENVELOPE_REASON_KEY,
    GATE_SPLITS,
    HOLDOUT,
    HOLDOUT2,
    HOLDOUT_HARD,
    TRAIN,
    VAL,
    HoldoutLeakError,
    assert_not_gate_split,
)
from holdout_test import aggregate_stats, gate_splits, partition_envelope  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def test_hard_and_second_holdouts_are_declared_as_gate_splits():
    """Both new splits must be in GATE_SPLITS or the gate silently skips them."""
    assert HOLDOUT_HARD == "holdout-hard"
    assert HOLDOUT2 == "holdout2"
    for split in (HOLDOUT, HOLDOUT2, HOLDOUT_HARD):
        assert split in GATE_SPLITS


def test_gate_splits_are_distinct():
    assert len(set(GATE_SPLITS)) == len(GATE_SPLITS)


def test_hard_split_name_contains_holdout_so_yaml_guard_covers_it():
    """assert_holdout_is_clean substring-matches "holdout".

    A split named e.g. "difficult" would slip past the data.yaml train/val leak
    guard, so the hard split deliberately keeps "holdout" in its name.
    """
    assert "holdout" in HOLDOUT_HARD.lower()
    assert "holdout" in HOLDOUT2.lower()


@pytest.mark.parametrize(
    "bad",
    [
        "rpi/datasets/faces/images/holdout",
        "rpi/datasets/faces/images/holdout2",
        "rpi/datasets/faces/images/holdout-hard",
        "rpi/datasets/faces/images/holdout-hard/imman",
        "rpi/datasets/faces/labels/holdout2",
    ],
)
def test_gate_split_paths_are_refused(bad):
    with pytest.raises(HoldoutLeakError):
        assert_not_gate_split(Path(bad))


@pytest.mark.parametrize(
    "ok",
    [
        "rpi/enroll_photos/imman",
        "rpi/known_faces",
        "rpi/datasets/strangers/friend1",
        "review/holdout_720p_builtin",
    ],
)
def test_ordinary_enrolment_paths_are_allowed(ok):
    assert_not_gate_split(Path(ok))


def test_review_archive_named_like_a_holdout_is_still_allowed():
    """Archiving old frames into review/ must not trip the guard.

    review/holdout_720p_builtin contains the substring "holdout" but is not a
    split component, and blocking it would make the archive step impossible.
    """
    assert_not_gate_split(Path("review/holdout_720p_builtin"))


def test_enrol_tool_calls_the_guard_on_its_photo_dir():
    """The guard is useless if enrolment never invokes it."""
    source = (ROOT / "tools" / "enroll_laptop.py").read_text(encoding="utf-8")
    assert "assert_not_gate_split(photo_dir)" in source


def test_gate_splits_defaults_to_every_split_present(tmp_path):
    for split in GATE_SPLITS:
        d = tmp_path / "images" / split
        d.mkdir(parents=True)
        (d / "a.jpg").write_bytes(b"x")
    assert set(gate_splits(tmp_path, "")) == set(GATE_SPLITS)


def test_gate_splits_skips_splits_with_no_frames(tmp_path):
    """An empty split dir must not become a zero-frame report."""
    (tmp_path / "images" / HOLDOUT2).mkdir(parents=True)
    (tmp_path / "images" / HOLDOUT2 / "a.jpg").write_bytes(b"x")
    (tmp_path / "images" / HOLDOUT_HARD).mkdir(parents=True)
    assert gate_splits(tmp_path, "") == [HOLDOUT2]


def test_gate_splits_honours_explicit_request(tmp_path):
    for split in GATE_SPLITS:
        d = tmp_path / "images" / split
        d.mkdir(parents=True)
        (d / "a.jpg").write_bytes(b"x")
    assert gate_splits(tmp_path, "holdout-hard") == [HOLDOUT_HARD]
    # Requested order is preserved, so the operator controls report order.
    assert gate_splits(tmp_path, "holdout2,holdout-hard") == [HOLDOUT2, HOLDOUT_HARD]


def test_gate_splits_ignores_unknown_requested_names(tmp_path):
    d = tmp_path / "images" / HOLDOUT2
    d.mkdir(parents=True)
    (d / "a.jpg").write_bytes(b"x")
    assert gate_splits(tmp_path, "nonexistent") == []


def _stats(**kw):
    base = {
        "total": 0,
        "detected": 0,
        "missed": 0,
        "correct": 0,
        "unknown": 0,
        "wrong_identity": 0,
        "distances": [],
        "unknown_details": [],
        "missed_files": [],
        "per_condition": {},
    }
    base.update(kw)
    return base


def test_aggregate_pools_counts():
    a = _stats(total=4, detected=4, correct=4, distances=[0.4, 0.5])
    b = _stats(total=15, detected=15, correct=12, distances=[0.6])
    out = aggregate_stats([a, b])
    assert out["total"] == 19
    assert out["correct"] == 16
    assert out["accuracy"] == pytest.approx(16 / 19)


def test_aggregate_weights_by_frame_count_not_split_count():
    """A 4-frame split must not carry the same weight as a 15-frame split.

    Averaging the two per-split rates would give (100% + 80%)/2 = 90% and pass
    the gate; the true figure is 16/19 = 84.2%. This is why aggregate_stats
    recomputes from summed counts.
    """
    small = _stats(total=4, detected=4, correct=4)
    large = _stats(total=15, detected=15, correct=12)
    pooled = aggregate_stats([small, large])
    assert pooled["accuracy"] == pytest.approx(16 / 19)
    assert pooled["accuracy"] < 0.90  # must still fail the gate


def test_aggregate_concatenates_distances_and_unknown_details():
    a = _stats(
        total=1, detected=1, unknown=1, distances=[0.51],
        unknown_details=[{"file": "a.jpg", "distance": 0.51, "width": 100,
                          "height": 120, "aspect": 0.83}],
    )
    b = _stats(
        total=1, detected=1, unknown=1, distances=[0.62],
        unknown_details=[{"file": "b.jpg", "distance": 0.62, "width": 90,
                          "height": 130, "aspect": 0.69}],
    )
    out = aggregate_stats([a, b])
    assert out["distances"] == [0.51, 0.62]
    assert out["mean_distance"] == pytest.approx(0.565)
    assert out["max_distance"] == pytest.approx(0.62)
    assert [r["file"] for r in out["unknown_details"]] == ["a.jpg", "b.jpg"]


def test_aggregate_handles_empty_input_without_dividing_by_zero():
    out = aggregate_stats([])
    assert out["total"] == 0
    assert out["accuracy"] == 0.0
    assert out["detection_rate"] == 0.0


# --------------------------------------------------------------------------
# Out-of-envelope exclusion (D-037)
#
# An exclusion shrinks the denominator, so a gate can be made to pass by
# removing inconvenient frames. These pin the three properties that stop that:
# a reason is mandatory, holdout-hard is immune, and the excluded frames are
# still on disk.


def _meta(name, split, reason=None, flagged=True):
    record = {"file": f"images/{split}/{name}", "split": split, "person": "imman"}
    if flagged:
        record[ENVELOPE_KEY] = ENVELOPE_OUT
        record[ENVELOPE_REASON_KEY] = reason or ""
    return {name: record}


def test_partition_splits_flagged_frames_out_with_their_reason():
    metadata = {
        **_meta("a.jpg", "holdout2", "chin tilted up 60 degrees"),
        "b.jpg": {"file": "images/holdout2/b.jpg", "split": "holdout2", "person": "imman"},
    }
    paths = [Path("a.jpg"), Path("b.jpg")]
    scored, excluded = partition_envelope(metadata, "holdout2", paths)
    assert [p.name for p in scored] == ["b.jpg"]
    assert excluded == [("a.jpg", "chin tilted up 60 degrees")]


def test_partition_ignores_an_exclusion_with_no_reason():
    """An unexplained exclusion is indistinguishable from hiding a failure."""
    metadata = {**_meta("a.jpg", "holdout2", reason="   ")}
    scored, excluded = partition_envelope(metadata, "holdout2", [Path("a.jpg")])
    assert [p.name for p in scored] == ["a.jpg"], "blank reason must not exclude"
    assert excluded == []


def test_partition_ignores_envelope_values_other_than_out():
    metadata = {"a.jpg": {"file": "images/holdout2/a.jpg", "split": "holdout2",
                          "person": "imman", ENVELOPE_KEY: "in",
                          ENVELOPE_REASON_KEY: "normal"}}
    scored, excluded = partition_envelope(metadata, "holdout2", [Path("a.jpg")])
    assert [p.name for p in scored] == ["a.jpg"]
    assert excluded == []


def test_partition_refuses_to_exclude_anything_from_holdout_hard():
    """holdout-hard exists to be scored; hollowing it out proves nothing."""
    metadata = {**_meta("a.jpg", "holdout-hard", "looks odd")}
    paths = [Path("a.jpg"), Path("b.jpg")]
    scored, excluded = partition_envelope(metadata, "holdout-hard", paths)
    assert scored == paths, "holdout-hard must be scored in full"
    assert excluded == [], "holdout-hard must never be excluded"


def test_partition_leaves_frames_with_no_metadata_row_in_the_gate():
    """An unreadable row is not permission to drop the frame silently."""
    scored, excluded = partition_envelope({}, "holdout2", [Path("a.jpg")])
    assert [p.name for p in scored] == ["a.jpg"]
    assert excluded == []


def test_envelope_tool_has_no_delete_option():
    """The distinction between excluding and deleting must stay visible in the CLI."""
    source = Path("tools/holdout_envelope.py").read_text(encoding="utf-8")
    for banned in ("--delete", "os.remove", "unlink", "rmtree", "shutil.rmtree"):
        assert banned not in source, f"envelope tool must not be able to {banned}"
    assert "--reason" in source, "exclusion must require a reason"
    assert "--in" in source, "an exclusion must be reversible"


# --------------------------------------------------------------------------
# Capture-split choices (D-038)
#
# HOLDOUT2 existed in GATE_SPLITS while capture_dataset.py hardcoded its own
# choices tuple, so `--split holdout2` was rejected by the tool that creates the
# very frames the gate scores. The fix derives the choices from one constant;
# these pin that they stay derived, and that the one split which must never
# change stays uncapturable.


def test_captureable_splits_offer_holdout2():
    assert HOLDOUT2 in CAPTUREABLE_SPLITS
    assert TRAIN in CAPTUREABLE_SPLITS
    assert VAL in CAPTUREABLE_SPLITS


def test_captureable_splits_exclude_holdout_hard():
    """Capturing into the fixed regression witness would dilute the only split
    that is supposed to never change."""
    assert HOLDOUT_HARD not in CAPTUREABLE_SPLITS
    assert HOLDOUT_HARD in GATE_SPLITS, "it must still be a scored gate split"


def test_capture_tool_derives_its_choices_rather_than_hardcoding_them():
    source = Path("tools/capture_dataset.py").read_text(encoding="utf-8")
    assert "CAPTUREABLE_SPLITS" in source, "choices must come from the shared constant"
    hardcoded = 'choices=("train", "val", "holdout", STRANGERS)'
    assert hardcoded not in source, "the hardcoded tuple is what caused D-038"


def test_capture_tool_warns_for_every_gate_split_not_just_holdout():
    """The never-train-on warning must not be bypassable by choosing another name."""
    source = Path("tools/capture_dataset.py").read_text(encoding="utf-8")
    assert "if split in GATE_SPLITS:" in source
    assert 'if split == "holdout":' not in source


def test_every_captureable_gate_split_is_actually_a_gate_split():
    """A held-out split that capture can write to must be one the gate scores."""
    for split in CAPTUREABLE_SPLITS:
        if split in (TRAIN, VAL):
            continue
        assert split in GATE_SPLITS, f"{split} is capturable but not gate-scored"
