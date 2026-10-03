"""V4L2/USB camera capture for the compute node.

Geometry is never hardcoded here. ``Camera`` is configured from
:class:`shield_rpi.config.CameraConfig`, which reads ``config.yaml``.

Two behaviours matter for correctness and are both deliberate:

**Size mismatch is a warning, not an error.** A camera may not honour the
requested geometry. We log what we actually got and continue, because a detector
trained on one resolution still runs on another and refusing to open would mean
no intruder detection at all. The actual size is what gets reported to the
backend, so the record never lies about what was analysed.

**Frames are discarded after opening.** A freshly opened camera has not yet
locked auto-exposure and auto-white-balance; the first frames are typically
blown out, colour-shifted, or black. Training or recognising on them produces
worse embeddings than the steady state, so we throw them away.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import cv2

from shield_rpi.config import CameraConfig

LOGGER = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Raised when the camera cannot be opened at all."""


class StaleFrameError(CameraError):
    """Raised when a read does not complete within the staleness budget."""


@dataclass(frozen=True)
class FrameInfo:
    """What the camera is really delivering, as opposed to what was requested."""

    width: int
    height: int
    fourcc: str
    fps: float

    def __str__(self) -> str:
        return f"{self.width}x{self.height} {self.fourcc} @ {self.fps:.1f}fps"


