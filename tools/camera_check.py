"""Enumerate video capture devices and report what each can actually deliver.

Run this once the Zebronics 720p webcam is plugged in, to find out which device
index it is and whether it honours 1280x720 over MJPG.

    python tools/camera_check.py
    python tools/camera_check.py --max-device 8 --target 1280x720

The output is meant to be pasted into config.yaml, so it reports the index and
the best mode per device. Nothing here is hardcoded: ``--target`` sets the
geometry to probe and defaults to the PROTOCOL deployment target.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rpi"))

import cv2  # noqa: E402

# PROTOCOL section 4.1: deployment capture geometry.
#
# Was (1280, 720) and stale from before D-028 moved the burst path to 1080p.
# That default was not harmless: the probe asks each device for this exact
# geometry, the Logitech Brio 100 only grants 1920x1080, so the one working
# camera was dropped from consideration and this tool recommended the laptop's
# covered built-in (luma ~4) instead. config.yaml, PROTOCOL 4.1 and PROTOCOL's
# config table all say 1920x1080; keep this in step with them.
DEPLOYMENT_TARGET = (1920, 1080)

# A device that hands back frames this dark is covered, disabled or facing
# nothing. Mean luma below this is not a usable capture, whatever the geometry.
_MIN_LUMA = 25.0

# Modes worth probing, best-first. MJPG first: at 1280x720 an uncompressed
# stream typically cannot hold a usable frame rate on a USB 2 webcam.
FOURCCS = ("MJPG", "YUYV")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--max-device", type=int, default=6, help="highest index to probe")
    parser.add_argument("--target", nargs=2, type=int, metavar=("W", "H"),
                        default=DEPLOYMENT_TARGET, help="geometry to request")
    parser.add_argument("--warmup", type=int, default=10, help="frames to discard per attempt")
    return parser.parse_args()


def decode_fourcc(value: float) -> str:
    raw = int(value)
    if raw <= 0:
        return "?"
    return "".join(chr((raw >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ") or "?"


def probe(index: int, width: int, height: int, fourcc: str, warmup: int) -> dict[str, object] | None:
    """Try one (index, fourcc) combination. Returns None if it cannot open."""
    capture = cv2.VideoCapture(index)
    if not capture.isOpened():
        capture.release()
        return None

    try:
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        # Read back what was actually granted, then prove frames flow.
        actual_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fourcc = decode_fourcc(capture.get(cv2.CAP_PROP_FOURCC))

        for _ in range(warmup):
            if not capture.read()[0]:
                return {"index": index, "error": "no frames after open"}

        ok, frame = capture.read()
        if not ok or frame is None:
            return {"index": index, "error": "no frame after warm-up"}

        # Normalise to BGR before measuring brightness: packed YUYV arrives as two
        # channels and cvtColor would fail on it.
        from shield_rpi.camera import as_bgr

        bgr = as_bgr(frame)
        luma = float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean())

        return {
            "index": index,
            "requested": (width, height),
            "actual": (actual_w, actual_h),
            "frame_shape": (bgr.shape[1], bgr.shape[0]),
            "fourcc": actual_fourcc,
            "fps": float(capture.get(cv2.CAP_PROP_FPS)),
            "backend": int(capture.get(cv2.CAP_PROP_BACKEND)),
            "luma": luma,
        }
    finally:
        capture.release()


def _configured_device() -> int | None:
    """Device index currently set in config.yaml, or ``None`` if unavailable.

    Prefers staying on the camera already configured: re-running this tool should
    not silently repoint the system at a different sensor.
    """
    try:
        from shield_rpi.config import load_camera_config

        return load_camera_config().device
    except Exception:  # noqa: BLE001 - advisory only, never fatal to the probe
        return None


def main() -> int:
    args = parse_args()
    width, height = args.target
    print(f"probing devices 0..{args.max_device - 1} for {width}x{height}\n")

    found: list[dict[str, object]] = []
    for index in range(args.max_device):
        for fourcc in FOURCCS:
            result = probe(index, width, height, fourcc, args.warmup)
            if result is None:
                break  # device index absent; no point trying other formats
            if "error" in result:
                print(f"[{index}] {fourcc}: FAILED - {result['error']}")
                continue
            found.append(result)
            matched = result["actual"] == (width, height)
            print(
                f"[{index}] {fourcc}: {result['actual'][0]}x{result['actual'][1]} "
                f"{result['fourcc']} @ {result['fps']:.1f}fps"
                f"{'  <- exact match' if matched else ''}"
            )

    if not found:
        print("\nNo working capture devices found.")
        print("Check that the camera is plugged in, not in use by another app, and")
        print("that a camera app (Teams/Zoom/Windows Camera) has released it.")
        return 1

    print("\n--- summary ---")
    # The candidate pool is every device that delivered frames, collapsed to the
    # best-lit fourcc per index. Delivered geometry deliberately does NOT filter
    # the pool: a camera that only grants 1920x1080 is a working camera, and
    # requiring an exact match is what let a covered built-in be recommended over
    # the lit webcam beside it.
    per_device: dict[int, dict[str, object]] = {}
    for result in found:
        index = int(result["index"])
        if index not in per_device or float(result["luma"]) > float(per_device[index]["luma"]):
            per_device[index] = result

    configured = _configured_device()
    lit = [r for r in per_device.values() if float(r["luma"]) >= _MIN_LUMA]

    if len(per_device) > 1:
        print("More than one device answered. Brightness distinguishes a live camera")
        print("from a covered one:")
        for index in sorted(per_device):
            result = per_device[index]
            marker = "  <- lit" if float(result["luma"]) >= _MIN_LUMA else "  <- dark/covered"
            tag = " (currently in config.yaml)" if index == configured else ""
            print(
                f"  camera.device: {index}{marker}"
                f"  luma {float(result['luma']):.0f} @ {float(result['fps']):.1f}fps{tag}"
            )
        print()

    if lit:
        # Lit beats dark outright. Within the lit devices, prefer one that grants
        # the requested geometry, then whatever config.yaml already uses, then the
        # highest frame rate. Preferring the configured index first would let a
        # camera that has since been covered keep winning on inertia alone.
        best = max(
            lit,
            key=lambda r: (
                r["actual"] == (width, height),
                r["index"] == configured,
                float(r["fps"]),
            ),
        )
        print(
            f"Use camera.device: {best['index']}  "
            f"(delivers {best['actual'][0]}x{best['actual'][1]}, fourcc {best['fourcc']}, "
            f"mean luma {float(best['luma']):.0f})"
        )
        if best["actual"] != (width, height):
            # Say it plainly rather than leaving the reader to assume the printed
            # geometry was the one that was asked for.
            print(
                f"  note: no device grants {width}x{height}. The recommendation above "
                f"delivers {best['actual'][0]}x{best['actual'][1]}, and Camera.open() "
                f"logs a size-mismatch warning then continues with the actual size."
            )
            print(
                f"  Set camera.width/height in config.yaml to "
                f"{best['actual'][0]}/{best['actual'][1]} to match what is really delivered."
            )
    else:
        # Every device that answered was dark. Report that as its own finding and
        # name the devices, but do not print a "Use camera.device: N" line: pasting
        # a covered camera into config.yaml yields a black stream and a detector
        # that can never fire, which reads as "the camera is broken".
        print(f"WARNING: every probed device returned a dark scene (mean luma < {_MIN_LUMA:.0f}).")
        print("That means covered, disabled or facing nothing - not necessarily broken.")
        print("Check the lens cap, and whether another app (Teams/Zoom/Windows Camera)")
        print("is holding the device. config.yaml was left alone.")
        for index in sorted(per_device):
            result = per_device[index]
            print(
                f"  device {index}: {result['actual'][0]}x{result['actual'][1]} "
                f"luma {float(result['luma']):.0f}"
                + (" (currently in config.yaml)" if index == configured else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())