"""Dataset split/metadata bookkeeping, shared by the Phase 1 capture and gate tools.

Kept free of cv2/dlib/ultralytics so the split logic can be unit-tested without
loading the CV stack, and so ``capture_dataset.py`` and ``holdout_test.py`` cannot
disagree about what a split means. That includes *where* the stranger set lives and
how it is grouped by person: the gate reads whatever this module finds, so the two
tools cannot end up scoring different directories.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger("shield.dataset_meta")

TRAIN = "train"
VAL = "val"
HOLDOUT = "holdout"

# Additional held-out splits. Same rule as HOLDOUT - never trained on, never
# enrolled from - but kept apart so the gate can report a deliberately
# difficult set separately from the routine one. A single pooled number hides
# the thing that matters: *which* conditions the recogniser actually fails.
#
# ``holdout-hard`` was captured in a different place, further from the camera.
# It is preserved verbatim and never retaken or deleted, because the value of a
# hard set is that it stays hard and stays the same, so runs stay comparable.
HOLDOUT_HARD = "holdout-hard"
HOLDOUT2 = "holdout2"

# Every split the gate scores. Ordered easiest-first for reporting.
GATE_SPLITS = (HOLDOUT, HOLDOUT2, HOLDOUT_HARD)

# Splits a capture run may write into. Deliberately *not* the same as
# GATE_SPLITS: HOLDOUT_HARD is a fixed regression witness and capturing into it
# would dilute a set that exists precisely because it never changes. Keeping the
# omission here - rather than in the argparse call - means the tool's choices and
# this module cannot drift apart, which is exactly how `--split holdout2` came to
# be rejected while HOLDOUT2 sat in GATE_SPLITS all along (D-038).
CAPTUREABLE_SPLITS = (TRAIN, VAL, HOLDOUT, HOLDOUT2)

# The stranger set is not a training split and has no YOLO labels. It lives in its own
# tree (``rpi/datasets/strangers/<person>/``), one directory per person, because the
# person is what the gate reports against and it is also what makes the files easy to
# delete afterwards. It is named here so the capture tool and the gate cannot disagree
# about what a stranger set is - the same reasoning as ``FACE_CLASS_ID``.
STRANGERS = "strangers"

# YOLO class id for "a face". Single class by design: this detector answers "is
# there a face, and where", while *who* it is is decided separately by enrolment
# and recognition. Defined here so the capture tool and the general-face importer
# cannot drift apart - they previously did, and a 4-field label line is silently
# misread by the trainer rather than rejected.
FACE_CLASS_ID = 0

# Conditions the capture guidance asks for. A detector trained only on flat frontal
# light misses an intruder in a doorway lit from the side, so these are enumerated
# rather than left to the operator's memory.
CONDITIONS = (
    "front-light",
    "dim",
    "side-light",
    "far-2m",
    "mid-1m",
    "near-0.5m",
    "angle-left",
    "angle-right",
    "glasses",
    "no-glasses",
)


class HoldoutLeakError(RuntimeError):
    """Raised when the gate-test split is referenced by the training config."""


# Out-of-envelope marking. A held-out frame can be genuinely unusable for the
# deployment - a subject looking 60 degrees up at a camera mounted at or below
# eye level is not a case the product will ever meet. Excluding such a frame is
# legitimate; *deleting* it is indistinguishable from deleting any other failure,
# and a gate that improves because a file was removed measures nothing.
#
# So exclusion is a recorded field with a mandatory reason rather than a deletion.
# The frame stays on disk, the gate prints every exclusion with its reason, and
# the denominator only ever shrinks by a decision someone can be shown.
ENVELOPE_KEY = "envelope"
ENVELOPE_OUT = "out"
ENVELOPE_REASON_KEY = "envelope_reason"

_SPLIT_KEYS = frozenset({"train", "val", "test"})

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")

# Used when a stranger photo sits directly in the root rather than in a per-person
# subdirectory. Grouped under this name so it still appears in the per-person table
# instead of silently vanishing from the report.
UNGROUPED = "ungrouped"


def highest_frame_index(names: list[str], prefix: str) -> int:
    """Highest 1-based frame number already on disk for ``prefix``, else 0.

    Re-running a condition must *continue* numbering rather than restart at 1.
    Starting over would make the second session overwrite the first session's images,
    labels and metadata rows in place, so the extra captures would silently replace
    good data instead of adding to it. Shared by the capture tool's training splits and
    its stranger split so the rule cannot drift between them.

    A non-numeric suffix is ignored rather than raising: a hand-renamed or stray file
    must not stop a capture run, and the worst case is one reused index that
    overwrites that single file.
    """
    highest = 0
    for name in names:
        suffix = name[len(prefix):]
        if suffix.endswith((".jpg", ".jpeg", ".png")):
            suffix = suffix.rsplit(".", 1)[0]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest


def collect_stranger_images(root: Path) -> dict[str, list[Path]]:
    """Group every stranger photo under ``root`` by the person directory it sits in.

    ``rglob`` rather than ``glob``: the gate must find the photos wherever they are
    nested, and a gate that silently scores an empty set because the operator put the
    files one level deeper is worse than no gate - it would report "no strangers" and
    exit as if the check had passed. Files placed directly in ``root`` are grouped under
    :data:`UNGROUPED` so they are still scored.

    Returned per person as ``{person: [paths]}`` with people and paths sorted, so two
    runs over the same directory report in the same order.
    """
    root = Path(root)
    grouped: dict[str, list[Path]] = {}
    if not root.is_dir():
        return grouped

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:  # pragma: no cover - rglob cannot return a path outside root
            continue
        person = relative.parts[0] if len(relative.parts) > 1 else UNGROUPED
        grouped.setdefault(person, []).append(path)

    return {person: grouped[person] for person in sorted(grouped)}


def stranger_counts(grouped: dict[str, list[Path]]) -> int:
    """Total stranger photos across every person."""
    return sum(len(paths) for paths in grouped.values())


class MetadataCollisionError(Exception):
    """Two metadata rows claim the same image filename in different places.

    ``load_metadata`` indexes by bare filename, so two rows sharing a basename in
    different splits are not merely redundant - one of them silently replaces the
    other. That is not a hypothetical: capturing 30 frames into ``holdout`` whose
    numbers overlapped ``holdout2`` and ``holdout-hard`` replaced 9 of their rows,
    which surfaced as five frames reported as WRONG IDENTITY and holdout-hard
    losing every expected identity (D-039).

    Frame names therefore have to be unique across the whole dataset, and the
    uniqueness is checked rather than assumed.
    """


def find_basename_collisions(rows: list[dict[str, object]]) -> dict[str, list[str]]:
    """Map each duplicated basename to the distinct paths that claim it."""
    paths: dict[str, set[str]] = {}
    for row in rows:
        raw = str(row.get("file", ""))
        name = raw.rsplit("/", 1)[-1]
        if name:
            paths.setdefault(name, set()).add(raw)
    return {n: sorted(p) for n, p in paths.items() if len(p) > 1}


def assert_unique_basenames(rows: list[dict[str, object]]) -> None:
    """Refuse to proceed on metadata where one basename means two files."""
    collisions = find_basename_collisions(rows)
    if not collisions:
        return
    listed = "\n".join(
        f"  {name}\n    " + "\n    ".join(paths)
        for name, paths in sorted(collisions.items())
    )
    raise MetadataCollisionError(
        f"{len(collisions)} image filename(s) are claimed by more than one path, so "
        "indexing by filename would silently drop rows:\n"
        f"{listed}\n"
        "Rename one set so every frame name is unique across the dataset."
    )


def append_metadata(path: Path, record: dict[str, object]) -> None:
    """Append one capture record to ``metadata.jsonl``.

    One JSON object per line, so a session can be appended across many runs
    without rewriting or truncating earlier records.
    """
    # Refuse before writing rather than after: appending a colliding row is what
    # corrupted two gate splits in D-039, and the damage is invisible until the
    # gate is read. Backing up first means a refusal costs nothing.
    if path.is_file():
        existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert_unique_basenames(existing + [record])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def read_metadata_rows(path: Path) -> list[dict[str, object]]:
    """Every metadata row as a list, without indexing. Missing file -> empty list."""
    path = Path(path)
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("skipping malformed metadata line: %.60s", line)
                continue
            if isinstance(record, dict):
                rows.append(record)
    return rows


def load_metadata(root: Path) -> dict[str, dict[str, object]]:
    """Index ``metadata.jsonl`` under ``root`` by image filename."""
    path = Path(root) / "metadata.jsonl"
    if not path.is_file():
        return {}
    rows = read_metadata_rows(path)
    # Checked before indexing, not after. Indexing first would collapse the
    # duplicate and leave nothing to detect, which is exactly how D-039's nine
    # clobbered rows passed every earlier inspection.
    assert_unique_basenames(rows)
    records: dict[str, dict[str, object]] = {}
    for record in rows:
        name = str(record.get("file", "")).rsplit("/", 1)[-1]
        if name:
            records[name] = record
    return records


def summarise(root: Path) -> dict[str, dict[str, int]]:
    """Count saved frames per split, then per condition."""
    tally: dict[str, dict[str, int]] = {}
    for record in load_metadata(root).values():
        split = str(record.get("split", "?"))
        condition = str(record.get("condition", "?"))
        tally.setdefault(split, {}).setdefault(condition, 0)
        tally[split][condition] += 1
    return tally


def split_summary_lines(tally: dict[str, dict[str, int]]) -> list[str]:
    """Human-readable per-split totals, in split order, skipping empty splits."""
    lines: list[str] = []
    for split in (TRAIN, VAL, HOLDOUT):
        counts = tally.get(split)
        if not counts:
            continue
        detail = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        lines.append(f"{split}: {sum(counts.values())} frames ({detail})")
    return lines


def write_data_yaml(root: Path) -> Path:
    """Write ``data.yaml``, deliberately omitting the holdout split.

    The holdout set exists to test the gate and must never be trained on. Leaving
    it out of the file ultralytics reads means it cannot accidentally validate on
    the gate set, which is a stronger guarantee than relying on convention.
    """
    root = Path(root)
    data_yaml = root / "data.yaml"
    data_yaml.write_text(
        "# S.H.I.E.L.D face dataset\n"
        f"path: {root.as_posix()}\n"
        f"train: images/{TRAIN}\n"
        f"val: images/{VAL}\n"
        "# Every gate split is intentionally absent (holdout, holdout2,\n"
        "# holdout-hard): reserved for the gate test, never trained on. Only\n"
        "# train and val are listed, so no held-out frame can reach ultralytics.\n"
        "# See PLAN.md phase 1 and D-036/D-038.\n"
        "names:\n"
        "  0: face\n",
        encoding="utf-8",
    )
    return data_yaml


def assert_not_gate_split(path: Path) -> None:
    """Raise if ``path`` lies inside a held-out split directory.

    The enrolment tool reads raw photos and averages them into the one embedding
    the gate scores against. If it were ever pointed at a holdout directory - by
    a mistyped ``--photo-dir``, or by a future "build the centroid from existing
    photos" flag - the gate would be scoring the model on frames it trained on,
    and the number it reports would mean nothing. This refuses the path instead,
    because a silently leaked holdout produces a *passing* gate, which is the
    most expensive kind of bug in this project.
    """
    parts = {part.lower() for part in Path(path).parts}
    for split in GATE_SPLITS:
        if split.lower() in parts:
            raise HoldoutLeakError(
                f"{path} is inside the {split!r} split; enrolment must never read held-out "
                "frames - the gate would then be scoring frames it trained on"
            )


def assert_holdout_is_clean(root: Path) -> None:
    """Raise if ``data.yaml`` points a training split at the holdout directory.

    Guards against a hand-edited or regenerated ``data.yaml`` quietly training on
    the gate set. Only the ``train``/``val``/``test`` keys are inspected - the
    ``path:`` key is the dataset root, which may legitimately contain the word
    "holdout" in a directory name. Comments are ignored, because the generated
    file explains in a comment that holdout is absent on purpose.
    """
    data_yaml = Path(root) / "data.yaml"
    if not data_yaml.is_file():
        LOGGER.warning("no data.yaml in %s - cannot verify the holdout set was excluded", root)
        return
    for line in data_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip().lower() not in _SPLIT_KEYS:
            continue
        if HOLDOUT in value.strip().lower():
            raise HoldoutLeakError(
                f"data.yaml sets {key.strip()}: {value.strip()!r}; "
                "the gate set must never be trained on - fix data.yaml and retrain"
            )


def training_cameras(root: Path) -> set[str]:
    """Distinct cameras that contributed frames to a split, for domain-shift checks."""
    cameras: set[str] = set()
    for record in load_metadata(root).values():
        note = str(record.get("camera_note") or "").strip()
        label = note or f"device {record.get('device')}"
        cameras.add(label)
    return cameras