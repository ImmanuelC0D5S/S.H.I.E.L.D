"""Camera geometry negotiation tests.

Host-only, no webcam. A fake capture device stands in for ``cv2.VideoCapture`` so
the *policy* is under test, not the driver: size mismatch must warn and continue,
warm-up frames must be discarded, and I/O failure must never crash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from shield_rpi.camera import Camera, CameraError, FrameInfo, StaleFrameError, encode_jpeg
from shield_rpi.config import CameraConfig

SHIELD_DIR = Path(__file__).resolve().parents[1] / "shield_rpi"

CAP_WIDTH = "CAP_PROP_FRAME_WIDTH"
CAP_HEIGHT = "CAP_PROP_FRAME_HEIGHT"
CAP_FOURCC = "CAP_PROP_FOURCC"
CAP_FPS = "CAP_PROP_FPS"
CAP_BACKEND = "CAP_PROP_BACKEND"


def _decode(raw: int) -> str:
    return "".join(chr((raw >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")


class FakeCapture:
    """Stand-in for ``cv2.VideoCapture``.

    ``granted`` is what the device actually delivers. With ``honour_size=False``
    it ignores the requested geometry, which is how the built-in 640x480 webcam
    behaves when asked for 1280x720.
    """

    def __init__(
        self,
        granted: tuple[int, int],
        *,
        openable: bool = True,
        honour_size: bool = False,
        fail_reads: int = 0,
        frame: np.ndarray | None = None,
    ) -> None:
        self._openable = openable
        self.granted = granted
        self._honour_size = honour_size
        self._fail_reads = fail_reads
        self._frame = (
            frame if frame is not None else np.zeros((granted[1], granted[0], 3), np.uint8)
        )
        self.width = granted[0]
        self.height = granted[1]
        self.released = False
        self.read_count = 0
        self.requested_fourcc: list[str] = []

    # -- cv2.VideoCapture surface (naming mirrors cv2) --

    def isOpened(self) -> bool:  # noqa: N802
        return self._openable

    def set(self, prop: int, value: float) -> bool:  # noqa: A002, N802
        if prop == _prop(CAP_WIDTH):
            self.width = int(value)
        elif prop == _prop(CAP_HEIGHT):
            self.height = int(value)
        elif prop == _prop(CAP_FOURCC):
            self.requested_fourcc.append(_decode(int(value)))
        return True

    def get(self, prop: int) -> float:  # noqa: A002, N802
        if prop == _prop(CAP_WIDTH):
            return float(self.width if self._honour_size else self.granted[0])
        if prop == _prop(CAP_HEIGHT):
            return float(self.height if self._honour_size else self.granted[1])
        if prop == _prop(CAP_FPS):
            return 30.0
        if prop == _prop(CAP_BACKEND):
            return 700.0
        return 0.0

    def read(self) -> tuple[bool, np.ndarray | None]:
        self.read_count += 1
        if self._fail_reads > 0:
            self._fail_reads -= 1
            return False, None
        return True, self._frame.copy()

    def release(self) -> None:
        self.released = True


_CV_PROPS: dict[str, int] = {}


def _prop(name: str) -> int:
    """Resolve and cache a cv2 property constant."""
    if not _CV_PROPS:
        import cv2

        for key in (CAP_WIDTH, CAP_HEIGHT, CAP_FOURCC, CAP_FPS, CAP_BACKEND):
            _CV_PROPS[key] = getattr(cv2, key)
    return _CV_PROPS[name]


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Install a FakeCapture in place of cv2.VideoCapture."""

    def install(fake: FakeCapture) -> FakeCapture:
        import cv2

        monkeypatch.setattr(cv2, "VideoCapture", lambda _index: fake)
        return fake

    return install


def make_config(**overrides: Any) -> CameraConfig:
    base: dict[str, Any] = {"device": 0, "width": 1280, "height": 720, "warmup_frames": 10}
    base.update(overrides)
    return CameraConfig(**base)


# -- geometry negotiation -------------------------------------------------


def test_exact_match_reports_matching(patched):
    patched(FakeCapture((1280, 720), honour_size=True))
    with Camera(make_config()) as camera:
        assert camera.actual.width == 1280
        assert camera.actual.height == 720
        assert camera.matches_request is True


