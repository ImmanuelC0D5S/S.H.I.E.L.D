"""Phase 1 gate test: does the detector recognise you and reject a stranger?

The gate needs three separate answers, and it is easy to pass one while failing
another, so they are measured independently:

1. **Detection rate on you** - on held-out frames of your own face, does the
   detector find the face at all?
2. **Identification accuracy** - when it finds the face, is it labelled with your
   name rather than ``unknown`` or someone else's?
3. **Stranger detection rate (>=95%)** - on photos of other people, is the face
   *found*? This is the check that matters most and is easy to forget: a face the
   detector misses raises no event at all, so the system stays silent while
   someone is in the room. Training only on your own face makes this number
   terrible, which is why the training mix must include other people's faces -
   see ``PLAN.md`` phase 1.
4. **False accept rate** - every detected stranger face must read ``unknown``. A
   gate that only checks "me = named" is trivially passed by a model that calls
   everything my name, which is the exact failure that matters for an intrusion
   alarm.

**The held-out set is never trained on.** ``rpi/datasets/faces/images/holdout``
is deliberately absent from ``data.yaml``, so training cannot see it. This script
refuses to run if it finds holdout frames referenced by the training config,
rather than quietly reporting a meaningless number.

Usage:
    python tools/holdout_test.py
    python tools/holdout_test.py --strangers rpi/datasets/strangers --min-accuracy 0.9

Exit code 0 = gate passed, 1 = gate failed, 2 = not ready to test.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from recogniser import UNKNOWN, Recogniser, crop_rgb
from ultralytics import YOLO

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from dataset_meta import (  # noqa: E402
    ENVELOPE_KEY,
    ENVELOPE_OUT,
    ENVELOPE_REASON_KEY,
    GATE_SPLITS,
    HOLDOUT_HARD,
    HoldoutLeakError,
    MetadataCollisionError,
    assert_holdout_is_clean,
    collect_stranger_images,
    load_metadata,
    training_cameras,
)

LOGGER = logging.getLogger("shield.holdout_test")

NOT_READY = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default="rpi/datasets/faces", help="dataset root")
    parser.add_argument("--face-model", default="rpi/models/face.pt", help="trained face detector")
    parser.add_argument("--faces-dir", default="rpi/known_faces", help="enrolled embeddings")
    parser.add_argument("--strangers", default="rpi/datasets/strangers", help="stranger photos directory")
    parser.add_argument("--threshold", type=float, default=0.5, help="max cosine distance for a match")
    parser.add_argument("--conf", type=float, default=0.5, help="detector confidence floor")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument(
        "--splits",
        default="",
        help=(
            "comma-separated held-out splits to score, e.g. 'holdout2,holdout-hard'. "
            "Default: every gate split that has frames on disk. Reported separately, "
            "because pooling a deliberately hard set with a routine one hides which "
            "conditions actually fail"
        ),
    )
    parser.add_argument("--min-detection", type=float, default=0.95, help="required detection rate")
    parser.add_argument("--min-accuracy", type=float, default=0.90, help="required identification accuracy")
    parser.add_argument("--max-false-accepts", type=int, default=0, help="allowed misidentifications")
    parser.add_argument(
        "--min-stranger-detection",
        type=float,
        default=0.95,
        help=(
            "required detection rate on stranger faces. A missed face raises no event "
            "at all, so a low rate here means a real intruder can go unseen - a worse "
            "failure for this system than a false alarm, which at worst pages someone"
        ),
    )
    return parser.parse_args()


def best_face_box(results: object, conf: float) -> tuple[int, int, int, int] | None:
    """Highest-confidence face box from one ultralytics result."""
    boxes = getattr(results, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return None
    best = max(range(len(boxes)), key=lambda i: float(boxes.conf[i]))
    if float(boxes.conf[best]) < conf:
        return None
    x, y, x2, y2 = (int(v) for v in boxes.xyxy[best].tolist())
    return x, y, x2 - x, y2 - y


def score_images(
    detector: YOLO,
    recogniser: Recogniser,
    paths: list[Path],
    expected: dict[str, str],
    conf: float,
    imgsz: int,
) -> dict[str, object]:
    """Run detect+recognise over images, returning counts and a per-condition table."""
    detected = 0
    correct = 0
    wrong_identity = 0
    unknown = 0
    distances: list[float] = []
    per_condition: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    misses: list[str] = []
    unknown_details: list[dict[str, object]] = []

    for path in paths:
        frame = cv2.imread(str(path))
        if frame is None:
            LOGGER.warning("unreadable image, skipping: %s", path)
            continue

        results = detector(frame, imgsz=imgsz, conf=conf, verbose=False)[0]
        box = best_face_box(results, conf)
        want = expected.get(path.name, "")
        condition = want or "unknown"

        if box is None:
            misses.append(path.name)
            per_condition[condition][1] += 1
            continue

        detected += 1
        crop = crop_rgb(frame, box)
        if crop is None:
            misses.append(path.name)
            continue

        label, distance = recogniser.match_rgb(crop)
        distances.append(distance)
        per_condition[condition][1] += 1

        if label == want:
            correct += 1
            per_condition[condition][0] += 1
        elif label == UNKNOWN:
            unknown += 1
            # Face width is the useful diagnostic here. Recognition crops are
            # padded from this box, so a frame that is far away, or clipped by
            # the frame edge, produces a small or oddly proportioned crop and a
            # larger distance. Logging the width turns "it said unknown" into
            # "it said unknown *because* the face was 164px wide".
            unknown_details.append(
                {
                    "file": path.name,
                    "distance": distance,
                    "width": box[2],
                    "height": box[3],
                    "aspect": round(box[2] / box[3], 3) if box[3] else None,
                }
            )
            LOGGER.info(
                "%s: %s (distance %.3f, face %dpx wide, %dx%d) - not recognised",
                path.name, label, distance, box[2], box[2], box[3],
            )
        else:
            wrong_identity += 1
            LOGGER.info(
                "%s: matched %r but expected %r (distance %.3f) - WRONG IDENTITY",
                path.name, label, want, distance,
            )

    total = len(paths)
    return {
        "total": total,
        "detected": detected,
        "missed": len(misses),
        "correct": correct,
        "unknown": unknown,
        "wrong_identity": wrong_identity,
        "detection_rate": detected / total if total else 0.0,
        "accuracy": correct / detected if detected else 0.0,
        "mean_distance": float(np.mean(distances)) if distances else float("nan"),
        "max_distance": float(np.max(distances)) if distances else float("nan"),
        "per_condition": dict(per_condition),
        "missed_files": misses[:10],
        "unknown_details": unknown_details,
        "distances": distances,
        "split": None,
    }


def partition_envelope(
    metadata: dict[str, dict[str, object]],
    split: str,
    paths: list[Path],
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Split a split's frames into (scored, excluded-with-reason).

    Excluded frames are *not* deleted and *not* silently dropped: they are
    returned so the caller can print every one with the reason it was excluded.
    A frame excluded without a recorded reason is treated as in-envelope, because
    an unexplained exclusion is indistinguishable from hiding a failure.
    """
    if split == HOLDOUT_HARD:
        # holdout-hard is the fixed regression witness. It exists to be scored,
        # so allowing exclusions from it would let it be hollowed out until it
        # proves nothing - the exact opposite of why it was preserved.
        return list(paths), []

    scored: list[Path] = []
    excluded: list[tuple[str, str]] = []
    for path in paths:
        record = metadata.get(path.name, {})
        if (
            str(record.get(ENVELOPE_KEY, "")) == ENVELOPE_OUT
            and str(record.get(ENVELOPE_REASON_KEY, "")).strip()
        ):
            excluded.append((path.name, str(record[ENVELOPE_REASON_KEY])))
        else:
            scored.append(path)
    return scored, excluded


