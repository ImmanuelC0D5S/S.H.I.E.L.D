"""Live face detect + recognise at 720p on the laptop.

Phase 1 pass criteria: your face is labelled with your name in normal room
light, and a different person is labelled ``unknown``.

Usage:
    python tools/live_test.py
    python tools/live_test.py --no-show --duration 30     # headless accuracy run

Keys: ``q`` or ``Esc`` to quit. ``r`` reloads the face store, ``l`` reloads the
detector model, so enrolment can be iterated without restarting.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from recogniser import UNKNOWN, Recogniser, crop_rgb  # noqa: E402
from shield_rpi.camera import Camera, CameraError  # noqa: E402
from shield_rpi.config import ConfigError, load_camera_config  # noqa: E402
from ultralytics import YOLO  # noqa: E402

LOGGER = logging.getLogger("shield.live_test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--face-model", default="rpi/models/face.pt", help="trained face detector")
    parser.add_argument(
        "--person-model",
        default="yolov8n.pt",
        help="fallback person detector used when no face is found",
    )
    parser.add_argument("--faces-dir", default="rpi/known_faces")
    parser.add_argument("--threshold", type=float, default=0.5, help="max cosine distance for a match")
    parser.add_argument("--conf", type=float, default=0.5, help="detector confidence floor")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument(
        "--config",
        default=str(Path("rpi") / "shield_rpi" / "config.yaml"),
        help="camera device/geometry/fourcc come from here; nothing is hardcoded",
    )
    parser.add_argument("--no-show", action="store_true", help="headless; use --duration")
    parser.add_argument("--duration", type=float, default=0.0, help="seconds to run, 0 = until quit")
    return parser.parse_args()


def load_detector(path: str) -> YOLO:
    try:
        return YOLO(path)
    except Exception as exc:  # noqa: BLE001 - ultralytics raises several types
        LOGGER.error("cannot load face model %s: %s", path, exc)
        raise SystemExit(1) from exc


def draw(frame: cv2.MatLike, label: str, distance: float, box: tuple[int, int, int, int]) -> None:
    x, y, w, h = box
    colour = (0, 255, 0) if label != UNKNOWN else (0, 200, 255)
    cv2.rectangle(frame, (x, y), (x + w, y + h), colour, 2)
    text = label if label == UNKNOWN else f"{label} ({distance:.2f})"
    cv2.putText(frame, text, (x, max(y - 8, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    detector = load_detector(args.face_model)
    recogniser = Recogniser(args.faces_dir, args.threshold)
    if recogniser.empty:
        LOGGER.warning("no enrolled faces - everything will read %s", UNKNOWN)
        LOGGER.warning("enrol first: python tools/enroll_laptop.py --name <your-name>")

    person_detector: YOLO | None = None

    try:
        camera_config = load_camera_config(args.config)
    except ConfigError as exc:
        LOGGER.error("config error: %s", exc)
        return 1

    camera = Camera(camera_config)
    try:
        actual = camera.open()
    except CameraError as exc:
        LOGGER.error("%s; run tools/camera_check.py to list devices", exc)
        return 1
    LOGGER.info("streaming at %s, imgsz=%d, conf=%.2f", actual, args.imgsz, args.conf)

    deadline = time.monotonic() + args.duration if args.duration > 0 else 0.0
    frames = 0
    started = time.monotonic()
    try:
        while True:
            frame = camera.read_or_none()
            if frame is None:
                LOGGER.warning("camera dropped a frame; skipping")
                continue

            results = detector(frame, imgsz=args.imgsz, conf=args.conf, verbose=False)[0]
            faces = results.boxes
            if faces is None or len(faces) == 0:
                if person_detector is None:
                    person_detector = YOLO(args.person_model)
                    LOGGER.info("loaded person fallback %s", args.person_model)
                people = person_detector(
                    frame, imgsz=args.imgsz, conf=args.conf, classes=[0], verbose=False
                )[0]
                for box in people.boxes if people.boxes is not None else []:
                    x, y, w, h = (int(v) for v in box.xyxy[0].tolist())
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (128, 128, 128), 2)
                    cv2.putText(
                        frame, "person (no face)", (x, max(y - 8, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (128, 128, 128), 2,
                    )
            else:
                for box in faces:
                    x, y, w, h = (int(v) for v in box.xyxy[0].tolist())
                    crop = crop_rgb(frame, (x, y, w, h))
                    if crop is None:
                        continue
                    label, distance = recogniser.match_rgb(crop)
                    draw(frame, label, distance, (x, y, w, h))

            frames += 1
            if not args.no_show:
                cv2.putText(
                    frame, f"{frames} frames", (10, frame.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1,
                )
                cv2.imshow("shield", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("r"):
                    recogniser.reload()
                if key == ord("l"):
                    detector = load_detector(args.face_model)

            if deadline and time.monotonic() >= deadline:
                break
    finally:
        camera.close()
        cv2.destroyAllWindows()

    elapsed = time.monotonic() - started
    LOGGER.info(
        "%d frames in %.1fs = %.1f fps (RPi 4B target is under 1s trigger-to-reply)",
        frames,
        elapsed,
        frames / elapsed if elapsed else 0.0,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())