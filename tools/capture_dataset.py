"""Capture webcam frames into a YOLO-format face dataset.

Phase 1 needs ~250 frames of your own face at the resolution the camera actually
produces, in varied light, distance and pose, so the detector fits the real webcam
rather than a benchmark.

**Capture conditions.** Vary these deliberately - a detector trained only on flat
frontal light will miss an intruder standing in a doorway lit from the side:

    lighting   front lamp, dim, side light
    distance   0.5 m, 1 m, 2 m
    pose       head turned left / right, up / down
    glasses    with and without, if you wear them

Pass the condition as ``--condition`` so every frame is tagged in
``metadata.jsonl``; a weak class can then be traced to a condition instead of
guessed at. Vary lighting, distance and head angle roughly evenly across the set -
front-light-only data is the single most common way this dataset goes wrong.

**Splits.** ``--split train`` is what the detector learns from. ``--split holdout`` and
``--split holdout2`` are never trained on and exist solely to test the gate;
``data.yaml`` deliberately does not reference either, so a model cannot accidentally
validate on the gate set. ``holdout-hard`` is **not** offered: it is the fixed
regression witness, and capturing into it would dilute the one split that must never
change (D-038).
``--split strangers`` is the third kind of data and is different from both: it is the
set of people who are **not** enrolled, used only to measure whether the detector finds
faces it was never taught to recognise. It has no labels, is written to its own tree
(``rpi/datasets/strangers/<person>/``) and never reaches ``data.yaml``.

**Strangers are captured unfiltered.** Every ``--interval`` seconds is saved, whether or
not anything is detected in the frame. This is deliberate and is the whole point of the
split: the gate asks whether the detector finds these faces, so dropping the frames the
detector cannot find would delete exactly the evidence the gate is looking for and make
the result circular. There is also no ``s`` key here - capture is automatic and paced,
because a hand-picked set is a curated set and a curated set flatters the detector.
The detector box drawn on the preview is *feedback only*, so you can see whether a frame
is worth keeping before the run is over; it never decides what is written.

Each stranger photo is assumed to contain exactly one real face. That assumption is the
gate's, and a photo with two faces would be scored as a single detection - so one
person per frame, same as the training captures.

**Cameras.** The camera used is recorded in ``metadata.jsonl`` for every frame, so a
weak class can be traced to a sensor as well as to a condition. See the camera note
in ``rpi/shield_rpi/config.yaml`` for which device is currently selected.

Re-running a condition continues its numbering rather than restarting at zero, so a
second session adds frames instead of overwriting the first one's images and labels.

Faces are auto-annotated with ``face_recognition.face_locations``, whose default
model is **HOG** and whose box is the *inner* face - brow to nose, cheek to cheek.
That box is then padded by ``--pad`` and clipped to the frame. With the default
``--annotator yolo`` the box comes from the project's own pretrained detector, which
already covers the whole face and so takes ``--pad 0``; with ``--annotator dlib`` the
HOG box is an inner-face box and takes the 25% padding that restores
forehead-to-chin, ear-to-ear coverage. Training on an inner-face box produces crops
missing the jawline and hairline, which are exactly the features that separate one
person from another. Verified against the 68-point landmark extent on the built-in
camera - the padded box contained it on both axes in every frame - and pinned by
``test_pad_box_covers_landmark_extent``. Review the boxes with
``tools/preview_labels.py`` before training regardless.

Usage:
    python tools/capture_dataset.py --split train --person imman --condition front-light
    python tools/capture_dataset.py --split train --person imman --condition dim --count 40
    python tools/capture_dataset.py --split holdout --person imman --condition front-light
    python tools/capture_dataset.py --split strangers --person friend1 --condition front-light --count 20

Keys while capturing (train / val / holdout):
    s  save the frame (auto-detected face required)
    n  skip this frame
    q  quit

Keys while capturing strangers:
    space  pause / resume
    q  quit
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import face_recognition
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from dataset_meta import (  # noqa: E402
    CAPTUREABLE_SPLITS,
    CONDITIONS,
    FACE_CLASS_ID,
    GATE_SPLITS,
    HOLDOUT_HARD,
    STRANGERS,
    MetadataCollisionError,
    assert_unique_basenames,
    highest_frame_index,
    read_metadata_rows,
    split_summary_lines,
    summarise,
)
from dataset_meta import (
    append_metadata as append_metadata_record,
)
from dataset_meta import (
    write_data_yaml as write_data_yaml_file,
)
from holdout_test import best_face_box  # noqa: E402
from shield_rpi.camera import Camera, CameraError  # noqa: E402
from shield_rpi.config import CameraConfig, ConfigError, load_camera_config  # noqa: E402

LOGGER = logging.getLogger("shield.capture_dataset")

PREVIEW_WINDOW = "capture dataset"
COUNTDOWN_S = 3

# Mirrors enroll_laptop.MAX_FACE_FRACTION, so a face is held to the same upper
# bound in the dataset as it is at enrolment. The Brio's narrower lens is what
# makes this necessary: at a comfortable desk distance the face fills ~77% of the
# frame height, which is a tighter crop than the recognition gate can score well
# (D-029/D-031). A dataset built from those frames inherits the same problem.
MAX_FACE_FRACTION = 0.45

# Width the live preview detects at. HOG cost scales with pixel count - measured
# at 2686 ms on a 1920x1080 frame against 144 ms at 640x360 - so without this the
# loop runs at 0.4 fps and, worse, samples the keyboard only twice a second, which
# silently drops the S keypresses that save a frame. 640 matches the detector's
# own imgsz. Labels are unaffected: a save re-detects at native resolution.
PREVIEW_DETECT_WIDTH = 640


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="rpi/datasets/faces", help="dataset root directory")
    parser.add_argument("--count", type=int, default=250, help="target number of saved frames")
    parser.add_argument(
        "--split",
        choices=(*CAPTUREABLE_SPLITS, STRANGERS),
        default="train",
        help=(
            "train: what the detector learns from. "
            "val: tuning, still trained-on-adjacent. "
            "holdout / holdout2: NEVER trained on, reserved for the gate test. "
            f"{HOLDOUT_HARD} is deliberately not offered: it is a fixed regression "
            "witness and capturing into it would dilute the one split that must "
            "never change. "
            "strangers: people who are NOT enrolled, unlabelled and unfiltered, used "
            "only to measure whether the detector finds faces it never learned"
        ),
    )
    parser.add_argument(
        "--condition",
        default="unspecified",
        choices=("unspecified", *CONDITIONS),
        help="capture condition, recorded in metadata.json",
    )
    parser.add_argument(
        "--person",
        default="unlabelled",
        help="who is in frame, recorded in metadata.json",
    )
    parser.add_argument(
        "--camera-note",
        default="",
        help="free-text camera identification recorded in metadata.json",
    )
    parser.add_argument(
        "--config",
        default=str(Path("rpi") / "shield_rpi" / "config.yaml"),
        help="camera device/geometry/fourcc come from here; nothing is hardcoded",
    )
    parser.add_argument(
        "--min-face",
        type=int,
        default=60,
        help="smallest acceptable padded face, px in the shorter edge",
    )
    parser.add_argument(
        "--max-face",
        type=float,
        default=MAX_FACE_FRACTION,
        help="largest acceptable padded face, as a fraction of the frame's shorter "
        "edge (0.45 = 45%%). Refuses over-tight crops, which score poorly at the "
        "recognition gate; set to 1.0 to disable",
    )
    parser.add_argument(
        "--detect-width",
        type=int,
        default=0,
        help="dlib annotator only: width the frame is downscaled to before "
        "detection, boxes rescaled back. 0 (default) detects at native "
        "resolution, which is slow at 1080p but exact",
    )
    parser.add_argument(
        "--annotator",
        choices=("yolo", "dlib"),
        default="yolo",
        help="which detector writes the labels. yolo (default) is the project's "
        "own pretrained model at --imgsz - the same detector the gate scores "
        "with, and ~20x cheaper at 1080p. dlib reproduces the legacy labels",
    )
    parser.add_argument(
        "--imgsz",
        type=int,
        default=PREVIEW_DETECT_WIDTH,
        help="yolo annotator only: detector input size (640 = as trained)",
    )
    parser.add_argument(
        "--reject-dir",
        default=str(Path("rpi") / "datasets" / "_rejected"),
        help="where refused frames are dumped, with the reason, box and luma in "
        "the filename",
    )
    parser.add_argument(
        "--pad",
        type=float,
        default=None,
        help=(
            "pad each detected box by this fraction of its own size "
            "so it covers forehead-to-chin and ear-to-ear rather than "
            "the inner face (0.25 = 25%%). Defaults to 25%% for "
            "--annotator dlib, whose HOG box is an inner-face box, and 0%% "
            "for --annotator yolo, whose box already covers the whole face"
        ),
    )
    parser.add_argument(
        "--review", action="store_true", help="disable auto-save; press 's' to keep each frame"
    )
    parser.add_argument(
        "--strangers-out",
        default=str(Path("rpi") / "datasets" / STRANGERS),
        help="stranger set root; one subdirectory per --person",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="seconds between saved stranger frames (strangers split only)",
    )
    parser.add_argument(
        "--countdown",
        type=float,
        default=float(COUNTDOWN_S),
        help="seconds to wait before the first stranger frame is saved",
    )
    parser.add_argument(
        "--face-model",
        default=str(Path("rpi") / "models" / "face_pretrained.pt"),
        help=(
            "detector whose box is drawn on the stranger preview. Feedback only: it "
            "never decides whether a frame is saved, so a missing model is a warning "
            "rather than an error"
        ),
    )
    parser.add_argument(
        "--preview-conf",
        type=float,
        default=0.25,
        help="confidence floor for the preview box; low so near-misses are visible",
    )
    parser.add_argument(
        "--preview-imgsz", type=int, default=640, help="detector input size for the preview"
    )
    return parser.parse_args()


def open_camera(camera_config: CameraConfig) -> Camera:
    """Open the configured camera, negotiating geometry exactly as the app does."""
    camera = Camera(camera_config)
    try:
        info = camera.open()
    except CameraError:
        LOGGER.error(
            "cannot open camera device %d; run tools/camera_check.py to list devices",
            camera_config.device,
        )
        raise
    LOGGER.info(
        "camera %d open at %s (requested %dx%d, fourcc %s)",
        camera_config.device,
        info,
        camera_config.width,
        camera_config.height,
        camera_config.fourcc,
    )
    return camera


def pad_box(
    box: tuple[int, int, int, int],
    frame_w: int,
    frame_h: int,
    pad_ratio: float,
) -> tuple[int, int, int, int]:
    """Grow a face box by ``pad_ratio`` of its own size, clipped to the frame.

    Face detectors consistently return the *inner* face - brow ridge to just under
    the nose, cheek to cheek. A YOLO box trained on that teaches the detector to
    crop faces the same way, and the recognition embedding then comes from a crop
    missing the forehead, chin and jawline - exactly the features that distinguish
    one person from another. Padding by a fraction of the box size restores
    forehead-to-chin, ear-to-ear coverage. Clipping keeps the box on-image so the
    normalised label never goes negative or exceeds 1.0.
    """
    x, y, w, h = box
    pad_w = w * pad_ratio
    pad_h = h * pad_ratio

    x1 = max(0, int(round(x - pad_w)))
    y1 = max(0, int(round(y - pad_h)))
    x2 = min(frame_w, int(round(x + w + pad_w)))
    y2 = min(frame_h, int(round(y + h + pad_h)))

    return x1, y1, max(0, x2 - x1), max(0, y2 - y1)


def detect_faces(
    rgb: cv2.MatLike,
    frame_w: int,
    frame_h: int,
    pad_ratio: float,
    detect_width: int = 0,
) -> list[tuple[int, int, int, int]]:
    """Return padded ``(x, y, w, h)`` boxes for faces in one RGB frame.

    Uses ``face_recognition.face_locations``. Its default model is **HOG**, not
    CNN: the call below does not pass ``model="cnn"``, so what returns is the fast
    HOG inner-face box - brow ridge to just under the nose, cheek to cheek.
    ``number_of_times_to_upsample=1`` is the default and is stated explicitly only
    to pin it down if the library default ever changes.

    That inner box is the reason ``pad_ratio`` is not optional. The HOG box is
    accurate about position but cropped tight, so the crop it defines is missing
    forehead, chin and jawline - exactly the features that tell one person from
    another, and the reason the earlier dlib-HOG dataset taught the detector to
    frame only the eyes (D-020). ``pad_box`` grows the box by its own size to
    restore forehead-to-chin, ear-to-ear coverage. On the built-in camera this
    turns a raw 223x223 HOG box into a labelled 335x335 box.

    The same detector is used at enrolment and test time, so dataset boxes and
    recognition crops agree on what a face is. Passing ``model="cnn"`` would give
    tighter, more accurate boxes but costs seconds per frame, which is too slow for
    the interactive capture loop; revisit if the padded boxes prove inadequate.

    ``detect_width`` downscales before detection and rescales the boxes back, which
    is what makes the loop usable at 1080p: HOG cost scales with pixel count, so a
    1920x1080 frame measures 2686 ms against 144 ms at 640x360 (D-032). Boxes are
    returned in full-resolution coordinates either way, so callers do not change.
    Set it to 0 to detect at native resolution.
    """
    if detect_width and 0 < detect_width < frame_w:
        scale = frame_w / detect_width
        small = cv2.resize(rgb, (detect_width, max(1, int(round(frame_h / scale)))))
        locations = face_recognition.face_locations(
            small, number_of_times_to_upsample=1
        )
        locations = [
            (top * scale, right * scale, bottom * scale, left * scale)
            for top, right, bottom, left in locations
        ]
    else:
        locations = face_recognition.face_locations(
            rgb, number_of_times_to_upsample=1
        )
    return [
        pad_box((left, top, right - left, bottom - top), frame_w, frame_h, pad_ratio)
        for top, right, bottom, left in locations
    ]


class FaceDetector:
    """A face detector the capture loop can ask for boxes.

    One method, so the box drawn on the preview and the box written to the label
    cannot come from different detectors, different input sizes, or different
    frames. That divergence was the bug behind a preview that said "ready - press S"
    while the save silently refused the frame (D-033).

    Implementations receive the frame in **BGR**, the format the camera delivers.
    Each detector owns its own colour space from there: ultralytics reads BGR
    directly, dlib needs RGB. Converting once at the boundary is what keeps a
    channel swap from quietly degrading one of them.
    """

    description = "none"

    def boxes_for(self, frame_bgr: np.ndarray, frame_w: int, frame_h: int) -> list:
        raise NotImplementedError

    def padded_boxes_for(
        self, frame_bgr: np.ndarray, frame_w: int, frame_h: int, pad_ratio: float
    ) -> list:
        return [
            pad_box(box, frame_w, frame_h, pad_ratio)
            for box in self.boxes_for(frame_bgr, frame_w, frame_h)
        ]


class YoloFaceDetector(FaceDetector):
    """The project's own pretrained detector, at the size it was trained for.

    This is the detector the gate scores with, so using it to write the labels is
    what makes the dataset and the evaluation agree on what a face is. It is also
    an order of magnitude cheaper than dlib on a 1080p frame - measured 118 ms
    against 2686 ms - which is what makes an honest single pass possible (D-033).
    """

    def __init__(
        self, model: Any, imgsz: int = PREVIEW_DETECT_WIDTH, name: str = "yolo"
    ) -> None:
        self._model = model
        self.imgsz = imgsz
        # The name is passed in rather than read off the model: an ultralytics
        # YOLO instance does not expose the path it was loaded from, and the whole
        # point of this string is to say out loud which weights are in play (D-033).
        self.description = f"{name} (ultralytics YOLO) imgsz={imgsz}"

    def boxes_for(self, frame_bgr: np.ndarray, frame_w: int, frame_h: int) -> list:
        # BGR, as ultralytics expects. Passing RGB here still returns boxes, which
        # is why the mistake survives: the channels are simply swapped and the
        # detector reports a worse face rather than failing (D-033).
        result = self._model.predict(frame_bgr, imgsz=self.imgsz, verbose=False)[0]
        boxes = []
        # ultralytics reports xyxy as (x1, y1, x2, y2). Unpacking it in any other
        # order still yields four plausible numbers, so the mistake produces a
        # garbled rectangle rather than an error - and a garbled rectangle passes
        # the size bounds often enough to look like a face (D-033).
        for x1, y1, x2, y2 in result.boxes.xyxy.cpu().tolist():
            # Clamp into the frame: a label normalised against a box that hangs off
            # the edge encodes a centre and size that do not describe the image.
            left = max(0, min(int(round(x1)), frame_w))
            top = max(0, min(int(round(y1)), frame_h))
            right = max(0, min(int(round(x2)), frame_w))
            bottom = max(0, min(int(round(y2)), frame_h))
            boxes.append((left, top, right - left, bottom - top))
        return boxes


class DlibFaceDetector(FaceDetector):
    """dlib HOG inner-face boxes, padded.

    Kept so the existing dataset can be reproduced label-for-label, not because it
    is the better annotator. Its HOG box is brow-to-nose and cheek-to-cheek, which
    is why ``pad_ratio`` is not optional here (D-020), and its cost scales with
    pixel count, which is what made 1080p capture unusable (D-032).
    """

    def __init__(self, detect_width: int = 0) -> None:
        self.detect_width = detect_width
        self.imgsz = detect_width or 0
        suffix = f"detect_width={detect_width}" if detect_width else "native 1:1"
        self.description = f"dlib HOG + pad ({suffix})"

    def boxes_for(self, frame_bgr: np.ndarray, frame_w: int, frame_h: int) -> list:
        # dlib's face_locations wants RGB, the opposite of ultralytics.
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        return detect_faces(rgb, frame_w, frame_h, 0.0, self.detect_width)


@dataclass(frozen=True)
class Assessment:
    """One frame's capture decision, computed once.

    The preview and the save both read this object, so the status line cannot
    promise a save the save path then refuses.
    """

    frame: cv2.MatLike
    boxes: tuple
    usable: tuple
    best: tuple | None
    reason: str
    colour: tuple
    luma: int
    detector: str

    @property
    def accepted(self) -> bool:
        return self.best is not None


def assess_frame(
    frame: cv2.MatLike,
    detector: FaceDetector,
    min_face: int,
    max_fraction: float,
    pad_ratio: float,
) -> Assessment:
    """Decide whether ``frame`` can be saved. The only place that decision is made."""
    frame_h, frame_w = frame.shape[:2]
    boxes = tuple(detector.padded_boxes_for(frame, frame_w, frame_h, pad_ratio))
    usable = tuple(
        usable_boxes(boxes, min_face, max_fraction * min(frame_w, frame_h))
    )
    best = max(usable, key=lambda box: box[2] * box[3]) if usable else None
    reason, colour = reason_no_face(
        boxes, min_face, frame_w, frame_h, max_fraction
    )
    return Assessment(
        frame=frame,
        boxes=boxes,
        usable=usable,
        best=best,
        reason=reason,
        colour=colour,
        luma=frame_luma(frame),
        detector=detector.description,
    )


def frame_luma(frame: cv2.MatLike) -> int:
    """Mean luma, 0-255.

    Recorded on every accepted and rejected frame. Scene brightness is the variable
    that most often decides whether a face is detectable at all, so a rejected
    frame without it cannot be diagnosed after the fact (D-028).
    """
    return int(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())


def write_rejected_frame(assessment: Assessment, reject_dir: Path, sequence: int) -> Path | None:
    """Save a refused frame with its reason, box and luma in the filename.

    A refusal the operator cannot inspect is indistinguishable from a refusal they
    caused by moving. Every rejected save lands here so the next run can say which
    it was (D-033).
    """
    try:
        reject_dir.mkdir(parents=True, exist_ok=True)
        box = assessment.best or next(iter(assessment.boxes), None)
        geometry = (
            f"box{box[0]}x{box[1]}_{box[2]}x{box[3]}" if box is not None else "nobox"
        )
        slug = assessment.reason.split("(")[0].strip().replace(" ", "_")[:40]
        path = reject_dir / f"{sequence:04d}_{slug}_{geometry}_luma{assessment.luma}.jpg"
        cv2.imwrite(str(path), assessment.frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return path
    except Exception as exc:  # noqa: BLE001 - diagnostics must never block a capture
        LOGGER.warning("could not write rejected frame: %s", exc)
        return None


REASON_HOLD_MS = 1000


def show_reason_for(
    reason: str, hold_ms: int, preview: cv2.MatLike | None = None
) -> None:
    """Hold a refusal on screen long enough to read it.

    At 8 fps a one-frame status line is gone before it can be read, and a refusal
    nobody can read is a refusal nobody can act on - which is how a save can fail
    twenty times for reasons nobody noticed (D-033). The reason is stamped onto
    the frame rather than left to the HUD's status line, so it cannot be
    overwritten by the next iteration's "ready".
    """
    if preview is None:
        deadline = time.monotonic() + hold_ms / 1000.0
        while time.monotonic() < deadline and window_is_open():
            if cv2.waitKey(10) & 0xFF in (ord("q"), ord("Q"), 27):
                return
        return

    held = preview.copy()
    cv2.rectangle(held, (0, held.shape[0] - 44), (held.shape[1], held.shape[0]), (0, 0, 0), -1)
    cv2.putText(
        held,
        f"SAVE REFUSED: {reason}",
        (10, held.shape[0] - 14),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 140, 255),
        2,
    )
    deadline = time.monotonic() + hold_ms / 1000.0
    while time.monotonic() < deadline:
        if not window_is_open():
            return
        cv2.imshow(PREVIEW_WINDOW, held)
        if cv2.waitKey(10) & 0xFF in (ord("q"), ord("Q"), 27):
            return


def build_annotator(annotator: str, args: argparse.Namespace) -> FaceDetector:
    """Build the detector that writes the labels, and say which one it is.

    The identity of this detector is logged rather than assumed, because the
    preview and the gate disagreed about which model was in play and nobody could
    tell from the output (D-033).
    """
    if annotator == "dlib":
        LOGGER.warning(
            "--annotator dlib: labels will be written by dlib HOG, not by the "
            "model the gate scores with. Use this only to reproduce the existing "
            "dataset."
        )
        return DlibFaceDetector(args.detect_width)

    model_path = Path(args.face_model)
    if not model_path.is_file():
        raise SystemExit(
            f"no detector at {model_path}. Train one, or pass --face-model, or use "
            "--annotator dlib to label with dlib instead."
        )
    from ultralytics import YOLO

    return YoloFaceDetector(YOLO(str(model_path)), args.imgsz, model_path.name)


def usable_boxes(
    boxes: list[tuple[int, int, int, int]], min_face: int, max_face_px: float
) -> list[tuple[int, int, int, int]]:
    """Return the boxes that can be saved: both edges at least ``min_face``, neither over.

    Both bounds are checked against the padded box in full-resolution pixels, so the
    answer does not depend on the resolution detection happened to run at (D-032).
    """
    return [
        box
        for box in boxes
        if box[2] >= min_face
        and box[3] >= min_face
        and min(box[2], box[3]) <= max_face_px
    ]


def reason_no_face(
    raw_boxes: list[tuple[int, int, int, int]],
    min_face: int,
    frame_w: int | None = None,
    frame_h: int | None = None,
    max_fraction: float = MAX_FACE_FRACTION,
) -> tuple[str, tuple[int, int, int]]:
    """Explain, on screen, whether this frame can be saved.

    Distinguishes "no face here" from "face found but too small", so a frozen
    counter reads as an instruction (move closer) rather than a mystery. Reports
    "ready" when a box does qualify, so the message is truthful regardless of
    what the caller assumed about the boxes.

    A face that is too *large* is refused for the same reason a too-small one is:
    an over-tight crop loses the context that separates one face from another, and
    on the Brio that is the default outcome rather than a mistake. The frame
    dimensions are optional so callers that only want the lower bound (and the
    existing tests) can omit them.
    """
    if not raw_boxes:
        return "no face detected - move into frame", (0, 140, 255)

    # Mirrors the acceptance test in main(): both edges must reach min_face, so
    # report the box that came closest and the edge that fell short.
    best = max(raw_boxes, key=lambda box: min(box[2], box[3]))
    best_edge = int(min(best[2], best[3]))
    if best_edge < min_face:
        return (
            f"face too small ({best_edge}px < {min_face}px) - move closer",
            (0, 140, 255),
        )

    if frame_w is not None and frame_h is not None:
        limit = max_fraction * min(frame_w, frame_h)
        if best_edge > limit:
            return (
                f"face too large ({best_edge}px > {limit:.0f}px = "
                f"{max_fraction:.0%} of frame) - move back",
                (0, 140, 255),
            )
    return "ready - press S to save", (0, 255, 0)


def draw_box(
    preview: cv2.MatLike,
    box: tuple[int, int, int, int],
    label: str = "",
    colour: tuple[int, int, int] = (0, 255, 0),
) -> None:
    """Draw one face box with an optional corner label onto the preview."""
    x, y, w, h = box
    cv2.rectangle(preview, (x, y), (x + w, y + h), colour, 2)
    if label:
        cv2.putText(preview, label, (x, max(y - 6, 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)


def write_yolo_label(
    path: Path, box: tuple[int, int, int, int], frame_w: int, frame_h: int
) -> None:
    """Write one YOLO detection label: ``class cx cy w h`` normalised to 0..1.

    The leading class id is mandatory. A 4-field line is silently *misread* rather
    than rejected by the trainer: YOLO shifts the columns and treats the centre-x
    as a class index, so every box lands somewhere arbitrary instead of failing
    loudly. `tools/preview_labels.py` exists to catch exactly that, which is how
    the omission was found - it reported "no label file / empty" for frames whose
    labels were present but the wrong width.
    """
    x, y, w, h = box
    path.write_text(
        f"{FACE_CLASS_ID} {(x + w / 2) / frame_w:.6f} {(y + h / 2) / frame_h:.6f} "
        f"{w / frame_w:.6f} {h / frame_h:.6f}\n",
        encoding="utf-8",
    )


def append_metadata(path: Path, record: dict[str, object]) -> None:
    """Append one capture record to ``metadata.jsonl``."""
    append_metadata_record(path, record)


def write_data_yaml(root: Path) -> Path:
    """Write ``data.yaml`` with the holdout split omitted."""
    return write_data_yaml_file(root)


# -- strangers -------------------------------------------------------------
#
# Everything below captures the set of people who are *not* enrolled. It shares the
# camera, the config file and the geometry helpers with the YOLO captures above, and
# nothing else: no labels, no data.yaml, and - the point of the whole split - no
# filtering on what the detector can see.


def draw_hud(
    preview: cv2.MatLike,
    lines: list[str],
    status_colour: tuple[int, int, int] = (255, 255, 255),
    status_index: int = 1,
) -> None:
    """Draw a text HUD onto the preview, one line per entry.

    The dark backing behind each line keeps the text legible over any lighting
    condition, which matters here because half of these captures are deliberately dim.
    """
    for index, line in enumerate(lines):
        y = 30 + index * 28
        cv2.rectangle(preview, (0, y - 22), (760, y + 8), (0, 0, 0), -1)
        cv2.putText(
            preview,
            line,
            (10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            status_colour if index == status_index else (255, 255, 255),
            2,
        )


def stranger_stem(person: str, condition: str, index: int) -> str:
    """Filename stem for one stranger frame. The person leads, so a sorted listing of
    the directory groups each person's frames together without opening anything."""
    return f"{person}_{condition}_{index:04d}"


