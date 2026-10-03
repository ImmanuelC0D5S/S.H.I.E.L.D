"""Face matching against the enrolled store, shared by the Phase 1 harness.

Both ``live_test.py`` (live camera) and ``holdout_test.py`` (offline gate test)
must match identically, otherwise the gate result does not describe the behaviour
seen on the live feed. Keeping one implementation here makes that impossible to
get wrong by accident.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import face_recognition
import numpy as np
from face_store import load_all

LOGGER = logging.getLogger("shield.recogniser")

UNKNOWN = "unknown"


class Recogniser:
    """Face embeddings matched against the enrolled store by cosine distance."""

    def __init__(self, faces_dir: str | Path, threshold: float) -> None:
        self.faces_dir = Path(faces_dir)
        self.threshold = threshold
        self.names: list[str] = []
        self.embeddings: list[np.ndarray] = []
        self.reload()

    def reload(self) -> None:
        self.names, self.embeddings = load_all(self.faces_dir)
        LOGGER.info(
            "recogniser ready: %d enrolled (%s), threshold %.2f",
            len(self.names),
            ", ".join(self.names) or "none",
            self.threshold,
        )

    @property
    def empty(self) -> bool:
        """True when nothing is enrolled, so every face must read :data:`UNKNOWN`."""
        return not self.embeddings

    def match_rgb(self, rgb_crop: cv2.MatLike) -> tuple[str, float]:
        """Return ``(name, distance)`` for one **RGB** crop.

        A distance above :attr:`threshold` yields :data:`UNKNOWN` with the raw
        distance returned, so a near-miss is visible in the logs rather than
        silently collapsed.
        """
        height, width = rgb_crop.shape[:2]
        encodings = face_recognition.face_encodings(
            rgb_crop, [(0, width, height, 0)], num_jitters=1, model="hog"
        )
        if not encodings or not self.embeddings:
            return UNKNOWN, 1.0
        distances = face_recognition.face_distance(self.embeddings, encodings[0])
        index = int(np.argmin(distances))
        best = float(distances[index])
        return (self.names[index], best) if best < self.threshold else (UNKNOWN, best)

    def match_bgr(self, bgr_crop: cv2.MatLike) -> tuple[str, float]:
        """Convenience wrapper for OpenCV-ordered crops."""
        return self.match_rgb(cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2RGB))


# Recognition crops are padded outward from the detected box before the embedding
# is computed. A detector box is a tight fit around skin and contrast features; the
# embedding that decides identity is computed from a crop, and an inner-face crop
# omits forehead, jawline and hairline - the features that distinguish one person
# from another (D-020, D-026 step 5). Padding is applied *at recognition time only*,
# never to detector training labels, so the pretrained detector keeps regressing the
# face convention it was trained on.
RECOGNITION_PAD = 0.25


def pad_box(
    box: tuple[int, int, int, int], frame_w: int, frame_h: int, pad_ratio: float
) -> tuple[int, int, int, int]:
    """Grow ``(x, y, w, h)`` by ``pad_ratio`` of its own size, clipped to the frame.

    Clipping matters because a partially-visible face at the frame edge is still a
    valid detection; an out-of-frame box would produce an empty crop instead.
    """
    x, y, w, h = box
    grow_x = int(round(w * pad_ratio))
    grow_y = int(round(h * pad_ratio))
    x1 = max(x - grow_x, 0)
    y1 = max(y - grow_y, 0)
    x2 = min(x + w + grow_x, frame_w)
    y2 = min(y + h + grow_y, frame_h)
    return x1, y1, x2 - x1, y2 - y1


def crop_rgb(
    frame: cv2.MatLike,
    box: tuple[int, int, int, int],
    pad_ratio: float = RECOGNITION_PAD,
) -> cv2.MatLike | None:
    """Pad ``(x, y, w, h)``, clip to the frame, and return it as RGB, or ``None``.

    YOLO boxes run outside the frame on some inputs; clipping here means a
    partially-visible face at the edge is still matched instead of raising.

    Both the offline gate and the live feed call this one function, which is the
    point: if they cropped differently the gate would not describe live behaviour.
    """
    frame_h, frame_w = frame.shape[:2]
    x, y, w, h = pad_box(box, frame_w, frame_h, pad_ratio)
    x1, y1 = max(x, 0), max(y, 0)
    x2, y2 = min(x + w, frame_w), min(y + h, frame_h)
    if x2 <= x1 or y2 <= y1:
        return None
    return cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)