def _decode_fourcc(value: float) -> str:
    raw = int(value)
    if raw <= 0:
        return "?"
    return "".join(chr((raw >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ") or "?"


class Camera:
    """A configured, warmed-up camera.

    Use as a context manager; :meth:`close` is also safe to call directly and is
    idempotent.
    """

    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self._capture: cv2.VideoCapture | None = None
        self._actual: FrameInfo | None = None
        self._requested_match = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> FrameInfo:
        """Open the device, negotiate format, and discard warm-up frames."""
        if self._capture is not None:
            return self._actual  # type: ignore[return-value]

        capture = cv2.VideoCapture(self.config.device)
        if not capture.isOpened():
            capture.release()
            raise CameraError(f"cannot open camera device {self.config.device}")

        fourcc = cv2.VideoWriter_fourcc(*self.config.fourcc)
        capture.set(cv2.CAP_PROP_FOURCC, fourcc)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)

        actual = self._read_info(capture)
        self._capture = capture
        self._actual = actual
        self._requested_match = (
            actual.width == self.config.width and actual.height == self.config.height
        )

        if not self._requested_match:
            LOGGER.warning(
                "camera %d: requested %dx%d but delivering %s - continuing with the "
                "actual size. Detection accuracy may differ from training.",
                self.config.device,
                self.config.width,
                self.config.height,
                actual,
            )

        self._discard_warmup()
        LOGGER.info("camera %d ready at %s", self.config.device, actual)
        return actual

    def _read_info(self, capture: cv2.VideoCapture) -> FrameInfo:
        return FrameInfo(
            width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            fourcc=_decode_fourcc(capture.get(cv2.CAP_PROP_FOURCC)),
            fps=float(capture.get(cv2.CAP_PROP_FPS)),
        )

    def _discard_warmup(self) -> None:
        """Drop frames until auto-exposure settles."""
        discarded = 0
        for _ in range(self.config.warmup_frames):
            ok, _ = self._require_capture().read()
            if not ok:
                LOGGER.warning(
                    "warm-up frame %d/%d failed to read; continuing",
                    discarded + 1,
                    self.config.warmup_frames,
                )
                continue
            discarded += 1
        if discarded:
            LOGGER.debug("discarded %d warm-up frame(s) for auto-exposure", discarded)

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()
            LOGGER.debug("camera %d released", self.config.device)

    def __enter__(self) -> Camera:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- properties --------------------------------------------------------

    def _require_capture(self) -> cv2.VideoCapture:
        if self._capture is None:
            raise CameraError("camera is not open; call open() first")
        return self._capture

    @property
    def actual(self) -> FrameInfo:
        """Real delivered geometry. Raises if the camera is closed."""
        if self._actual is None:
            raise CameraError("camera is not open; call open() first")
        return self._actual

    @property
    def matches_request(self) -> bool:
        """True when the device honoured the requested geometry."""
        return self._requested_match

    # -- reading -----------------------------------------------------------

    def read(self) -> cv2.MatLike:
        """Read one BGR frame, raising :class:`StaleFrameError` if it does not arrive."""
        capture = self._require_capture()
        started = time.monotonic()
        ok, frame = capture.read()
        elapsed = time.monotonic() - started
        if not ok or frame is None:
            raise StaleFrameError(f"camera {self.config.device} returned no frame")
        if elapsed > self.config.stale_after_s:
            LOGGER.warning(
                "camera %d frame took %.2fs (budget %.2fs)",
                self.config.device,
                elapsed,
                self.config.stale_after_s,
            )
        return as_bgr(frame)

    def read_or_none(self) -> cv2.MatLike | None:
        """Read one frame, or ``None`` if it failed. Never raises on I/O error."""
        try:
            return self.read()
        except StaleFrameError as exc:
            LOGGER.warning("%s", exc)
            return None

    def burst(self, count: int | None = None) -> list[cv2.MatLike]:
        """Grab a burst of frames for one inference pass.

        The detector gets several chances at the intruder: someone mid-turn may
        be undetectable in frame 1 and perfectly clear in frame 4. Frames that
        fail to read are skipped rather than raising, so a partial burst still
        produces a verdict.
        """
        wanted = count if count is not None else self.config.burst_frames
        if wanted < 1:
            raise ValueError(f"burst count must be >= 1, got {wanted}")

        frames: list[cv2.MatLike] = []
        attempts = 0
        max_attempts = wanted * 3
        while len(frames) < wanted and attempts < max_attempts:
            attempts += 1
            frame = self.read_or_none()
            if frame is not None:
                frames.append(frame)

        if not frames:
            raise StaleFrameError(
                f"camera {self.config.device} delivered no frames in {attempts} attempts"
            )
        if len(frames) < wanted:
            LOGGER.warning(
                "burst short: wanted %d, got %d after %d attempts",
                wanted,
                len(frames),
                attempts,
            )
        return frames

    def __iter__(self) -> Iterator[cv2.MatLike]:
        """Yield frames until the camera stops delivering or fails."""
        while True:
            frame = self.read_or_none()
            if frame is None:
                return
            yield frame


def annotate(frame: cv2.MatLike, boxes: Sequence[tuple[int, int, int, int]], labels: Sequence[str]) -> cv2.MatLike:
    """Draw boxes and labels onto a copy of ``frame``. Used for snapshots."""
    out = frame.copy()
    for (x, y, w, h), text in zip(boxes, labels, strict=False):
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(
            out, text, (x, max(y - 8, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
        )
    return out


def as_bgr(frame: cv2.MatLike) -> cv2.MatLike:
    """Normalise a decoded frame to 3-channel BGR.

    OpenCV hands back packed YUYV/YUY2 as a **2-channel** array: Y, U, Y, V per
    two pixels, with no colour conversion applied. Passing that straight to the
    detector, to ``cv2.cvtColor(..., BGR2GRAY)`` or to ``imencode`` fails with an
    opaque assertion (``channels == 1 || channels == 3 || channels == 4``). Since
    the fourcc is a config value and a camera may ignore the MJPG request and grant
    YUY2 anyway, the conversion belongs here rather than in every caller - one
    place, and every consumer can assume BGR.
    """
    channels = 1 if frame.ndim == 2 else frame.shape[2]
    if channels == 3:
        return frame
    if channels == 1:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if channels == 2:
        return cv2.cvtColor(frame, cv2.COLOR_YUV2BGR_YUY2)
    LOGGER.warning("unexpected frame shape %s; passing through", frame.shape)
    return frame


def encode_jpeg(frame: cv2.MatLike, quality: int = 85) -> bytes:
    """Encode a frame as JPEG bytes for upload."""
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise CameraError("JPEG encoding failed")
    return buffer.tobytes()