def gate_splits(root: Path, requested: str) -> list[str]:
    """Held-out splits to score.

    ``requested`` is a comma-separated list; empty means "every gate split that
    actually has frames". Missing splits are skipped rather than fatal, so a
    dataset with only one holdout still runs.
    """
    if requested.strip():
        wanted = [s.strip() for s in requested.split(",") if s.strip()]
    else:
        wanted = list(GATE_SPLITS)
    return [
        split
        for split in wanted
        if (Path(root) / "images" / split).is_dir()
        and any((Path(root) / "images" / split).glob("*.jpg"))
    ]


def aggregate_stats(parts: list[dict[str, object]]) -> dict[str, object]:
    """Pool per-split owner stats into one gate decision.

    Accuracy is recomputed from summed counts, never averaged across splits -
    averaging rates would let a 4-frame split carry the same weight as a
    40-frame one.
    """
    total = sum(int(p["total"]) for p in parts)
    detected = sum(int(p["detected"]) for p in parts)
    correct = sum(int(p["correct"]) for p in parts)
    unknown = sum(int(p["unknown"]) for p in parts)
    wrong = sum(int(p["wrong_identity"]) for p in parts)
    missed = sum(int(p["missed"]) for p in parts)
    distances: list[float] = []
    for part in parts:
        distances.extend(float(d) for d in part.get("distances") or [])
    return {
        "total": total,
        "detected": detected,
        "missed": missed,
        "correct": correct,
        "unknown": unknown,
        "wrong_identity": wrong,
        "detection_rate": detected / total if total else 0.0,
        "accuracy": correct / detected if detected else 0.0,
        "mean_distance": float(np.mean(distances)) if distances else float("nan"),
        "max_distance": float(np.max(distances)) if distances else float("nan"),
        "distances": distances,
        "unknown_details": [r for p in parts for r in (p.get("unknown_details") or [])],
        "per_condition": {},
        "missed_files": [f for p in parts for f in (p.get("missed_files") or [])][:10],
    }


