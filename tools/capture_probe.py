"""Is the problem the camera, or the keyboard?

``capture_dataset.py`` needs a keypress to save, so when nothing saves there are two
very different causes: the frames are not acceptable (a framing or lighting problem the
operator cannot see the cause of), or the keypresses never arrive (a window-focus
problem). Both look identical from outside - "nothing saved" - and they are fixed by
opposite actions.

This probe asks the first question without a preview window and without any keypress:
it reads frames, assesses each one with the same function the capture loop uses, and
prints the verdict. If it reports ``ready`` then the frames are fine and the problem is
the keyboard.

    python tools/capture_probe.py --frames 20
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

from capture_dataset import (  # noqa: E402
    MAX_FACE_FRACTION,
    PREVIEW_DETECT_WIDTH,
    assess_frame,
    build_annotator,
    open_camera,
)
from shield_rpi.config import ConfigError, load_camera_config  # noqa: E402

LOGGER = logging.getLogger("shield.capture_probe")


class Args:
    """Minimal stand-in for capture_dataset's namespace."""

    def __init__(self, parsed: argparse.Namespace) -> None:
        self.annotator = parsed.annotator
        self.detect_width = parsed.detect_width
        self.face_model = parsed.face_model
        self.imgsz = parsed.imgsz


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--frames", type=int, default=20, help="frames to assess")
    parser.add_argument(
        "--config",
        default=str(Path("rpi") / "shield_rpi" / "config.yaml"),
        help="camera config; nothing about the camera is hardcoded",
    )
    parser.add_argument("--annotator", choices=("yolo", "dlib"), default="yolo")
    parser.add_argument("--imgsz", type=int, default=PREVIEW_DETECT_WIDTH)
    parser.add_argument("--detect-width", type=int, default=0)
    parser.add_argument(
        "--face-model", default=str(Path("rpi") / "models" / "face_pretrained.pt")
    )
    parser.add_argument(
        "--min-face", type=int, default=60, help="lower bound, px in the shorter edge"
    )
    parser.add_argument(
        "--max-face",
        type=float,
        default=MAX_FACE_FRACTION,
        help="upper bound, fraction of the frame's shorter edge",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    try:
        camera_config = load_camera_config(args.config)
    except ConfigError as exc:
        LOGGER.error("config error: %s", exc)
        return 1

    LOGGER.info("device %d at %dx%d %s", camera_config.device, camera_config.width,
                camera_config.height, camera_config.fourcc)
    LOGGER.warning("opening the camera: on Windows this can take minutes, it is not a hang")

    annotator = build_annotator(args.annotator, Args(args))
    LOGGER.info("detector: %s", annotator.description)
    LOGGER.info(
        "bounds: min-face %dpx, max-face %.0f%% of the shorter edge",
        args.min_face,
        args.max_face * 100,
    )

    camera = open_camera(camera_config)
    ready = 0
    reasons: dict[str, int] = {}
    try:
        info = camera.actual
        LOGGER.info(
            "actual: %dx%d fourcc=%r fps=%s matches_request=%s",
            info.width, info.height, info.fourcc, round(info.fps, 1),
            camera.matches_request,
        )
        if not camera.matches_request:
            LOGGER.warning(
                "camera did not honour %dx%d - the framing bounds below are "
                "fractions of the frame actually delivered",
                camera_config.width, camera_config.height,
            )

        print()
        print("  #   luma  faces  best box            verdict")
        print("  " + "-" * 68)
        for index in range(1, args.frames + 1):
            frame = camera.read_or_none()
            if frame is None:
                LOGGER.warning("frame %d: camera returned nothing", index)
                continue
            view = assess_frame(
                frame, annotator, args.min_face, args.max_face, 0.0
            )
            reasons[view.reason] = reasons.get(view.reason, 0) + 1
            ready += view.accepted
            box = f"{view.best[0]},{view.best[1]} {view.best[2]}x{view.best[3]}" if view.best else "-"
            print(
                f"  {index:>2}  {view.luma:>4}  {len(view.boxes):>5}  "
                f"{box:<18}  {view.reason}"
            )
    finally:
        camera.close()

    print()
    print(f"  ready to save: {ready}/{args.frames}")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {count:>3}x  {reason}")
    print()

    if ready == 0:
        print("  No frame was acceptable, so nothing could be saved.")
        print("  Fix the framing or the light first - pressing S cannot help.")
        print("  Likely: move back (face too large), move closer (too small),")
        print("          or add light (no face detected).")
        return 1
    if ready < args.frames * 0.8:
        print("  Some frames were acceptable. If S still does not save, the frames")
        print("  are not the problem - the keypress is. Click the preview window")
        print("  once to give it keyboard focus, then press S.")
    else:
        print("  Frames are consistently acceptable. If S does not save, the frames")
        print("  are not the problem - the keypress is. Click the preview window")
        print("  once to give it keyboard focus, then press S.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