def stranger_due(saved_at: float | None, now: float, interval: float) -> bool:
    """True when a stranger frame may be saved now.

    The first frame of a run is always due: nothing has been saved yet, so there is no
    previous timestamp to measure from, and waiting would only lose the first frame.
    After that the rule is wall-clock, not frame-count - a camera that drops frames must
    slow the save rate down, not silently speed it up.

    Negative or zero ``interval`` is treated as "as fast as the camera delivers". The
    default of 0.5 s is the deliberate choice: 20 frames at 0.5 s is 10 s of a person
    standing there, and successive frames are near-identical, so a faster rate buys
    correlated duplicates rather than more evidence.
    """
    if saved_at is None:
        return True
    if interval <= 0:
        return True
    return (now - saved_at) >= interval


def stranger_hud_lines(
    saved: int,
    count: int,
    person: str,
    condition: str,
    interval: float,
    paused: bool,
    detected: bool,
    undetected: int,
) -> list[str]:
    """The preview overlay for a stranger capture.

    ``undetected`` is reported and *never* acted on. It is the honest framing of the
    split: the operator can see that the detector currently misses some of these faces
    and decide to re-shoot, without the tool quietly deleting the frames the gate needs
    to find.
    """
    box = "face found" if detected else "no face found (frame kept anyway)"
    return [
        f"STRANGERS SAVED {saved}/{count}",
        "PAUSED - press SPACE to resume" if paused else f"saving every {interval:g}s",
        f"person: {person}   condition: {condition}",
        f"preview box: {box}",
        f"frames with no box: {undetected} (kept - the gate needs them)",
        "SPACE = pause / resume    Q / Esc = quit",
    ]