def report(name: str, stats: dict[str, object], *, expected_identity: bool) -> None:
    print(f"\n=== {name} ===")
    print(f"frames            : {stats['total']}")
    print(f"faces detected    : {stats['detected']} ({stats['detection_rate'] * 100:.1f}%)")
    print(f"missed            : {stats['missed']}")
    if expected_identity:
        print(f"correct identity  : {stats['correct']} (accuracy {stats['accuracy'] * 100:.1f}% of detected)")
        print(f"read as unknown   : {stats['unknown']}")
    print(f"wrong identity    : {stats['wrong_identity']}")
    mean_d = stats["mean_distance"]
    if mean_d == mean_d:  # not NaN
        print(f"match distance    : mean {mean_d:.3f}, max {stats['max_distance']:.3f}")

    conditions = stats["per_condition"]
    if isinstance(conditions, dict) and len(conditions) > 1:
        print("  per condition   : hit/total")
        for condition, (hit, total) in sorted(conditions.items()):
            flag = "  <-- no hits" if hit == 0 and total else ""
            print(f"    {condition:<14} {hit}/{total}{flag}")

    # Only meaningful when a match was *expected*. For strangers, reading
    # "unknown" is the pass condition, so listing 50 of them as "unknown frames"
    # is noise that buries the owner's three real misses.
    unknown_details = stats.get("unknown_details") or []
    if expected_identity and unknown_details:
        print("  unknown frames  : distance and detected face size")
        for row in unknown_details:
            print(
                f"    {str(row['file']):<34} d={float(row['distance']):.3f}  "
                f"w={row['width']}px h={row['height']}px aspect={row['aspect']}"
            )


