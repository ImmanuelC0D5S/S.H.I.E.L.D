"""Face identification against the enrolled store, for the live compute node.

**Why this file duplicates ``tools/recogniser.py`` instead of importing it.**

``tools/holdout_test.py`` is the Phase 1 gate: the thing that decides whether
the detector and the enrolment are good enough. It is currently *failing* at
55% identification accuracy (DECISIONS.md D-030, and open in HANDOFF.md), and
the cause is a known cross-camera enrolment mismatch. Refactoring the gate's
cropping code while it is being diagnosed would move the number that is being
used to make the decision.

So the live path gets its own copy, and ``rpi/tests/test_recogniser_parity.py``
asserts the two agree box-for-box over a spread of geometries. If they ever
diverge, the test fails loudly instead of the live feed quietly stopping to
describe the gate. D-010's promotion of one shared implementation stays queued
behind the Phase 1 gate passing.

What must stay identical between the two: the recognition padding, and the fact
that padding is applied at recognition time only and never to detector training
labels.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

from shield_rpi.config import RecognizerConfig

LOGGER = logging.getLogger(__name__)

UNKNOWN = "unknown"

#: Crops are padded outward before the embedding is computed. A detector box is a
#: tight fit around contrast features; the embedding that decides identity is
#: computed from a crop, and an inner-face crop omits forehead, jawline and
#: hairline - the features that distinguish one person from another (D-020, D-026
#: step 5).
RECOGNITION_PAD = 0.25

EMBEDDING_DIMS = 128


def pad_box(
    box: tuple[int, int, int, int], frame_w: int, frame_h: int, pad_ratio: float
) -> tuple[int, int, int, int]:
    """Grow ``(x, y, w, h)`` by ``pad_ratio`` of its own size, clipped to the frame.

    Clipping matters because a partially-visible face at the frame edge is still
    a valid detection; an out-of-frame box would produce an empty crop instead.
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

    Returns ``None`` rather than raising when the box collapses: a degenerate
    box is a bad frame, not a reason to abandon the trigger.
    """
    frame_h, frame_w = frame.shape[:2]
    x, y, w, h = pad_box(box, frame_w, frame_h, pad_ratio)
    x1, y1 = max(x, 0), max(y, 0)
    x2, y2 = min(x + w, frame_w), min(y + h, frame_h)
    if x2 <= x1 or y2 <= y1:
        return None
    return cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)


def load_enrolled(faces_dir: str | Path) -> tuple[list[str], list[np.ndarray]]:
    """Load ``<faces_dir>/<name>.npy`` embeddings, skipping unusable files.

    A malformed enrolment file must not stop the node from detecting strangers,
    so bad files are logged and skipped rather than raising.
    """
    directory = Path(faces_dir)
    if not directory.is_dir():
        return [], []

    names: list[str] = []
    vectors: list[np.ndarray] = []
    for path in sorted(directory.glob("*.npy")):
        try:
            vector = np.load(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("skipping unreadable embedding %s: %s", path.name, exc)
            continue
        if vector.shape != (EMBEDDING_DIMS,):
            LOGGER.warning(
                "skipping %s: expected shape (%d,), got %s",
                path.name,
                EMBEDDING_DIMS,
                vector.shape,
            )
            continue
        names.append(path.stem)
        vectors.append(np.asarray(vector, dtype=np.float64))

    LOGGER.info("loaded %d enrolled face(s) from %s", len(names), directory)
    return names, vectors


class Recogniser:
    """Face embeddings matched against the enrolled store by cosine distance."""

    def __init__(self, config: RecognizerConfig) -> None:
        self.config = config
        self.faces_dir = Path(config.faces_dir)
        self.threshold = config.match_distance
        self.names: list[str] = []
        self.embeddings: list[np.ndarray] = []
        self.reload()

    def reload(self) -> None:
        """Re-read the store. Cheap enough to call per trigger, which matters:
        a re-enrolment should take effect without restarting the service."""
        self.names, self.embeddings = load_enrolled(self.faces_dir)
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
        """Return ``(name, cosine_distance)`` for one **RGB** crop.

        A distance above the threshold yields :data:`UNKNOWN` with the raw
        distance, so a near-miss is visible in the log rather than silently
        collapsed. That matters when the gate is failing: a run of near-misses
        at 0.45-0.49 is the evidence that the threshold is the problem, and
        collapsing them all to "unknown" would hide it.
        """
        import face_recognition  # imported here so config-only use needs no dlib

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