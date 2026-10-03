"""Montage labelled dataset frames so the boxes can be eyeballed before training.

Auto-labelled boxes are only as good as the detector that drew them. A wrong box
is worse than a missing one: it teaches the detector the wrong thing, quietly, and
the symptom only shows up later as poor detection on real faces. So this exists to
make the labels reviewable in one screen instead of frame by frame.

Each tile shows the image with its YOLO box drawn, cropped around the box with
context, plus the filename and box size. Tiles are laid out in a grid and written
to a PNG.

What to look for:
  - the box should span forehead to chin and ear to ear, not just the eyes
  - a box clipped hard against the image edge is padding hitting the frame border
  - a box much larger than the visible face is usually a second face's box
  - tiles with a red flag marker failed the aspect-ratio or off-frame checks

Usage:
    python tools/preview_labels.py --split train --count 20
    python tools/preview_labels.py --split train --count 40 --out review/train.png
    python tools/preview_labels.py --split holdout --count 20 --audit-only
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from dataset_meta import HOLDOUT, TRAIN, VAL  # noqa: E402

LOGGER = logging.getLogger("shield.preview_labels")

TILE = 320
COLUMNS = 5
FONT = cv2.FONT_HERSHEY_SIMPLEX

# A whole-face box is taller than it is wide; a box that is much wider than tall
# is usually wrong, and a box shorter than it is wide is usually a partial face.
ASPECT_MIN = 0.6
ASPECT_MAX = 2.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default="rpi/datasets/faces", help="dataset root")
    parser.add_argument("--split", default=TRAIN, choices=(TRAIN, VAL, HOLDOUT))
    parser.add_argument("--count", type=int, default=20, help="how many random frames to show")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed, for a reproducible sample")
    parser.add_argument("--out", default="", help="output PNG; defaults to review/<split>.png")
    parser.add_argument("--audit-only", action="store_true", help="report problems, write no image")
    return parser.parse_args()


def read_labels(path: Path) -> list[tuple[float, float, float, float]]:
    """Read one YOLO label file as normalised ``(cx, cy, w, h)`` tuples.

    Raises ``ValueError`` on a malformed line rather than skipping it. Skipping made
    a real dataset defect invisible: the capture tool was writing 4-field labels
    with no class id, and this reader discarded every one of them, so the audit
    reported "no label file / empty" for frames that in fact had labels. A loud
    failure naming the file and line is the only thing that gets this fixed.
    """
    if not path.is_file():
        raise FileNotFoundError(f"no label file for this image: {path.name}")
    boxes: list[tuple[float, float, float, float]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 5:
            raise ValueError(
                f"{path.name}:{number}: expected 5 fields "
                f"(class cx cy w h), got {len(parts)}: {line.strip()!r}"
            )
        try:
            _, cx, cy, w, h = (float(p) for p in parts)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number}: non-numeric field: {line.strip()!r}") from exc
        boxes.append((cx, cy, w, h))
    return boxes


def to_pixels(box: tuple[float, float, float, float], frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    cx, cy, w, h = box
    x = int(round((cx - w / 2) * frame_w))
    y = int(round((cy - h / 2) * frame_h))
    return x, y, int(round(w * frame_w)), int(round(h * frame_h))


def audit(boxes: list[tuple[float, float, float, float]], frame_w: int, frame_h: int) -> list[str]:
    """Return human-readable problems for one frame; empty means it looks sane."""
    problems: list[str] = []
    if not boxes:
        return ["no label file / empty"]

    for index, box in enumerate(boxes):
        cx, cy, w, h = box
        tag = f"box{index}"
        if not (0.0 <= w <= 1.0 and 0.0 <= h <= 1.0):
            problems.append(f"{tag} size out of range")
        if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
            problems.append(f"{tag} centre off image")
        if cx - w / 2 < -0.01 or cx + w / 2 > 1.01 or cy - h / 2 < -0.01 or cy + h / 2 > 1.01:
            problems.append(f"{tag} crosses image edge")
        if w <= 0 or h <= 0:
            problems.append(f"{tag} empty")
            continue

        px_w, px_h = w * frame_w, h * frame_h
        aspect = px_h / px_w if px_w else 0.0
        if aspect < ASPECT_MIN:
            problems.append(f"{tag} too wide (h/w={aspect:.2f}) - probably not a whole face")
        if aspect > ASPECT_MAX:
            problems.append(f"{tag} too tall (h/w={aspect:.2f}) - check for a second face merged in")
        if min(px_w, px_h) < 40:
            problems.append(f"{tag} small ({int(min(px_w, px_h))}px) - below useful resolution")
        if max(px_w, px_h) > 0.9 * max(frame_w, frame_h):
            problems.append(f"{tag} nearly fills the frame")

    return problems


def make_tile(image: np.ndarray, boxes: list[tuple[float, float, float, float]], name: str, problems: list[str]) -> np.ndarray:
    """Crop around the first box with context, draw all boxes, and caption the tile."""
    frame_h, frame_w = image.shape[:2]
    overlay = image.copy()
    colours = [(0, 255, 0), (0, 200, 255), (255, 128, 0)]

    for index, box in enumerate(boxes):
        x, y, w, h = to_pixels(box, frame_w, frame_h)
        colour = (0, 0, 255) if problems else colours[index % len(colours)]
        cv2.rectangle(overlay, (x, y), (x + w, y + h), colour, 3 if problems else 2)

    # Crop around the primary box with 60% context margin, so a mis-placed box is
    # judged against the visible face rather than the whole 720p frame.
    if boxes:
        bx, by, bw, bh = to_pixels(boxes[0], frame_w, frame_h)
        mx, my = int(bw * 0.6), int(bh * 0.6)
        x1 = max(0, bx - mx)
        y1 = max(0, by - my)
        x2 = min(frame_w, bx + bw + mx)
        y2 = min(frame_h, by + bh + my)
    else:
        x1, y1, x2, y2 = 0, 0, frame_w, frame_h
    crop = overlay[y1:y2, x1:x2]

    tile = np.full((TILE, TILE, 3), 32, dtype=np.uint8)
    ch, cw = crop.shape[:2]
    if ch and cw:
        scale = min(TILE / cw, (TILE - 34) / ch)
        resized = cv2.resize(crop, (max(1, int(cw * scale)), max(1, int(ch * scale))))
        rh, rw = resized.shape[:2]
        tile[2 : 2 + rh, (TILE - rw) // 2 : (TILE - rw) // 2 + rw] = resized

    caption = name if len(name) <= 26 else name[:23] + "..."
    cv2.putText(tile, caption, (4, TILE - 18), FONT, 0.38, (220, 220, 220), 1, cv2.LINE_AA)
    detail = f"{len(boxes)} box" + ("" if len(boxes) == 1 else "es")
    cv2.putText(tile, detail, (4, TILE - 5), FONT, 0.36, (160, 160, 160), 1, cv2.LINE_AA)

    if problems:
        cv2.rectangle(tile, (0, 0), (TILE - 1, 1), (0, 0, 255), -1)
        cv2.putText(tile, "FLAG", (TILE - 52, 16), FONT, 0.44, (0, 0, 255), 2, cv2.LINE_AA)

    return tile


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    root = Path(args.dataset)
    images_dir = root / "images" / args.split
    labels_dir = root / "labels" / args.split
    if not images_dir.is_dir():
        LOGGER.error("no images in %s", images_dir)
        return 2

    paths = sorted(images_dir.glob("*.jpg"))
    if not paths:
        LOGGER.error("no .jpg frames in %s", images_dir)
        return 2

    orphan_labels = sorted(p.stem for p in labels_dir.glob("*.txt")) if labels_dir.is_dir() else []
    unlabelled = [p.name for p in paths if p.stem not in set(orphan_labels)]
    if unlabelled:
        LOGGER.warning(
            "%d/%d frame(s) have no label file and would be ignored by training: %s",
            len(unlabelled), len(paths), ", ".join(unlabelled[:5]),
        )

    rng = random.Random(args.seed)
    sample = rng.sample(paths, min(args.count, len(paths)))

    tiles: list[np.ndarray] = []
    flagged = 0
    for path in sample:
        image = cv2.imread(str(path))
        if image is None:
            LOGGER.warning("unreadable, skipping: %s", path.name)
            continue
        try:
            boxes = read_labels(labels_dir / f"{path.stem}.txt")
        except (ValueError, FileNotFoundError) as exc:
            LOGGER.error("%s", exc)
            flagged += 1
            tiles.append(make_tile(image, [], path.name, [str(exc)]))
            continue
        problems = audit(boxes, image.shape[1], image.shape[0])
        if problems:
            flagged += 1
            LOGGER.warning("%s: %s", path.name, "; ".join(problems))
        tiles.append(make_tile(image, boxes, path.name, problems))

    print(f"\nsampled {len(tiles)}/{len(paths)} frames from {args.split}")
    print(f"flagged {flagged}")
    if flagged:
        print("  fix or delete flagged frames before training; see PLAN.md phase 1")

    if args.audit_only or not tiles:
        return 1 if flagged else 0

    rows = (len(tiles) + COLUMNS - 1) // COLUMNS
    sheet = np.full((rows * TILE, COLUMNS * TILE, 3), 18, dtype=np.uint8)
    for index, tile in enumerate(tiles):
        row, column = divmod(index, COLUMNS)
        sheet[row * TILE : (row + 1) * TILE, column * TILE : (column + 1) * TILE] = tile

    out = Path(args.out) if args.out else Path("review") / f"{args.split}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), sheet)
    print(f"wrote {out}")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())