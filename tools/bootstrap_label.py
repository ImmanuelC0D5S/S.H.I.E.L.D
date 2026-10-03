"""Auto-label captured frames with a pretrained face detector.

Replaces training-from-scratch as the primary path (D-026). The HOG auto-labeller in
``capture_dataset.py`` is only good enough for an interactive tool: it finds the face
in a well-framed webcam frame, and its box is the *inner* face. A pretrained detector
already trained on a large public face corpus is both more accurate and consistent
across pose, scale and lighting, so its labels are the better teacher when the goal
is to fine-tune on a small personal set.

**This writes detector output, not ground truth.** Every label must still be reviewed
with ``tools/preview_labels.py`` before training. The tool reports what it did so the
review is targeted rather than blind: it counts detections, refuses to label a frame
where it is unsure, and flags the geometric problems that are cheap to catch
mechanically.

**Boxes are written un-padded.** The pretrained model's own box convention is what a
fine-tune should regress toward - padding the training labels would teach the
fine-tuned model to expect a looser box than its base, and would desynchronise it
from the recognition path, where padding *is* applied (D-026 step 5).

Usage:
    python tools/bootstrap_label.py --model rpi/models/face_pretrained.pt --dry-run
    python tools/bootstrap_label.py --model rpi/models/face_pretrained.pt
    python tools/bootstrap_label.py --model rpi/models/face_pretrained.pt --overwrite
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2
from ultralytics import YOLO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset_meta import (  # noqa: E402
    FACE_CLASS_ID,
    append_metadata,
    load_metadata,
    split_summary_lines,
    summarise,
    write_data_yaml,
)

LOGGER = logging.getLogger("shield.bootstrap_label")

# A face smaller than this fraction of the frame, or thinner than a face can be, is
# almost always a false positive - a window frame, a hand, part of a poster.
MIN_AREA_FRACTION = 0.0015
MIN_ASPECT = 0.45
MAX_ASPECT = 1.45


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", default="rpi/datasets/faces", help="dataset root")
    parser.add_argument(
        "--model", default=str(Path("rpi") / "models" / "face_pretrained.pt"),
        help="pretrained detector to label with",
    )
    parser.add_argument(
        "--splits", nargs="+", default=["train"],
        help="splits to label; include holdout deliberately if you mean to test on it",
    )
    parser.add_argument("--conf", type=float, default=0.25, help="detector confidence floor")
    parser.add_argument("--imgsz", type=int, default=640, help="detector input size")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="relabel frames that already have a label file",
    )
    parser.add_argument("--dry-run", action="store_true", help="report the plan, write nothing")
    return parser.parse_args()


def best_box(results: object, conf: float) -> tuple[int, int, int, int, float] | None:
    """Highest-confidence box above ``conf`` as ``(x, y, w, h, confidence)``."""
    boxes = getattr(results, "boxes", None)
    if boxes is None or not len(boxes):
        return None
    best = max(range(len(boxes)), key=lambda i: float(boxes.conf[i]))
    score = float(boxes.conf[best])
    if score < conf:
        return None
    x1, y1, x2, y2 = (int(round(v)) for v in boxes.xyxy[best].tolist())
    return x1, y1, x2 - x1, y2 - y1, score


def geometric_problems(box: tuple[int, int, int, int], frame_w: int, frame_h: int) -> list[str]:
    """Cheap sanity checks on a label. Empty list means it looks sane."""
    problems: list[str] = []
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return ["empty box"]
    if (w * h) / (frame_w * frame_h) < MIN_AREA_FRACTION:
        problems.append(f"very small ({100 * w * h / (frame_w * frame_h):.2f}% of frame)")
    aspect = w / h
    if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
        problems.append(f"implausible aspect {aspect:.2f}")
    if x < 0 or y < 0 or x + w > frame_w or y + h > frame_h:
        problems.append("box runs outside the frame")
    return problems


def write_label(path: Path, box: tuple[int, int, int, int], frame_w: int, frame_h: int) -> None:
    """Write one YOLO detection label: ``class cx cy w h`` normalised to 0..1."""
    x, y, w, h = box
    path.write_text(
        f"{FACE_CLASS_ID} {(x + w / 2) / frame_w:.6f} {(y + h / 2) / frame_h:.6f} "
        f"{w / frame_w:.6f} {h / frame_h:.6f}\n",
        encoding="utf-8",
    )


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    root = Path(args.dataset)
    model_path = Path(args.model)
    if not model_path.is_file():
        LOGGER.error("no detector at %s", model_path)
        return 2

    detector = YOLO(str(model_path))
    LOGGER.info(
        "detector %s loaded: nc=%d names=%s", model_path.name, len(detector.names), detector.names
    )
    if len(detector.names) != 1:
        LOGGER.error(
            "expected a single-class face detector, got %d classes: %s",
            len(detector.names),
            detector.names,
        )
        return 2

# load_metadata() is already keyed by image filename, so this preserves the split,
    # condition and device recorded at capture time instead of relabelling them "unknown".
    known = load_metadata(root)

    totals: dict[str, int] = {
        "labelled": 0,
        "skipped_existing": 0,
        "no_face": 0,
        "implausible": 0,
        "unreadable": 0,
    }

    for split in args.splits:
        images_dir = root / "images" / split
        labels_dir = root / "labels" / split
        if not images_dir.is_dir():
            LOGGER.error("no images in %s", images_dir)
            return 2
        labels_dir.mkdir(parents=True, exist_ok=True)

        paths = sorted(images_dir.glob("*.jpg"))
        for path in paths:
            label_path = labels_dir / f"{path.stem}.txt"
            if label_path.is_file() and not args.overwrite:
                totals["skipped_existing"] += 1
                continue

            image = cv2.imread(str(path))
            if image is None:
                LOGGER.warning("unreadable, skipping: %s", path.name)
                totals["unreadable"] += 1
                continue

            frame_h, frame_w = image.shape[:2]
            found = best_box(detector(image, imgsz=args.imgsz, conf=args.conf, verbose=False)[0],
                             args.conf)
            if found is None:
                # Leave whatever is on disk untouched and say so. Deleting or
                # blanking a label silently converts a good frame into an
                # unlabelled one, which training then ignores.
                LOGGER.warning("no face at conf>=%.2f, label left untouched: %s",
                               args.conf, path.name)
                totals["no_face"] += 1
                continue

            x, y, w, h, score = found
            problems = geometric_problems((x, y, w, h), frame_w, frame_h)
            if problems:
                LOGGER.warning("%s: %s - label left untouched", path.name, "; ".join(problems))
                totals["implausible"] += 1
                continue

            if args.dry_run:
                LOGGER.info(
                    "[dry-run] would label %s: box=(%d,%d,%d,%d) conf=%.2f",
                    path.name, x, y, w, h, score,
                )
                totals["labelled"] += 1
                continue

            write_label(label_path, (x, y, w, h), frame_w, frame_h)
            append_metadata(
                root / "metadata.jsonl",
                {
                    "file": f"images/{split}/{path.name}",
                    "split": split,
                    "condition": known.get(path.stem, {}).get("condition", "bootstrapped"),
                    "person": known.get(path.stem, {}).get("person", "unlabelled"),
                    "device": known.get(path.stem, {}).get("device", -1),
                    "labeller": model_path.name,
                    "labeller_conf": round(score, 4),
                    "face_box": [x, y, w, h],
                    "frame": [frame_w, frame_h],
                    "pad_applied": False,
                },
            )
            totals["labelled"] += 1

    if not args.dry_run:
        write_data_yaml(root)
        for line in summarise_lines(root):
            LOGGER.info("%s", line)

    LOGGER.info(
        "labelled %(labelled)d, skipped %(skipped_existing)d existing, "
        "%(no_face)d no face, %(implausible)d implausible, %(unreadable)d unreadable",
        totals,
    )
    if totals["implausible"] or totals["no_face"]:
        LOGGER.warning(
            "frames left untouched above need a human look; review with "
            "tools/preview_labels.py before training"
        )
    # A dry run that cannot label everything, or an untouched set, is not a failure
    # of the tool - but it is not a dataset either.
    return 0


def summarise_lines(root: Path) -> list[str]:
    return split_summary_lines(summarise(root))


if __name__ == "__main__":
    raise SystemExit(main())