def test_size_mismatch_warns_and_continues(patched, caplog):
    """The built-in webcam at 640x480 when asked for 720p: warn, keep going."""
    fake = patched(FakeCapture((640, 480), honour_size=False))
    with caplog.at_level("WARNING"):
        camera = Camera(make_config())
        info = camera.open()
        camera.close()

    assert (info.width, info.height) == (640, 480), "must report what is delivered"
    assert camera.matches_request is False
    assert any("1280x720" in r.message for r in caplog.records), "mismatch must be logged"
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert fake.released, "camera must be released even on a mismatch"


def test_mjpg_is_requested(patched):
    fake = patched(FakeCapture((1280, 720), honour_size=True))
    with Camera(make_config()):
        pass
    assert "MJPG" in fake.requested_fourcc, "MJPG must be requested before geometry"


def test_warmup_frames_are_discarded(patched):
    fake = patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config(warmup_frames=10))
    camera.open()
    try:
        assert fake.read_count == 10, f"expected 10 warm-up reads, got {fake.read_count}"
        fake.read_count = 0
        assert camera.read() is not None
        assert fake.read_count == 1, "the frame returned to the caller is not warm-up"
    finally:
        camera.close()


def test_zero_warmup_frames_allowed(patched):
    fake = patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config(warmup_frames=0))
    camera.open()
    try:
        assert fake.read_count == 0
    finally:
        camera.close()


def test_warmup_survives_failing_reads(patched, caplog):
    """A dropped warm-up frame must not abort the open."""
    fake = patched(FakeCapture((1280, 720), honour_size=True, fail_reads=3))
    with caplog.at_level("WARNING"):
        camera = Camera(make_config(warmup_frames=10))
        camera.open()
        camera.close()
    assert fake.read_count == 10, "must keep attempting until the budget is spent"


# -- open failures --------------------------------------------------------


def test_unopenable_camera_raises(patched):
    patched(FakeCapture((0, 0), openable=False))
    with pytest.raises(CameraError, match="cannot open camera device 0"):
        Camera(make_config()).open()


def test_reading_before_open_raises():
    with pytest.raises(CameraError, match="not open"):
        Camera(make_config()).read()


def test_actual_before_open_raises():
    with pytest.raises(CameraError, match="not open"):
        _ = Camera(make_config()).actual


# -- reads and bursts -----------------------------------------------------


def test_failed_read_raises_stale(patched):
    patched(FakeCapture((1280, 720), honour_size=True, fail_reads=1))
    camera = Camera(make_config(warmup_frames=0))
    camera.open()
    try:
        with pytest.raises(StaleFrameError):
            camera.read()
    finally:
        camera.close()


def test_read_or_none_swallows_io_errors(patched, caplog):
    """Fail safe at the call site: a dead camera yields None, not an exception."""
    patched(FakeCapture((1280, 720), honour_size=True, fail_reads=5))
    camera = Camera(make_config(warmup_frames=0))
    camera.open()
    try:
        with caplog.at_level("WARNING"):
            assert camera.read_or_none() is None
        assert any("no frame" in r.message for r in caplog.records)
    finally:
        camera.close()


def test_burst_returns_requested_count(patched):
    patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config(warmup_frames=0, burst_frames=5))
    camera.open()
    try:
        assert len(camera.burst()) == 5
    finally:
        camera.close()


def test_burst_tolerates_intermittent_drops(patched):
    """A partial burst still produces a verdict - that is the whole point."""
    patched(FakeCapture((1280, 720), honour_size=True, fail_reads=4))
    camera = Camera(make_config(warmup_frames=0, burst_frames=5))
    camera.open()
    try:
        assert len(camera.burst()) == 5, "burst must retry past dropped frames"
    finally:
        camera.close()


def test_burst_raises_when_camera_is_totally_dead(patched):
    patched(FakeCapture((1280, 720), honour_size=True, fail_reads=10_000))
    camera = Camera(make_config(warmup_frames=0, burst_frames=5))
    camera.open()
    try:
        with pytest.raises(StaleFrameError, match="delivered no frames"):
            camera.burst()
    finally:
        camera.close()