def stranger_record(
    relative_file: str,
    person: str,
    condition: str,
    device: int,
    requested: tuple[int, int],
    actual: tuple[int, int],
    fourcc: str,
    fps: float,
    matches_request: bool,
    frame_w: int,
    frame_h: int,
    camera_note: str,
    preview_box: tuple[int, int, int, int] | None,
) -> dict[str, object]:
    """Build the ``metadata.jsonl`` row for one saved stranger frame.

    Camera index, delivered resolution and the free-text note are recorded for the same
    reason they are on the training frames: a domain shift that only shows up on
    strangers - a different webcam, a resolution the detector never saw - has to be
    traceable to its cause rather than guessed at. ``requested`` is kept alongside
    ``actual`` because a camera that quietly returns a different size is exactly the
    kind of thing that reads as "the model got worse".

    ``preview_box`` is stored as provenance, explicitly named so it cannot later be
    mistaken for a label. No label file is written for this split.
    """
    return {
        "file": relative_file,
        "split": STRANGERS,
        "person": person,
        "condition": condition,
        "device": device,
        "requested": list(requested),
        "actual": list(actual),
        "actual_fourcc": fourcc,
        "fps": round(fps, 1),
        "geometry_matches_request": matches_request,
        "camera_note": camera_note,
        "frame": [frame_w, frame_h],
        # Feedback only, written for traceability. Nothing filters on this.
        "preview_box": list(preview_box) if preview_box else None,
        "labelled": False,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }


