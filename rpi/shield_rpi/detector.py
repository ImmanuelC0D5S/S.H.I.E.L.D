"""Face and person detection on the compute node.

Wraps ultralytics YOLO with the geometry the pipeline actually uses, and hides
the two API mistakes that return plausible-looking nonsense instead of raising
(both recorded in HANDOFF.md):

1. ultralytics wants **BGR**, not RGB.
2. ultralytics reports ``xyxy`` as ``(x1, y1, x2, y2)``, not
   ``(top, left, bottom, right)``.

Both feed the wrong crop and produce an embedding from a differently-shaped
region than the gate scored, which shows up as "the live feed disagrees with the
gate" and costs hours. They are converted once, here, at the boundary.

Models are loaded lazily. Constructing a :class:`Detector` costs nothing, so
config validation and unit tests do not need the weights on disk.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from shield_rpi.config import DetectorConfig

LOGGER = logging.getLogger(__name__)

#: ``(x, y, w, h)`` in pixels, clipped to the frame.
Box = tuple[int, int, int, int]


class DetectorError(RuntimeError):
    """Raised when a model is missing or inference fails unrecoverably."""


def xyxy_to_xywh(box: Sequence[float], frame_w: int, frame_h: int) -> Box:
    """Convert one ultralytics ``xyxy`` box to a clipped ``(x, y, w, h)``.

    Boxes routinely run outside the frame when a face is clipped by the edge;
    they are clamped rather than dropped, because a partially-visible face is
    still a person worth recognising.
    """
    x1, y1, x2, y2 = (float(value) for value in box[:4])
    x1 = max(0, min(int(round(x1)), frame_w))
    y1 = max(0, min(int(round(y1)), frame_h))
    x2 = max(0, min(int(round(x2)), frame_w))
    y2 = max(0, min(int(round(y2)), frame_h))
    if x2 <= x1 or y2 <= y1:
        return (0, 0, 0, 0)
    return (x1, y1, x2 - x1, y2 - y1)


@dataclass(frozen=True)
class Detection:
    """One box with its score."""

    box: Box
    confidence: float


@dataclass(frozen=True)
class DetectionResult:
    """Everything one model saw in one frame."""

    detections: list[Detection]
    inference_ms: float

    @property
    def best(self) -> Detection | None:
        return max(self.detections, key=lambda d: d.confidence, default=None)


class Detector:
    """Lazily-loaded face and person detectors."""

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self._face = None
        self._person = None

    def _load(self, which: str, path: str):
        # `from ultralytics import YOLO` inside the method: importing the package
        # costs a second and pulls torch, which the config tests must not pay.
        from ultralytics import YOLO

        resolved = Path(path)
        if not resolved.is_file():
            raise DetectorError(
                f"{which} model not found at {resolved}. "
                f"Trained weights are gitignored; see HANDOFF.md 'Next'."
            )
        LOGGER.info("loading %s model from %s", which, resolved)
        return YOLO(str(resolved))

    @property
    def face(self):
        if self._face is None:
            self._face = self._load("face", self.config.face_model)
        return self._face

    @property
    def person(self):
        if self._person is None:
            self._person = self._load("person", self.config.person_model)
        return self._person

    def warm(self) -> None:
        """Load both models now, before anything can ask for a verdict.

        Lazy loading is right for the models in isolation and wrong for the
        service: the first pipeline run paid ~4.7 s of ``ultralytics`` import
        and weights loading on the laptop, which lands inside PROTOCOL.md 4.3's
        1 s reply budget *and* past the node's 5 s reply timeout (2.9). A
        service that loads on first use therefore answers its very first
        trigger late, and the sensor node treats an unanswered trigger as a
        dead link and goes to its local fail-safe. Loading here makes start-up
        fail loudly when a weight file is missing, which is the only moment the
        operator is watching.
        """
        self._face = self._load("face", self.config.face_model)
        self._person = self._load("person", self.config.person_model)
        LOGGER.info("detector warm: face and person models loaded")

    # -- inference ---------------------------------------------------------

    def _run(self, model, frame: cv2.MatLike, wanted_class: int | None) -> DetectionResult:
        started = time.perf_counter()
        results = model.predict(
            frame,  # BGR, see module docstring
            imgsz=self.config.imgsz,
            conf=self.config.conf,
            verbose=False,
        )
        inference_ms = (time.perf_counter() - started) * 1000.0

        if not results:
            return DetectionResult([], inference_ms)

        result = results[0]
        frame_h, frame_w = frame.shape[:2]
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return DetectionResult([], inference_ms)

        xyxy = np.asarray(boxes.xyxy)
        confidences = np.asarray(boxes.conf).reshape(-1)
        classes = (
            np.asarray(boxes.cls).reshape(-1).astype(int)
            if getattr(boxes, "cls", None) is not None
            else np.zeros(len(confidences), dtype=int)
        )

        detections: list[Detection] = []
        for coords, score, cls in zip(xyxy, confidences, classes, strict=False):
            if wanted_class is not None and int(cls) != wanted_class:
                continue
            box = xyxy_to_xywh(coords, frame_w, frame_h)
            if box == (0, 0, 0, 0):
                continue
            detections.append(Detection(box=box, confidence=float(score)))

        detections.sort(key=lambda d: d.confidence, reverse=True)
        return DetectionResult(detections=detections, inference_ms=inference_ms)

    def detect_faces(self, frame: cv2.MatLike) -> DetectionResult:
        """Face detections, highest confidence first.

        The face model is single-class (``nc=1``, ``names={0:'FACE'}``), so no
        class filter is applied.
        """
        return self._run(self.face, frame, wanted_class=None)

    def detect_person(self, frame: cv2.MatLike) -> DetectionResult:
        """Person detections. Class 0 of the person model."""
        return self._run(self.person, frame, wanted_class=0)

    def reload(self) -> None:
        """Drop cached models. For a weights swap without a restart."""
        self._face = None
        self._person = None