def holdout_camera_problem(
    metadata: dict[str, dict], names: list[str], configured_device: int | None
) -> str | None:
    """Explain why a holdout set cannot fairly score recognition, or ``None``.

    The gate compares an enrolled embedding against holdout frames, and both come
    from a specific sensor. A webcam's optics and crop differ enough between units
    that an enrolment and its holdout on *different* cameras are not the same test:
    the two live measurably further apart than the match threshold, so the gate
    scores camera transfer rather than the thing it exists to measure (D-030). That
    failure is invisible in the output - it just reads as a mediocre percentage -
    so it is caught here instead.

    Two conditions are refused, both hard errors rather than warnings: a holdout
    spanning more than one camera, and a holdout that is not on the camera the
    system is configured to deploy on.
    """
    devices = {
        metadata[name].get("device")
        for name in names
        if name in metadata and metadata[name].get("device") is not None
    }
    if not devices:
        return None  # no camera recorded; advisory only, never fatal
    if len(devices) > 1:
        listed = ", ".join(str(device) for device in sorted(devices))
        return (
            f"holdout spans {len(devices)} cameras (devices {listed}); a single "
            "camera cannot score recognition across sensors - recapture the whole "
            "holdout on one camera (see D-030)"
        )
    only = next(iter(devices))
    if configured_device is not None and only != configured_device:
        return (
            f"holdout was captured on camera device {only} but config.yaml selects "
            f"device {configured_device}; the gate would be scoring camera transfer "
            "rather than recognition - recapture the holdout on the configured "
            "camera (see D-030)"
        )
    return None


