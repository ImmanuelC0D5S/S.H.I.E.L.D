"""Enrol a person into the face store from the webcam.

Phase 1 step 4. Captures several photos with a short countdown between them,
averages the embeddings, and writes ``<faces-dir>/<name>.npy``.

Use 5-10 photos per person, varied in angle and light. The raw photos are kept
so an enrolment can be redone without re-shooting.

Usage:
    python tools/enroll_laptop.py --name imman --photos 7
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import face_recognition

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from dataset_meta import HoldoutLeakError, assert_not_gate_split  # noqa: E402
from face_store import FaceStoreError, mean_embedding, save  # noqa: E402
from holdout_test import best_face_box  # noqa: E402
from recogniser import crop_rgb  # noqa: E402
from shield_rpi.camera import Camera, CameraError  # noqa: E402
from shield_rpi.config import ConfigError, load_camera_config  # noqa: E402
from ultralytics import YOLO  # noqa: E402

LOGGER = logging.getLogger("shield.enroll_laptop")

DEFAULT_PHOTO_DIR = Path("rpi/enroll_photos")

# What the operator is told to change before each capture. A single averaged
# embedding is only as good as the spread of conditions behind it: enrol ten
# near-identical frames and the centroid is a narrow spike that misses any real
# frame outside that one spot.
#
# The four pose prompts are not speculative. They are the measured failure axis
# (D-036): every owner frame the gate rejected was a head-turn or a chin-tilt -
# yaw left d=0.589, yaw right d=0.515, chin up d=0.622 - while the far set at a
# completely different distance and brightness passed 4/4 with max 0.486. Pose
# is a known weakness of the dlib 128-d embedding, so it has to be covered by
# the enrolment set rather than wished for.
#
# The lighting and distance prompts were the *previous* hypothesis and are not
# supported by the data, but they are kept: they cost nothing, the operator was
# asked for them, and dropping them on one thin measurement would be its own
# kind of guess. Order matters only in that --photos 8 covers each exactly once.
CONDITION_PROMPTS = (
    "indoor lamp light, face square to the camera",
    "daylight near a window, face square to the camera",
    "about 1 m from the camera (face fills much of the frame)",
    "about 2 m from the camera (head and shoulders visible)",
    "turn your head about 30 degrees to YOUR LEFT, keep eyes forward",
    "turn your head about 30 degrees to YOUR RIGHT, keep eyes forward",
    "chin tilted UP about 20 degrees, as if looking at something above",
    "chin tucked DOWN about 20 degrees, as if looking at something below",
)


def condition_prompt(index: int) -> str:
    """The prompt for capture number ``index`` (0-based), cycling through the set."""
    return CONDITION_PROMPTS[index % len(CONDITION_PROMPTS)]


def draw_condition_prompt(preview, prompt: str) -> None:
    """Write the current condition across the bottom of the preview, wrapped."""
    height, width = preview.shape[:2]
    max_chars = max(20, (width - 20) // 11)
    lines = [prompt[i:i + max_chars] for i in range(0, len(prompt), max_chars)] or [prompt]
    y = height - 20 - 26 * (len(lines) - 1)
    for line in lines:
        cv2.putText(
            preview, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 4
        )
        cv2.putText(
            preview, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 255, 255), 2
        )
        y += 26

# A face cannot be too *large*, and this is not a framing preference - it breaks the
# crop guarantee D-026 standardised on. ``pad_box`` clips at the frame edge, so once
# the face is big enough the 25% margin is clipped away entirely and ``crop_rgb``
# returns (near enough) the whole frame. The enrolled embedding would then come from a
# differently-shaped region than the gate's crop ever will, which is precisely the bug
# D-026 step 5 exists to prevent.
#
# The bound is a fraction of the frame's SHORT edge so it means the same thing at 720p
# and 1080p. Measured on this hardware (D-029): the laptop built-in framed a face at
# 24-36% of the short edge and those photos were sound; the Brio 100 held at arm's
# length reached 61%, which degenerated the crop to the full frame height.
MAX_FACE_FRACTION = 0.45

# Matches the capture_dataset.py --min-face default, so a face is held to the same
# lower bound at enrolment as it is in the dataset.
DEFAULT_MIN_FACE = 60


def framing_problem(
    box: tuple[int, int, int, int],
    frame_w: int,
    frame_h: int,
    min_face: int,
    max_fraction: float = MAX_FACE_FRACTION,
) -> str | None:
    """Explain why a detected face cannot be enrolled, or ``None`` if it can.

    Mirrors ``capture_dataset.reason_no_face`` so the two capture tools teach the same
    lesson: a refused frame states the number and the movement that fixes it, rather
    than leaving a frozen counter to be interpreted.
    """
    short_edge = int(min(box[2], box[3]))
    if short_edge < min_face:
        return f"face too small ({short_edge}px < {min_face}px) - move closer"
    limit = max_fraction * min(frame_w, frame_h)
    if short_edge > limit:
        return (
            f"face too large ({short_edge}px > {limit:.0f}px = "
            f"{max_fraction:.0%} of frame) - move back"
        )
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="person's name, e.g. imman")
    parser.add_argument("--photos", type=int, default=7, help="photos to average (5-10 recommended)")
    parser.add_argument("--faces-dir", default="rpi/known_faces")
    parser.add_argument("--photo-dir", default=str(DEFAULT_PHOTO_DIR))
    parser.add_argument(
        "--config",
        default=str(Path("rpi") / "shield_rpi" / "config.yaml"),
        help="camera device/geometry/fourcc come from here; nothing is hardcoded",
    )
    parser.add_argument("--jitters", type=int, default=1, help="dlib jitters per photo (slower, better)")
    parser.add_argument(
        "--face-model",
        default=str(Path("rpi") / "models" / "face_pretrained.pt"),
        help="detector used to locate the face; must match the inference detector",
    )
    parser.add_argument("--conf", type=float, default=0.5, help="detector confidence floor")
    parser.add_argument("--imgsz", type=int, default=640, help="detector input size")
    parser.add_argument(
        "--min-face",
        type=int,
        default=DEFAULT_MIN_FACE,
        help="reject a face whose short edge is under this many px (move closer)",
    )
    parser.add_argument(
        "--max-face",
        type=float,
        default=MAX_FACE_FRACTION,
        help=(
            "reject a face whose short edge exceeds this fraction of the frame's short "
            "edge (move back); a larger face degenerates the recognition crop"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if args.photos < 1:
        LOGGER.error("--photos must be at least 1")
        return 2

    try:
        camera_config = load_camera_config(args.config)
    except ConfigError as exc:
        LOGGER.error("config error: %s", exc)
        return 1

    detector = YOLO(args.face_model)
    LOGGER.info("detector %s loaded for enrolment cropping", args.face_model)

    camera = Camera(camera_config)
    try:
        info = camera.open()
    except CameraError:
        LOGGER.error(
            "cannot open camera device %d; run tools/camera_check.py to list devices",
            camera_config.device,
        )
        return 1
    LOGGER.info("camera %d open at %s", camera_config.device, info)

    photo_dir = Path(args.photo_dir) / args.name
    try:
        assert_not_gate_split(photo_dir)
        assert_not_gate_split(Path(args.faces_dir))
    except HoldoutLeakError as exc:
        LOGGER.error("%s", exc)
        return 1
    photo_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    embeddings: list = []
    shots: list[tuple[str, object]] = []
    captured = 0
    recorded: dict[str, int] = {}
    LOGGER.info("capturing %d photos of %r. press 'q' to abort, 's' to capture", args.photos, args.name)
    LOGGER.info(
        "conditions cycled per photo - change these between shots:\n  %s",
        "\n  ".join(f"{i + 1}. {p}" for i, p in enumerate(CONDITION_PROMPTS)),
    )

    try:
        while captured < args.photos:
            frame = camera.read_or_none()
            if frame is None:
                continue

            preview = frame.copy()
            prompt = condition_prompt(captured)
            draw_condition_prompt(preview, prompt)
            cv2.putText(
                preview,
                f"{args.name}  {captured}/{args.photos}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )
            cv2.imshow("enrol", preview)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                LOGGER.warning("aborted after %d photo(s)", captured)
                return 1

            if key == ord("s") or key == ord(" ") or captured == 0:
                # Detect with the same detector and crop with the same padding as
                # inference. face_encodings() on a whole frame would otherwise run
                # its own internal HOG pass on the untightened image, so the enrolled
                # embedding and the live crops would come from different regions and
                # the gate would not describe live behaviour (D-026 step 5).
                box = best_face_box(
                    detector(frame, imgsz=args.imgsz, conf=args.conf, verbose=False)[0],
                    args.conf,
                )
                if box is not None:
                    # Reject an unusably *sized* face before cropping, and say which way
                    # to move. Without this, a face filling the frame is embedded happily
                    # from a whole-frame "crop" and enrolment silently stops matching.
                    problem = framing_problem(
                        box,
                        frame.shape[1],
                        frame.shape[0],
                        args.min_face,
                        args.max_face,
                    )
                    if problem is not None:
                        LOGGER.warning("%s", problem)
                        cv2.putText(
                            preview,
                            problem,
                            (10, preview.shape[0] - 24),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (0, 140, 255),
                            2,
                        )
                        cv2.imshow("enrol", preview)
                        cv2.waitKey(600)
                        continue

                crop = crop_rgb(frame, box) if box is not None else None
                if crop is None:
                    LOGGER.warning("no face found in this frame, try again")
                    continue

                encodings = face_recognition.face_encodings(
                    crop, [(0, crop.shape[1], crop.shape[0], 0)],
                    num_jitters=args.jitters,
                    model="hog",
                )
                if not encodings:
                    LOGGER.warning("could not embed the face in this frame, try again")
                    continue

                captured += 1
                embeddings.append(encodings[0])
                shots.append((prompt, encodings[0]))
                path = photo_dir / f"{stamp}-{captured:02d}.jpg"
                cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                LOGGER.info(
                    "photo %d/%d -> %s  [condition: %s]", captured, args.photos, path.name, prompt
                )
                recorded[prompt] = recorded.get(prompt, 0) + 1

                if captured < args.photos:
                    LOGGER.info("next up - %s", condition_prompt(captured))
                    for remaining in range(3, 0, -1):
                        cv2.putText(
                            preview,
                            str(remaining),
                            (preview.shape[1] - 60, 60),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            1.5,
                            (0, 200, 255),
                            3,
                        )
                        cv2.imshow("enrol", preview)
                        cv2.waitKey(700)
                time.sleep(0.1)
    finally:
        camera.close()
        cv2.destroyAllWindows()

    if not embeddings:
        LOGGER.error("no embeddings captured, nothing written")
        return 1

    try:
        averaged = mean_embedding(embeddings)
        path = save(args.faces_dir, args.name, averaged)
    except FaceStoreError as exc:
        LOGGER.error("enrolment failed: %s", exc)
        return 1

    spread = [float(face_recognition.face_distance([averaged], e)[0]) for e in embeddings]
    LOGGER.info("wrote %s", path)
    LOGGER.info(
        "intra-person cosine distance: min %.3f max %.3f (lower is more consistent)",
        min(spread),
        max(spread),
    )
    if max(spread) > 0.4:
        LOGGER.warning("photos look inconsistent - re-shoot with steadier framing and lighting")

    missing = [p for p in CONDITION_PROMPTS if recorded.get(p, 0) == 0]
    if missing:
        LOGGER.warning(
            "%d of %d conditions were never captured: %s",
            len(missing),
            len(CONDITION_PROMPTS),
            "; ".join(missing),
        )
        LOGGER.warning(
            "an enrolment that misses a condition tends to miss it at the gate too - "
            "re-run with at least --photos %d to cover all of them",
            len(CONDITION_PROMPTS),
        )
    else:
        LOGGER.info("condition coverage: all %d prompts captured", len(CONDITION_PROMPTS))
    for prompt_text, count in recorded.items():
        LOGGER.info("  %dx  %s", count, prompt_text)

    # Distance from each photo to the centroid that was just written, grouped by
    # the condition it was shot under. A pose photo sitting far from the centroid
    # means that pose is not represented, which is the whole failure mode D-036
    # measured - and it is visible here rather than three gate runs later.
    if shots:
        print("\nper-condition distance to the centroid that was written:")
        by_prompt: dict[str, list[float]] = {}
        for prompt_text, embedding in shots:
            d = float(face_recognition.face_distance([averaged], embedding)[0])
            by_prompt.setdefault(prompt_text, []).append(d)
        for prompt_text in CONDITION_PROMPTS:
            values = by_prompt.get(prompt_text)
            if not values:
                continue
            worst = max(values)
            flag = "   <-- outlier" if worst > 0.4 else ""
            LOGGER.info(
                "  %-58s n=%d  mean %.3f  worst %.3f%s",
                prompt_text[:58], len(values), sum(values) / len(values), worst, flag,
            )
        pose_worst = max(
            (max(v) for k, v in by_prompt.items() if "head" in k.lower() or "chin" in k.lower()),
            default=None,
        )
        if pose_worst is not None:
            LOGGER.info("  worst pose-photo distance: %.3f", pose_worst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())