def open_preview_detector(model_path: Path) -> object | None:
    """Load the detector used *only* to draw a box on the preview.

    Warn-and-continue, unlike every other model load in these tools, because this model
    decides nothing. The box is feedback so the operator can see whether a frame is
    worth keeping; refusing to capture because the overlay is missing would turn an
    optional convenience into a blocker, and the likely response to that under time
    pressure would be to capture fewer conditions rather than fewer frames.
    """
    if not model_path.is_file():
        LOGGER.warning(
            "no preview detector at %s - capturing without the feedback box. "
            "Frames are still saved unfiltered; run --face-model to see the box.",
            model_path,
        )
        return None
    try:
        from ultralytics import YOLO

        detector = YOLO(str(model_path))
    except Exception as exc:  # noqa: BLE001 - the overlay must never block a capture
        LOGGER.warning("preview detector %s failed to load (%s) - continuing without it", model_path, exc)
        return None
    LOGGER.info(
        "preview detector %s loaded: feedback only, no frame is filtered on its verdict",
        model_path.name,
    )
    return detector


def window_is_open() -> bool:
    """True while the preview window still exists.

    Needed because this loop saves on a timer instead of on a keypress. Without it,
    closing the window leaves the loop spinning and quietly writing hundreds of frames
    of an empty room until ``--count`` is reached.
    """
    try:
        return cv2.getWindowProperty(PREVIEW_WINDOW, cv2.WND_PROP_VISIBLE) >= 1
    except cv2.error:
        return False