def test_burst_count_must_be_positive(patched):
    patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config(warmup_frames=0))
    camera.open()
    try:
        with pytest.raises(ValueError, match=">= 1"):
            camera.burst(0)
    finally:
        camera.close()


def test_iteration_ends_on_dead_camera(patched):
    patched(FakeCapture((1280, 720), honour_size=True, fail_reads=3))
    camera = Camera(make_config(warmup_frames=0))
    camera.open()
    try:
        assert len(list(camera)) == 0
    finally:
        camera.close()


# -- lifecycle ------------------------------------------------------------


def test_close_is_idempotent(patched):
    fake = patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config())
    camera.open()
    camera.close()
    camera.close()
    assert fake.released


def test_reopen_after_close_works(patched):
    patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config())
    camera.open()
    camera.close()
    assert camera.open().width == 1280
    camera.close()


def test_double_open_does_not_renegotiate(patched):
    fake = patched(FakeCapture((1280, 720), honour_size=True))
    camera = Camera(make_config())
    camera.open()
    reads_after_first = fake.read_count
    camera.open()
    assert fake.read_count == reads_after_first, "second open() must be a no-op"
    camera.close()


# -- config integration ---------------------------------------------------


# -- frame normalisation ---------------------------------------------------


def test_packed_yuyv_is_converted_to_bgr(patched):
    """OpenCV hands packed YUY2 back as 2 channels.

    Every downstream consumer assumes BGR, so the conversion belongs in read()
    rather than in each caller. Without it, a camera that ignores the MJPG request
    crashes imencode/cvtColor with an opaque channels assertion.
    """
    packed = np.zeros((720, 1280, 2), np.uint8)
    patched(FakeCapture((1280, 720), frame=packed))
    camera = Camera(make_config())
    camera.open()
    frame = camera.read()
    assert frame.shape == (720, 1280, 3), f"expected BGR, got {frame.shape}"


def test_grayscale_is_expanded_to_three_channels(patched):
    patched(FakeCapture((1280, 720), frame=np.zeros((720, 1280), np.uint8)))
    camera = Camera(make_config())
    camera.open()
    assert camera.read().shape == (720, 1280, 3)


def test_already_bgr_frames_pass_through_untouched(patched):
    """A 3-channel frame must not be re-converted; that would corrupt colour."""
    frame = np.full((720, 1280, 3), 7, np.uint8)
    patched(FakeCapture((1280, 720), frame=frame))
    camera = Camera(make_config())
    camera.open()
    assert np.array_equal(camera.read(), frame)


def test_yuyv_frames_can_be_encoded(patched):
    """The failure this fixes surfaced as an imencode assertion, not a decode error."""
    packed = np.zeros((720, 1280, 2), np.uint8)
    patched(FakeCapture((1280, 720), frame=packed))
    camera = Camera(make_config())
    camera.open()
    assert encode_jpeg(camera.read(), 80)[:2] == b"\xff\xd8"


def test_burst_frames_are_all_bgr(patched):
    packed = np.zeros((720, 1280, 2), np.uint8)
    patched(FakeCapture((1280, 720), frame=packed))
    camera = Camera(make_config(warmup_frames=0))
    camera.open()
    assert all(f.shape[2] == 3 for f in camera.burst(3))


def test_no_hardcoded_geometry_in_camera_source():
    """Camera geometry belongs in config.yaml, not in Python.

    The test fixtures request 1280x720, but that must live in the test, never in
    the module under test.
    """
    source = (SHIELD_DIR / "camera.py").read_text(encoding="utf-8")
    for literal in ("1280", "720", "640", "480"):
        assert literal not in source, f"{literal} is hardcoded in camera.py; use config.yaml"


def test_frame_info_is_readable():
    assert str(FrameInfo(640, 480, "MJPG", 29.97)) == "640x480 MJPG @ 30.0fps"


def test_encode_jpeg_round_trip():
    data = encode_jpeg(np.zeros((48, 64, 3), np.uint8), quality=70)
    assert data[:2] == b"\xff\xd8", "must be a JPEG"
    assert len(data) > 0