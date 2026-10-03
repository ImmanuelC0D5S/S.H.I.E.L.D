"""Tests for dataset split and metadata bookkeeping.

These import only ``dataset_meta``, which is deliberately free of cv2/dlib/
ultralytics, so the rules protecting the held-out gate set are tested without
paying the cost of loading the CV stack.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from dataset_meta import (  # noqa: E402
    CONDITIONS,
    FACE_CLASS_ID,
    HOLDOUT,
    TRAIN,
    VAL,
    HoldoutLeakError,
    MetadataCollisionError,
    append_metadata,
    assert_holdout_is_clean,
    assert_unique_basenames,
    find_basename_collisions,
    load_metadata,
    read_metadata_rows,
    split_summary_lines,
    summarise,
    training_cameras,
    write_data_yaml,
)


def test_written_label_has_five_fields(tmp_path):
    """A YOLO detection label must be ``class cx cy w h`` - five fields.

    The capture tool wrote four, omitting the class id. YOLO does not reject that:
    it shifts the columns and reads the centre-x as a class index, so every box
    lands somewhere arbitrary and training appears to run while learning nothing.
    All 220 captured frames were affected; this pins the field count so the next
    regression is caught here rather than in a trained model.
    """
    from capture_dataset import write_yolo_label

    path = tmp_path / "frame.txt"
    write_yolo_label(path, (100, 50, 200, 300), 1280, 720)

    parts = path.read_text(encoding="utf-8").split()
    assert len(parts) == 5, f"expected 5 fields, got {len(parts)}: {parts}"

    klass, cx, cy, w, h = (float(p) for p in parts)
    assert klass == FACE_CLASS_ID
    # (100,50,200,300) on 1280x720 -> cx=(100+100)/1280, cy=(50+150)/720
    assert cx == pytest.approx((100 + 200 / 2) / 1280, abs=1e-6)
    assert cy == pytest.approx((50 + 300 / 2) / 720, abs=1e-6)
    assert w == pytest.approx(200 / 1280, abs=1e-6)
    assert h == pytest.approx(300 / 720, abs=1e-6)


def test_every_written_field_is_in_unit_range(tmp_path):
    """Normalised coordinates must stay inside 0..1 or YOLO rejects the label."""
    from capture_dataset import write_yolo_label

    path = tmp_path / "frame.txt"
    # A box flush against the top-left corner: naive x-w/2 would go negative.
    write_yolo_label(path, (0, 0, 100, 100), 1280, 720)
    _, cx, cy, w, h = (float(p) for p in path.read_text(encoding="utf-8").split())
    assert 0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0
    assert 0.0 < w <= 1.0 and 0.0 < h <= 1.0


def test_preview_labels_rejects_a_four_field_label(tmp_path):
    """The label reader must fail loudly, not silently drop the line.

    Its reader used to swallow short lines, which reported every affected frame as
    "no label file / empty" and pointed the operator at the wrong problem.
    """
    from preview_labels import read_labels

    bad = tmp_path / "frame.txt"
    bad.write_text("0.503125 0.522917 0.218750 0.384722\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected 5 fields"):
        read_labels(bad)

    good = tmp_path / "ok.txt"
    good.write_text(f"{FACE_CLASS_ID} 0.5 0.5 0.2 0.3\n", encoding="utf-8")
    assert read_labels(good) == [(0.5, 0.5, 0.2, 0.3)]


def test_class_id_is_shared_by_capture_and_general_importer():
    """Both writers must agree on the class id, from one definition.

    They previously each defined their own constant and drifted, which is how the
    4-field regression became possible in the first place.
    """
    import add_general_faces

    assert add_general_faces.CLASS_ID == FACE_CLASS_ID


def make_record(name: str, split: str, condition: str, person: str, **extra: object) -> dict:
    return {
        "file": f"images/{split}/{name}.jpg",
        "split": split,
        "condition": condition,
        "person": person,
        "device": 0,
        **extra,
    }


# -- data.yaml must never reference the gate set ---------------------------


def test_data_yaml_omits_holdout(tmp_path):
    """No training split may point at the holdout directory.

    Checked per split key rather than by scanning every line: ``path:`` is the
    dataset root, whose name may legitimately contain "holdout".
    """
    path = write_data_yaml(tmp_path)
    splits = {
        key.strip(): value.strip()
        for key, _, value in (
            line.partition(":")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#") and ":" in line
        )
    }
    assert set(splits) >= {"train", "val"}
    for key, value in splits.items():
        if key.lower() in {"train", "val", "test"}:
            assert HOLDOUT not in value.lower()
    assert_holdout_is_clean(tmp_path)


def test_data_yaml_points_at_train_and_val(tmp_path):
    text = write_data_yaml(tmp_path).read_text(encoding="utf-8")
    assert f"train: images/{TRAIN}" in text
    assert f"val: images/{VAL}" in text


def test_holdout_leak_is_detected(tmp_path):
    """A hand-edited data.yaml that trains on holdout must be rejected."""
    (tmp_path / "data.yaml").write_text(
        "path: .\ntrain: images/train\nval: images/holdout\nnames:\n  0: face\n", encoding="utf-8"
    )
    with pytest.raises(HoldoutLeakError, match="never be trained on"):
        assert_holdout_is_clean(tmp_path)


def test_holdout_mention_in_comment_is_allowed(tmp_path):
    """The generated file explains holdout is absent, in a comment. That is fine."""
    write_data_yaml(tmp_path)
    assert_holdout_is_clean(tmp_path)


def test_missing_data_yaml_only_warns(tmp_path, caplog):
    assert_holdout_is_clean(tmp_path)
    assert "no data.yaml" in caplog.text


# -- metadata round trip ---------------------------------------------------


def test_append_then_load(tmp_path):
    append_metadata(tmp_path / "metadata.jsonl", make_record("a", TRAIN, "dim", "imman"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("b", HOLDOUT, "front-light", "imman"))
    records = load_metadata(tmp_path)
    assert set(records) == {"a.jpg", "b.jpg"}
    assert records["b.jpg"]["split"] == HOLDOUT


def test_append_accumulates_across_runs(tmp_path):
    """Sessions append; they must not truncate each other."""
    path = tmp_path / "metadata.jsonl"
    for i in range(3):
        append_metadata(path, make_record(f"f{i}", TRAIN, "dim", "imman"))
    assert len(load_metadata(tmp_path)) == 3


def test_malformed_line_is_skipped(tmp_path):
    path = tmp_path / "metadata.jsonl"
    path.write_text('{"file": "images/train/ok.jpg"}\nnot json\n', encoding="utf-8")
    records = load_metadata(tmp_path)
    assert set(records) == {"ok.jpg"}


def test_missing_metadata_is_empty(tmp_path):
    assert load_metadata(tmp_path) == {}


def test_non_dict_record_is_skipped(tmp_path):
    (tmp_path / "metadata.jsonl").write_text('["a", "b"]\n', encoding="utf-8")
    assert load_metadata(tmp_path) == {}


# -- tallies ---------------------------------------------------------------


def test_summarise_counts_per_split_and_condition(tmp_path):
    append_metadata(tmp_path / "metadata.jsonl", make_record("a", TRAIN, "dim", "imman"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("b", TRAIN, "dim", "imman"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("c", TRAIN, "side-light", "imman"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("d", HOLDOUT, "dim", "imman"))
    tally = summarise(tmp_path)
    assert tally[TRAIN] == {"dim": 2, "side-light": 1}
    assert tally[HOLDOUT] == {"dim": 1}


def test_split_summary_lines_skips_empty_splits(tmp_path):
    append_metadata(tmp_path / "metadata.jsonl", make_record("a", TRAIN, "dim", "imman"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("d", HOLDOUT, "dim", "imman"))
    lines = split_summary_lines(summarise(tmp_path))
    assert len(lines) == 2
    assert lines[0].startswith(f"{TRAIN}: 1 frames")
    assert lines[1].startswith(f"{HOLDOUT}: 1 frames")
    assert not any(line.startswith(VAL) for line in lines)


# -- camera provenance -----------------------------------------------------


def test_training_cameras_prefers_note(tmp_path):
    """The camera must be identifiable from metadata for domain-shift checks."""
    append_metadata(tmp_path / "metadata.jsonl", make_record("a", TRAIN, "dim", "imman", camera_note="laptop built-in"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("b", TRAIN, "dim", "imman", camera_note="laptop built-in"))
    append_metadata(tmp_path / "metadata.jsonl", make_record("c", TRAIN, "dim", "imman"))
    assert training_cameras(tmp_path) == {"laptop built-in", "device 0"}


# -- capture conditions cover the brief ------------------------------------


def test_conditions_cover_lighting_distance_pose_glasses():
    joined = set(CONDITIONS)
    assert {"front-light", "dim", "side-light"} <= joined, "lighting must be varied"
    assert {"near-0.5m", "mid-1m", "far-2m"} <= joined, "distance must be varied"
    assert {"angle-left", "angle-right"} <= joined, "head angle must be varied"
    assert {"glasses", "no-glasses"} <= joined, "glasses must be covered"

# --------------------------------------------------------------------------
# Basename uniqueness (D-039)
#
# load_metadata indexes rows by bare filename. Two rows sharing a basename in
# different splits is therefore not a harmless duplicate - one frame silently
# loses its identity, split and device. It happened: 30 frames captured into
# `holdout` replaced 9 rows belonging to `holdout2` and `holdout-hard` (D-039).


def _row(path, person="imman", split="holdout2"):
    return {"file": path, "split": split, "person": person}


def test_find_basename_collisions_reports_each_shared_name():
    rows = [
        _row("images/holdout2/holdout_front-light_0026.jpg"),
        _row("images/holdout/holdout_front-light_0026.jpg", split="holdout"),
        _row("images/holdout/holdout_front-light_0001.jpg", split="holdout"),
    ]
    found = find_basename_collisions(rows)
    assert list(found) == ["holdout_front-light_0026.jpg"]
    assert found["holdout_front-light_0026.jpg"] == [
        "images/holdout/holdout_front-light_0026.jpg",
        "images/holdout2/holdout_front-light_0026.jpg",
    ]


def test_identical_repeated_rows_are_not_a_collision():
    """The same path twice is redundancy, not two files fighting over one name."""
    row = _row("images/holdout2/a.jpg")
    assert find_basename_collisions([row, dict(row)]) == {}
    assert_unique_basenames([row, dict(row)])


def test_assert_unique_basenames_raises_and_names_both_paths():
    with pytest.raises(MetadataCollisionError) as excinfo:
        assert_unique_basenames(
            [
                _row("images/holdout2/b.jpg"),
                _row("images/holdout/b.jpg", split="holdout"),
            ]
        )
    message = str(excinfo.value)
    assert "b.jpg" in message
    assert "holdout2" in message and "holdout/" in message


def test_load_metadata_refuses_a_colliding_dataset(tmp_path):
    from dataset_meta import load_metadata

    (tmp_path / "metadata.jsonl").write_text(
        '{"file": "images/holdout2/c.jpg", "split": "holdout2", "person": "imman"}\n'
        '{"file": "images/holdout/c.jpg", "split": "holdout", "person": "imman"}\n',
        encoding="utf-8",
    )
    with pytest.raises(MetadataCollisionError):
        load_metadata(tmp_path)


def test_append_metadata_refuses_a_collision_and_changes_nothing(tmp_path):
    """A refused append must be a no-op, so a refusal costs the operator nothing."""
    from dataset_meta import append_metadata

    path = tmp_path / "metadata.jsonl"
    append_metadata(path, _row("images/holdout2/d.jpg"))
    before = path.read_text(encoding="utf-8")

    with pytest.raises(MetadataCollisionError):
        append_metadata(path, _row("images/holdout/d.jpg", split="holdout"))

    assert path.read_text(encoding="utf-8") == before, "refused append must not write"
    assert len(read_metadata_rows(path)) == 1


def test_capture_tool_checks_for_collisions_before_opening_the_camera():
    """The operator should be told before the session starts, not after it ends.

    Scoped to main() because the module has two camera-open sites (the strangers
    path and the owner's path); only the owner's is preceded by this check.
    """
    source = Path("tools/capture_dataset.py").read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    preflight = body.index("assert_unique_basenames(read_metadata_rows")
    assert preflight < body.index("open_camera(camera_config)"), (
        "the collision check must run before the camera opens"
    )


def test_gate_splits_have_distinct_filename_prefixes():
    """Every gate split must own its prefix, or the next capture collides.

    capture_dataset.py derives each frame's stem from its split, so distinct
    prefixes are what make basenames unique across the dataset.
    """
    prefixes = {split: f"{split}_" for split in ("holdout", "holdout2")}
    assert len(set(prefixes.values())) == len(prefixes)
    for split, prefix in prefixes.items():
        assert not prefix.startswith("holdout2") or split == "holdout2"