def run_countdown(camera: Camera, seconds: float) -> bool:
    """Show a live countdown before the first stranger frame is saved.

    Returns False if the operator quit or closed the window, which aborts the run.

    Without this the camera opens and saves whatever happens to be in frame, which for
    a stranger run is reliably the back of the operator's head walking into position -
    so the opening frames of every condition would be the least useful ones in the set.
    Frames keep being read during the countdown so the camera's buffer does not fill.
    """
    if seconds <= 0:
        return True
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        frame = camera.read_or_none()
        if frame is None:
            continue
        preview = frame.copy()
        draw_hud(
            preview,
            [
                f"STRANGERS - get into frame in {remaining:.1f}s",
                "nothing is saved until the countdown ends",
                f"person: {PREVIEW_PERSON[0]}   condition: {PREVIEW_CONDITION[0]}",
            ],
            status_colour=(0, 200, 255),
        )
        cv2.putText(
            preview,
            str(int(remaining) + 1),
            (preview.shape[1] - 70, 90),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (0, 200, 255),
            3,
        )
        cv2.imshow(PREVIEW_WINDOW, preview)
        if (cv2.waitKey(30) & 0xFF) in (ord("q"), 27) or not window_is_open():
            return False


# The countdown HUD needs the person and condition, and run_countdown is called from
# capture_strangers where they are known. Passing them through would add two parameters
# for one line of text; these are set immediately before the call.
PREVIEW_PERSON = ["?"]
PREVIEW_CONDITION = ["?"]