def configured_device() -> int | None:
    """Device index from config.yaml, or ``None`` if it cannot be read."""
    try:
        from shield_rpi.config import load_camera_config

        return load_camera_config().device
    except Exception:  # noqa: BLE001 - advisory only, never fatal to the gate
        return None


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    root = Path(args.dataset)
    try:
        assert_holdout_is_clean(root)
    except HoldoutLeakError as exc:
        LOGGER.error("%s", exc)
        return NOT_READY

    try:
        metadata = load_metadata(root)
    except MetadataCollisionError as exc:
        LOGGER.error("%s", exc)
        return NOT_READY

    if not Path(args.face_model).is_file():
        LOGGER.error("no trained model at %s", args.face_model)
        LOGGER.error("train first: yolo detect train model=yolov8n.pt data=%s epochs=50 imgsz=640", root / "data.yaml")
        return NOT_READY
    if not metadata:
        LOGGER.warning("no metadata.jsonl - cannot read expected identities, assuming 'unknown' for all")

    splits = gate_splits(root, args.splits)
    if not splits:
        LOGGER.error("no held-out frames under %s", root / "images")
        LOGGER.error(
            "known gate splits: %s. Capture some, e.g. python tools/capture_dataset.py "
            "--split holdout2 --person <you> --condition front-light",
            ", ".join(GATE_SPLITS),
        )
        return NOT_READY

    detector = YOLO(args.face_model)
    recogniser = Recogniser(args.faces_dir, args.threshold)
    if recogniser.empty:
        LOGGER.error("no enrolled embeddings in %s - enrol first", args.faces_dir)
        return NOT_READY

    cameras = training_cameras(root)
    LOGGER.info("dataset frames captured on: %s", ", ".join(sorted(cameras)) or "unrecorded")

    owner_parts: list[dict[str, object]] = []
    for split in splits:
        paths = sorted((root / "images" / split).glob("*.jpg"))
        expected = {
            name: str(record.get("person", ""))
            for name, record in metadata.items()
            if record.get("split") == split and record.get("person") not in (None, "", "unlabelled")
        }
        if not expected:
            LOGGER.error(
                "%s frames exist but none were captured with --person, so there is no "
                "expected identity to score against",
                split,
            )
            return NOT_READY

        camera_problem = holdout_camera_problem(
            metadata, [path.name for path in paths], configured_device()
        )
        if camera_problem:
            LOGGER.error("[%s] %s", split, camera_problem)
            return NOT_READY

        scored_paths, excluded = partition_envelope(metadata, split, paths)
        for name, reason in excluded:
            LOGGER.info(
                "[%s] EXCLUDED from the gate: %s - %s", split, name, reason
            )
        if excluded:
            share = len(excluded) / len(paths) if paths else 0.0
            print(
                f"\n[{split}] {len(excluded)} of {len(paths)} frames excluded as "
                f"out-of-envelope ({share * 100:.0f}% of the split, kept on disk):"
            )
            for name, reason in excluded:
                print(f"    {name}: {reason}")
            if share > 0.2:
                print(
                    "    WARNING: over 20% of this split is excluded, so the "
                    "percentage below rests on a much smaller sample and will "
                    "overstate real-world accuracy."
                )
        if not scored_paths:
            LOGGER.error("[%s] every frame is excluded - nothing left to score", split)
            return NOT_READY

        stats = score_images(detector, recogniser, scored_paths, expected, args.conf, args.imgsz)
        stats["split"] = split
        report(f"your face [{split}]", stats, expected_identity=True)
        owner_parts.append(stats)

    mine = aggregate_stats(owner_parts)
    if len(owner_parts) > 1:
        report("your face [POOLED - the gate decides on this]", mine, expected_identity=True)

    stranger_dir = Path(args.strangers)
    grouped = collect_stranger_images(stranger_dir)
    stranger_paths_all: list[Path] = []
    for _person, paths in grouped.items():
        stranger_paths_all.extend(paths)

    stranger_paths = stranger_paths_all
    strangers = None
    if stranger_paths:
        strangers = score_images(detector, recogniser, stranger_paths, {}, args.conf, args.imgsz)
        report("strangers: must all read unknown", strangers, expected_identity=False)
        by_person: dict[str, dict[str, object]] = {}
        for person, paths in grouped.items():
            s = score_images(detector, recogniser, paths, {}, args.conf, args.imgsz)
            by_person[person] = {
                "total": s["total"],
                "detected": s["detected"],
                "detection_rate": s["detection_rate"],
                "wrong_identity": s["wrong_identity"],
                "correct": s["correct"],
                "missed_files": s["missed_files"],
            }
        if by_person:
            print("\n=== strangers by person ===")
            for person in sorted(by_person):
                p = by_person[person]
                fa = int(p["wrong_identity"]) + int(p["correct"])
                print(
                    f"{person:>12}: {p['detected']}/{p['total']} detected ({p['detection_rate']*100:.1f}%), "
                    f"false accepts {fa} (wrong={p['wrong_identity']}, correct-as-match={p['correct']})"
                )
    else:
        LOGGER.warning(
            "no stranger photos in %s - the false-accept half of the gate is UNTESTED. "
            "Add photos of other people, or run tools/live_test.py with a second person present.",
            stranger_dir,
        )

    failures: list[str] = []
    if float(mine["detection_rate"]) < args.min_detection:
        failures.append(
            f"detection rate {float(mine['detection_rate']) * 100:.1f}% < {args.min_detection * 100:.0f}%"
        )
    if float(mine["accuracy"]) < args.min_accuracy:
        failures.append(
            f"identification accuracy {float(mine['accuracy']) * 100:.1f}% < {args.min_accuracy * 100:.0f}%"
        )
    if int(mine["wrong_identity"]) > args.max_false_accepts:
        failures.append(f"{mine['wrong_identity']} holdout frame(s) matched the wrong identity")

    if stranger_paths:
        misses = []
        for _person, paths in grouped.items():
            s = score_images(detector, recogniser, paths, {}, args.conf, args.imgsz)
            misses.extend(s["missed_files"])
        if misses:
            print(f"\nFiles that failed detection ({len(misses)} shown max 20):")
            for m in misses[:20]:
                print(f"  - {m}")
    if strangers is None:
        print("\nGATE: INCOMPLETE - no stranger set, detection and false accepts unverified")
        return NOT_READY

    # A stranger face the detector never finds produces no event at all: the
    # system stays silent while someone is in the room. That is the worse of the
    # two failure modes here - a false alarm is noisy, a miss is invisible - so
    # detection on strangers is gated, not just identification.
    stranger_detection = float(strangers["detection_rate"])
    if stranger_detection < args.min_stranger_detection:
        failures.append(
            f"stranger detection rate {stranger_detection * 100:.1f}% < "
            f"{args.min_stranger_detection * 100:.0f}% - undetected faces raise no alarm"
        )
    falsely_identified = int(strangers["wrong_identity"]) + int(strangers["correct"])
    if falsely_identified > args.max_false_accepts:
        failures.append(f"{falsely_identified} stranger face(s) falsely identified as enrolled")

    print()
    if failures:
        print("GATE: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        "GATE: PASS - you are named, strangers are detected and read unknown, "
        "on frames never trained on"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())