"""Add other people's faces to the training set, as YOLO labels.

**Why this is not optional.** Training a face detector only on your own captures
produces a model that reliably finds *you* and reliably misses everyone else. On
an intrusion alarm that is the dangerous direction to fail: a stranger walks in,
the detector finds nothing, and the system stays silent. Recognition cannot rescue
it either, because recognition only ever runs on faces the detector found. So the
training mix needs other people's faces, and the gate in ``holdout_test.py``
measures stranger detection for exactly this reason.

Two source formats are supported:

**1. WIDER FACE** (recommended). The standard face-detection benchmark: 32k images,
393k labelled faces across 61 event categories, with heavy variation in scale,
pose and occlusion. Annotate the same class for every face - the class is
``face``, not the identity; identity is handled separately by enrolment.

    python tools/add_general_faces.py --source wider \\
        --wider-annotations WIDER_face_split/wider_face_split_bbx_train.txt \\
        --wider-images WIDER_train/images \\
        --max-images 3000 --min-face 40

**2. A directory already in YOLO format**, e.g. a Roboflow Universe face export.
Labels must be ``<image-stem>.txt`` beside the images with one
``class cx cy w h`` row per face, normalised to 0..1.

    python tools/add_general_faces.py --source yolo --yolo-dir rpi/datasets/external_faces

**Licensing.** WIDER FACE is released for non-commercial research use, and some
redistributions carry CC BY-NC-ND 4.0. That is fine for a personal home-security
project; it is *not* fine if this is ever shipped or sold. Check the licence of
whatever you download and record it here. S.H.I.E.L.D is a personal-use system,
so non-commercial terms are acceptable - revisit if that changes.

Images are copied into ``images/train`` and ``labels/train`` alongside your own
captures, tagged ``general`` in ``metadata.jsonl`` so they can be excluded or
re-counted later. WIDER faces flagged ``invalid`` are skipped: they are
unrecognisable, and a detector trained on them learns to find faces that are not
faces.
"""

from __future__ import annotations

import argparse
import logging
import random
import shutil
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset_meta import (  # noqa: E402
    FACE_CLASS_ID as CLASS_ID,
)
from dataset_meta import (
    TRAIN,
    append_metadata,
    write_data_yaml,
)

LOGGER = logging.getLogger("shield.add_general_faces")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default="rpi/datasets/faces", help="dataset root to add into")
    parser.add_argument("--source", choices=("wider", "yolo"), required=True)
    parser.add_argument("--wider-annotations", default="", help="wider_face_split_bbx_train.txt")
    parser.add_argument("--wider-images", default="", help="directory holding the WIDER train images")
    parser.add_argument("--yolo-dir", default="", help="root of an existing YOLO-format face dataset")
    parser.add_argument("--max-images", type=int, default=3000, help="cap on images copied")
    parser.add_argument(
        "--min-face",
        type=int,
        default=40,
        help="skip faces smaller than this in pixels; too small to be useful to the detector",
    )
    parser.add_argument("--include-invalid", action="store_true", help="keep WIDER faces flagged invalid")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true", help="report the plan, copy nothing")
    return parser.parse_args()


def parse_wider(path: Path) -> list[tuple[str, list[tuple[float, float, float, float]], int]]:
    """Parse a WIDER FACE ``*_bbx_train.txt`` annotation file.

    Format is one event block per section::

        0--Parade/0_Parade_marchingband_1_849.jpg
        1
        <invalid> <blur> <pose> <occlusion> <x1> <y1> <x2> <y2> ...

    Returns ``(image_name, boxes, dropped)`` per image, where boxes are pixel
    ``(x1, y1, x2, y2)`` and ``dropped`` counts faces excluded as too small or
    flagged invalid.
    """
    if not path.is_file():
        raise SystemExit(f"wider annotations not found: {path}")
    # utf-8-sig transparently drops a byte-order mark, which would otherwise make
    # the first image line fail its digit check and silently drop that whole block.
    text = path.read_text(encoding="utf-8-sig", errors="replace")

    images: list[tuple[str, list[tuple[float, float, float, float]], int]] = []
    current: str | None = None
    boxes: list[tuple[float, float, float, float]] = []
    dropped = 0
    expected: int | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line[0].isdigit() and "--" in line:
            if current is not None:
                images.append((current, boxes, dropped))
            current = line
            boxes, dropped, expected = [], 0, None
            continue

        if current is None:
            continue

        if expected is None:
            expected = int(line)
            continue

        if expected and len(boxes) + dropped >= expected:
            continue

        parts = line.split()
        # 4 flags (invalid blur pose occlusion) then 4 coords: 8 fields minimum.
        if len(parts) < 8:
            continue
        invalid, _blur, _pose, _occlusion = (int(p) for p in parts[:4])
        x1, y1, x2, y2 = (float(p) for p in parts[4:8])
        if invalid:
            dropped += 1
            continue
        boxes.append((x1, y1, x2, y2))

    if current is not None:
        images.append((current, boxes, dropped))
    return images