def capture_strangers(args: argparse.Namespace, camera_config: CameraConfig) -> int:
    """Capture unfiltered photos of one non-enrolled person, paced by ``--interval``.

    Saves every frame the interval allows, whatever the detector says about it. See the
    module docstring: filtering here would delete the evidence the gate measures, and
    would make a high stranger detection rate a statement about the capture tool rather
    than about the detector.
    """
    person = args.person
    if not person or person == "unlabelled":
        LOGGER.error(
            "--person must name this stranger: it becomes the directory they are stored "
            "in and the row they are reported under in the gate"
        )
        return 2
    if args.interval < 0:
        LOGGER.error("--interval cannot be negative")
        return 2
    if args.review:
        LOGGER.warning(
            "--review is ignored for strangers: capture is automatic. A hand-picked "
            "stranger set would no longer measure what the gate is meant to measure."
        )

    root = Path(args.strangers_out)
    images_dir = root / person
    images_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = root / "metadata.jsonl"

    prefix = f"{person}_{args.condition}_"
    highest = highest_frame_index(
        [p.name for p in images_dir.glob(f"{prefix}*")], prefix
    )
    if highest:
        LOGGER.info(
            "%d existing stranger frame(s) for %s/%s; continuing from %04d",
            highest,
            person,
            args.condition,
            highest + 1,
        )

    PREVIEW_PERSON[0] = person
    PREVIEW_CONDITION[0] = args.condition

    detector = open_preview_detector(Path(args.face_model))
    camera = open_camera(camera_config)

    LOGGER.info(
        "strangers: person=%s condition=%s count=%d interval=%gs -> %s",
        person,
        args.condition,
        args.count,
        args.interval,
        images_dir,
    )
    LOGGER.info("nothing is filtered by the detector here; keys: SPACE pause, Q quit")
    if args.camera_note:
        LOGGER.info("camera note: %s", args.camera_note)

    saved = 0
    undetected = 0
    paused = False
    saved_at: float | None = None
    quit_requested = False

    try:
        quit_requested = not run_countdown(camera, args.countdown)
        while not quit_requested and saved < args.count:
            frame = camera.read_or_none()
            if frame is None:
                LOGGER.debug("camera dropped a frame; skipping")
                continue

            frame_h, frame_w = frame.shape[:2]
            box = None
            if detector is not None:
                box = best_face_box(
                    detector(frame, imgsz=args.preview_imgsz, conf=args.preview_conf, verbose=False)[0],
                    args.preview_conf,
                )

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord(" "):
                paused = not paused
                saved_at = None
                LOGGER.info("paused" if paused else "resumed")
            if not window_is_open():
                LOGGER.warning("preview window closed - stopping")
                break

            preview = frame.copy()
            if box is not None:
                draw_box(preview, box, "preview only", (0, 200, 255))
            draw_hud(
                preview,
                stranger_hud_lines(
                    saved, args.count, person, args.condition,
                    args.interval, paused, box is not None, undetected,
                ),
                status_colour=(0, 200, 255) if paused else (0, 255, 0),
            )
            cv2.imshow(PREVIEW_WINDOW, preview)

            if paused:
                continue

            now = time.monotonic()
            if not stranger_due(saved_at, now, args.interval):
                continue

            stem = stranger_stem(person, args.condition, highest + saved + 1)
            path = images_dir / f"{stem}.jpg"
            if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                # A silently skipped write would make the run report fewer frames than
                # it saved, and the gate would then score a smaller set than the one
                # the operator believes they captured.
                LOGGER.error("failed to write %s - stopping rather than under-reporting", path)
                return 1
            info = camera.actual
            append_metadata(
                metadata_path,
                stranger_record(
                    f"{person}/{stem}.jpg",
                    person,
                    args.condition,
                    camera_config.device,
                    (camera_config.width, camera_config.height),
                    (info.width, info.height),
                    info.fourcc,
                    info.fps,
                    camera.matches_request,
                    frame_w,
                    frame_h,
                    args.camera_note,
                    box,
                ),
            )
            saved += 1
            saved_at = now
            if box is None:
                undetected += 1
            LOGGER.info("saved %s (%d/%d)", stem, saved, args.count)
    finally:
        camera.close()
        cv2.destroyAllWindows()

    LOGGER.info("wrote %d frame(s) to %s", saved, images_dir)
    LOGGER.info("metadata: %s", metadata_path)
    if undetected:
        LOGGER.info(
            "%d/%d saved frames had no preview box - these are the ones the gate is "
            "most interested in, so keep them",
            undetected, saved,
        )
    if saved < args.count:
        LOGGER.warning("only %d/%d frames captured - rerun to continue", saved, args.count)
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    root = Path(args.out)
    split = args.split

    # A frame name already used by another split would quietly destroy that
    # split's identity rows (D-039), so the collision is caught here - before the
    # camera opens - rather than discovered when the gate is next read.
    try:
        assert_unique_basenames(read_metadata_rows(root / "metadata.jsonl"))
    except MetadataCollisionError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        camera_config = load_camera_config(args.config)
    except ConfigError as exc:
        LOGGER.error("config error: %s", exc)
        return 1

    # The stranger set is not a YOLO dataset: different root, no labels, no data.yaml,
    # and never near the training tree. Dispatch before anything under --out is created
    # so a stranger run cannot leave an empty images/strangers/ directory behind that
    # later looks like a split somebody trained on.
    if split == STRANGERS:
        return capture_strangers(args, camera_config)

    images_dir = root / "images" / split
    labels_dir = root / "labels" / split
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = root / "metadata.jsonl"

    if args.pad is None:
        args.pad = 0.25 if args.annotator == "dlib" else 0.0
        LOGGER.info(
            "--pad not given; defaulting to %.0f%% for --annotator %s",
            args.pad * 100,
            args.annotator,
        )
    reject_dir = Path(args.reject_dir)
    detector = build_annotator(args.annotator, args)

    camera = open_camera(camera_config)

    if split in GATE_SPLITS:
        # Fires for every held-out split, not just one named "holdout". The
        # warning is the only thing standing between an operator and a frame set
        # that quietly becomes training data, so it must not be reachable by
        # picking a different split name.
        LOGGER.warning(
            "capturing to %s: these frames are NEVER trained on and exist solely "
            "to test the gate. Keep them out of %s/images/train.", split, root
        )

    # Continue numbering from whatever is already on disk. Starting at 0 every run
    # would make a second session with the same --split/--condition overwrite the
    # first one's images, labels and metadata rows in place, so the extra captures
    # would silently replace good data instead of adding to it. Same helper the
    # strangers split uses, so the rule cannot drift between them.
    prefix = f"{split}_{args.condition}_"
    existing = sorted(p.name for p in images_dir.glob(f"{prefix}*.jpg"))
    highest = highest_frame_index(existing, prefix)
    if existing:
        LOGGER.info(
            "%d existing frame(s) for condition %r in %s; continuing from %04d",
            len(existing),
            args.condition,
            split,
            highest + 1,
        )

    saved = 0
    skipped_no_face = 0
    skipped_small = 0
    rejected = 0
    LOGGER.info(
        "split=%s condition=%s person=%s target=%d. keys: s save, n skip, q quit",
        split,
        args.condition,
        args.person,
        args.count,
    )
    LOGGER.info("save-path detector: %s", detector.description)
    LOGGER.info(
        "frame bounds: min-face %dpx, max-face %.0f%%, pad +%.0f%%",
        args.min_face,
        args.max_face * 100,
        args.pad * 100,
    )
    if args.camera_note:
        LOGGER.info("camera note: %s", args.camera_note)

    try:
        while saved < args.count:
            frame = camera.read_or_none()
            if frame is None:
                LOGGER.debug("camera dropped a frame; skipping")
                continue

            # One read, one detection, one decision. The HUD below and the save
            # below both read this object, so "ready - press S" cannot be shown for
            # a frame the save path will then refuse (D-033).
            view = assess_frame(
                frame, detector, args.min_face, args.max_face, args.pad
            )
            frame_h, frame_w = frame.shape[:2]

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

            preview = frame.copy()
            for box in view.usable:
                draw_box(preview, box)

            if not view.accepted:
                if view.boxes:
                    skipped_small += 1
                else:
                    skipped_no_face += 1
                status, colour = view.reason, view.colour
            elif key == ord("n"):
                status, colour = "skipped - press N", (0, 200, 255)
            elif key not in (ord("s"), ord("a")):
                status, colour = "ready - press S to save", (0, 255, 0)
            else:
                status, colour = "saving...", (0, 255, 0)

            lines = [
                f"SAVED {saved}/{args.count}   ({split})",
                status,
                f"condition: {args.condition}   person: {args.person}",
                f"detector: {view.detector}",
                f"pad: +{args.pad:.0%}   faces: {len(view.boxes)}   luma: {view.luma}   "
                f"min-face: {args.min_face}px   max-face: {args.max_face:.0%}",
                f"unusable frames: no face {skipped_no_face}, framing {skipped_small}"
                f"   rejected saves: {rejected}",
                "S = save this frame    N = skip    Q / Esc = quit",
            ]
            draw_hud(preview, lines, status_colour=colour, status_index=1)
            cv2.imshow(PREVIEW_WINDOW, preview)

            if key == ord("n"):
                continue
            if key not in (ord("s"), ord("a")):
                continue
            if not view.accepted:
                # An S press on a frame the HUD already showed as unusable: keep the
                # frame so the refusal can be inspected instead of inferred.
                rejected += 1
                dump = write_rejected_frame(view, reject_dir, rejected)
                LOGGER.warning(
                    "save refused: %s%s",
                    view.reason,
                    f" (frame kept at {dump})" if dump else "",
                )
                show_reason_for(view.reason, REASON_HOLD_MS, preview)
                continue

            # The very object the preview drew, assessed once. No re-read, no
            # re-detection, no second opinion that could disagree with the HUD.
            biggest = view.best
            assert biggest is not None  # guaranteed by view.accepted above

            # Inside the loop, deliberately: when this block sat *outside* it,
            # `saved` never advanced while the loop was running, so
            # `while saved < args.count` could only ever end on Q/Esc - and then
            # exactly one frame was written on the way out. Every S press in
            # between looped and discarded, which reads to the operator as "S does
            # nothing" (D-034).
            stem = f"{split}_{args.condition}_{highest + saved + 1:04d}"
            cv2.imwrite(
                str(images_dir / f"{stem}.jpg"),
                view.frame,
                [cv2.IMWRITE_JPEG_QUALITY, 95],
            )
            write_yolo_label(labels_dir / f"{stem}.txt", biggest, frame_w, frame_h)
            info = camera.actual
            append_metadata(
                metadata_path,
                {
                    "file": f"images/{split}/{stem}.jpg",
                    "split": split,
                    "condition": args.condition,
                    "person": args.person,
                    "device": camera_config.device,
                    "requested": [camera_config.width, camera_config.height],
                    "actual": [info.width, info.height],
                    "actual_fourcc": info.fourcc,
                    "fps": round(info.fps, 1),
                    "geometry_matches_request": camera.matches_request,
                    "camera_note": args.camera_note,
                    "face_box": list(biggest),
                    "frame": [frame_w, frame_h],
                },
            )
            saved += 1
            LOGGER.info("saved %s (%d/%d)", stem, saved, args.count)
    finally:
        camera.close()
        cv2.destroyAllWindows()

    data_yaml = write_data_yaml(root)
    LOGGER.info("wrote %s", data_yaml)

    tally = summarise(root)
    for line in split_summary_lines(tally):
        LOGGER.info("%s", line)
    if "holdout" not in tally:
        LOGGER.warning(
            "no holdout frames exist yet: capture some with --split holdout before trusting the gate"
        )

    if saved < args.count:
        LOGGER.warning("only %d/%d frames captured - rerun to continue", saved, args.count)
    if split == "train":
        LOGGER.warning("review the boxes in %s before training", labels_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())