def collect_wider(
    annotations: Path,
    images_root: Path,
    min_face: int,
    include_invalid: bool,
) -> list[tuple[Path, list[tuple[float, float, float, float]]]]:
    """Resolve WIDER annotations to on-disk images with usable pixel boxes."""
    parsed = parse_wider(annotations)
    LOGGER.info("parsed %d annotated images from %s", len(parsed), annotations.name)

    out: list[tuple[Path, list[tuple[float, float, float, float]]]] = []
    missing = 0
    too_small = 0
    flagged = 0

    for name, boxes, dropped in parsed:
        # --invalid is dropped during parsing unless explicitly kept.
        if dropped and not include_invalid:
            flagged += dropped
        image_path = images_root / name
        if not image_path.is_file():
            missing += 1
            continue
        usable = []
        for x1, y1, x2, y2 in boxes:
            if min(x2 - x1, y2 - y1) < min_face:
                too_small += 1
                continue
            usable.append((x1, y1, x2, y2))
        if usable:
            out.append((image_path, usable))

    LOGGER.info(
        "usable images: %d (missing %d, faces too small %d, invalid flagged %d)",
        len(out), missing, too_small, flagged,
    )
    if not out:
        raise SystemExit(
            f"no usable images. Check --wider-images points at the image root "
            f"(the annotation paths are relative to it), e.g. {images_root}"
        )
    return out


def collect_yolo(root: Path) -> list[tuple[Path, list[tuple[float, float, float, float]]]]:
    """Collect an existing YOLO-format face dataset as pixel boxes.

    Boxes arrive normalised, so no pixel-size filter is applied here; those sets
    are already curated for usable face size.
    """
    found: list[tuple[Path, list[tuple[float, float, float, float]]]] = []
    for label_path in sorted(root.rglob("*.txt")):
        image_path = _sibling_image(label_path)
        if image_path is None:
            continue
        boxes: list[tuple[float, float, float, float]] = []
        for line in label_path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            cx, cy, w, h = (float(p) for p in parts[1:5])
            boxes.append(((cx - w / 2), (cy - h / 2), (cx + w / 2), (cy + h / 2)))
        if boxes:
            found.append((image_path, boxes))
    if not found:
        raise SystemExit(f"no image/label pairs found under {root}")
    LOGGER.info("found %d labelled images under %s", len(found), root)
    return found


def _sibling_image(label_path: Path) -> Path | None:
    """Find the image a label file belongs to, tolerating common extensions."""
    for extension in (".jpg", ".jpeg", ".png", ".bmp"):
        candidate = label_path.with_suffix(extension)
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    root = Path(args.dataset)
    images_dir = root / "images" / TRAIN
    labels_dir = root / "labels" / TRAIN
    metadata_path = root / "metadata.jsonl"

    if args.source == "wider":
        if not args.wider_annotations or not args.wider_images:
            LOGGER.error("--source wider needs --wider-annotations and --wider-images")
            return 2
        items = collect_wider(
            Path(args.wider_annotations),
            Path(args.wider_images),
            args.min_face,
            args.include_invalid,
        )
    else:
        if not args.yolo_dir:
            LOGGER.error("--source yolo needs --yolo-dir")
            return 2
        items = collect_yolo(Path(args.yolo_dir))

    rng = random.Random(args.seed)
    rng.shuffle(items)
    chosen = items[: args.max_images]
    if len(items) > len(chosen):
        LOGGER.info("sampling %d of %d images", len(chosen), len(items))

    total_faces = sum(len(boxes) for _, boxes in chosen)
    print(f"\nplan: copy {len(chosen)} images, {total_faces} faces, into {images_dir}")
    print(f"      every image gets the class {CLASS_ID} (face); identity is handled by enrolment")
    if args.dry_run:
        print("dry run - nothing written")
        return 0

    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    faces = 0
    skipped_no_image = 0
    for image_path, boxes in chosen:
        frame = cv2.imread(str(image_path))
        if frame is None:
            LOGGER.warning("unreadable image, skipping: %s", image_path)
            skipped_no_image += 1
            continue
        height, width = frame.shape[:2]

        stem = f"general_{TRAIN}_{image_path.stem}"
        try:
            shutil.copy2(image_path, images_dir / f"{stem}{image_path.suffix.lower()}")
        except OSError as exc:
            LOGGER.warning("cannot copy %s: %s", image_path.name, exc)
            skipped_no_image += 1
            continue

        lines = []
        for x1, y1, x2, y2 in boxes:
            lines.append(
                f"{CLASS_ID} {((x1 + x2) / 2) / width:.6f} {((y1 + y2) / 2) / height:.6f} "
                f"{(x2 - x1) / width:.6f} {(y2 - y1) / height:.6f}"
            )
        (labels_dir / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        append_metadata(
            metadata_path,
            {
                "file": f"images/{TRAIN}/{stem}{image_path.suffix.lower()}",
                "split": TRAIN,
                "condition": "general",
                "person": "other",
                "source": args.source,
                "source_file": image_path.name,
                "faces": len(boxes),
                "frame": [width, height],
            },
        )
        copied += 1
        faces += len(boxes)

    data_yaml = write_data_yaml(root)
    LOGGER.info("copied %d images / %d faces, wrote %s", copied, faces, data_yaml)
    if skipped_no_image:
        LOGGER.warning("%d image(s) skipped", skipped_no_image)

    print(f"\nadded {copied} images, {faces} faces")
    print("next: python tools/preview_labels.py --split train --count 20")
    print("      (general faces use tight WIDER boxes, unlike your padded captures -")
    print("       check a few before training so the two styles do not fight)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())