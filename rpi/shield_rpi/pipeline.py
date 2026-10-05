"""Trigger-to-verdict pipeline. ``docs/PROTOCOL.md`` section 4.1.

One burst of frames in, one verdict out, plus the frame worth keeping as the
stored snapshot. The status mapping is the safety-critical part and is stated
here exactly as PROTOCOL.md 2.4.3 and 4.1 specify:

===========================  ======  ========================  ==============
situation                    status  backend status          alarm
===========================  ======  ========================  ==============
no person, no face           0       -- (no alert)            silent
face matched an enrolled id  1       -- (no alert)            silent
face, no match, conf >= thr  2       ``unknown``             alarm
person, no usable face, or   2       ``low_conf``            alarm
  confidence below threshold
===========================  ======  ========================  ==============

The last two share ``status: 2`` and differ only in whether the app should
expect to recognise the person. The ESP32 does not need to tell them apart -
both alarm.

**On "confidence".** There are two different quantities and conflating them
makes the log unreadable. The detector's box confidence says how sure the model
is that it found a face. The match confidence says how sure the identity is.
Reported confidence is the *identity* confidence, ``(1 - distance) * 100``, for
every outcome that involved an embedding - so a reported 87 for an unknown means
"87% sure this is nobody you enrolled", which is what the app's prompt is asking.
For ``low_conf`` there is no usable embedding, so the detector's own confidence
is reported instead, and that mismatch is precisely the condition the user is
being asked about.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2

from shield_rpi.config import Config
from shield_rpi.detector import Detection, Detector
from shield_rpi.recognizer import UNKNOWN, Recogniser, crop_rgb

LOGGER = logging.getLogger(__name__)

STATUS_CLEAR = 0
STATUS_KNOWN = 1
STATUS_ALERT = 2

#: Backend ``status`` strings, per PROTOCOL.md 2.4.3. They distinguish the two
#: cases that share ``status: 2``.
SOURCE_UNKNOWN = "unknown"
SOURCE_LOW_CONF = "low_conf"


@dataclass(frozen=True)
class Verdict:
    """What the pipeline concluded, and why."""

    status: int
    confidence: int
    identity: str | None
    person_detected: bool
    face_detected: bool
    #: Index into the burst of the frame chosen for the stored snapshot.
    snapshot_index: int
    #: ``unknown`` / ``low_conf`` when status is 2, else None.
    reason: str | None
    latency_ms: float
    #: Best match distance, kept for the event log. On a failing gate a run of
    #: near-misses at 0.45-0.49 is the evidence that the threshold is wrong, and
    #: collapsing it to "unknown" throws that evidence away.
    match_distance: float | None

    @property
    def needs_verdict(self) -> bool:
        """Whether the app should ask the user to identify the person.

        Always true for an alert: even a confident "I do not know this face" is
        worth confirming, because the enrolment set may simply be missing the
        owner's colleague.
        """
        return self.status == STATUS_ALERT


def _confidence_from_distance(distance: float) -> int:
    """Cosine distance (0 = identical) to an integer 0-100 identity confidence."""
    return max(0, min(100, int(round((1.0 - distance) * 100))))


class Pipeline:
    """Runs the burst and produces a :class:`Verdict`.

    Holds no I/O of its own: the caller supplies the frames. That is what lets
    the tests drive it with recorded numpy arrays and no camera.
    """

    def __init__(
        self,
        config: Config,
        detector: Detector,
        recogniser: Recogniser,
    ) -> None:
        self.config = config
        self.detector = detector
        self.recogniser = recogniser

    def classify(self, frames: list[cv2.MatLike]) -> Verdict:
        """Classify one burst of frames.

        Every frame is examined rather than stopping at the first face: a person
        mid-turn can be undetectable in frame 1 and perfectly clear in frame 4,
        and the best box across the burst is a better basis for identification
        than any single frame.
        """
        started = time.perf_counter()
        if not frames:
            raise ValueError("classify() needs at least one frame")

        best_face: Detection | None = None
        best_face_index = 0
        person_seen = False

        for index, frame in enumerate(frames):
            face_result = self.detector.detect_faces(frame)
            if (best := face_result.best) is not None and (
                best_face is None or best.confidence > best_face.confidence
            ):
                best_face, best_face_index = best, index

            if not person_seen:
                person_seen = bool(self.detector.detect_person(frame).detections)

        # PROTOCOL.md 4.1 step 4: no face anywhere falls back to the person
        # detector, which is the only way a body with no usable face becomes a
        # low_conf alert rather than silence.
        if best_face is None:
            return self._no_face_verdict(frames, person_seen, started)

        return self._face_verdict(frames, best_face, best_face_index, started)

    def _no_face_verdict(
        self, frames: list[cv2.MatLike], person_seen: bool, started: float
    ) -> Verdict:
        if not person_seen:
            return Verdict(
                status=STATUS_CLEAR,
                confidence=0,
                identity=None,
                person_detected=False,
                face_detected=False,
                snapshot_index=0,
                reason=None,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                match_distance=None,
            )

        # Person, no usable face. Pick the frame where the person is most
        # prominent, which is where the detector confidence reported below comes
        # from.
        #
        # NOTE (D-057): service.py does not publish a snapshot on this path --
        # `face_detected` is False, so there is no photograph of somebody facing
        # away from the camera. That leaves this as a second full inference pass
        # whose frame index is no longer consumed. The confidence half is still
        # used and is already available from the burst loop above, so the pass
        # looks removable. Not done here: it changes a reported number inside the
        # 1 s trigger-to-reply budget that PROTOCOL.md 4.3 calls the figure gating
        # Phase 2. Flagged for the owner rather than changed silently.
        snapshot_index, best_detection = self._best_person_frame(frames)
        confidence = int(round((best_detection.confidence if best_detection else 0.0) * 100))

        LOGGER.info(
            "person present with no usable face -> low_conf (%d%%)", confidence
        )
        return Verdict(
            status=STATUS_ALERT,
            confidence=confidence,
            identity=None,
            person_detected=True,
            face_detected=False,
            snapshot_index=snapshot_index,
            reason=SOURCE_LOW_CONF,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            match_distance=None,
        )

    def _face_verdict(
        self,
        frames: list[cv2.MatLike],
        detection: Detection,
        index: int,
        started: float,
    ) -> Verdict:
        """Identify a detected face.

        ``person_seen`` is intentionally not a parameter: once the face detector
        has fired, a person is present by definition, and threading the person
        model's opinion through would only create a way for the two to disagree.
        """
        detector_conf_pct = int(round(detection.confidence * 100))
        threshold = self.config.recognizer.alert_confidence

        crop = crop_rgb(frames[index], detection.box)
        if crop is None:
            LOGGER.warning("face box collapsed to an empty crop; treating as low_conf")
            return self._low_conf_verdict(index, detector_conf_pct, started)

        name, distance = self.recogniser.match_rgb(crop)
        latency_ms = (time.perf_counter() - started) * 1000.0

        if name != UNKNOWN:
            confidence = _confidence_from_distance(distance)
            LOGGER.info(
                "matched %r at distance %.3f -> status 1 (%.0fms)", name, distance, latency_ms
            )
            return Verdict(
                status=STATUS_KNOWN,
                confidence=confidence,
                identity=name,
                person_detected=True,
                face_detected=True,
                snapshot_index=index,
                reason=None,
                latency_ms=latency_ms,
                match_distance=distance,
            )

        if detector_conf_pct < threshold:
            LOGGER.info(
                "unmatched face at %.3f but detector confidence %d%% < %d%% -> low_conf",
                distance,
                detector_conf_pct,
                threshold,
            )
            return Verdict(
                status=STATUS_ALERT,
                confidence=detector_conf_pct,
                identity=None,
                person_detected=True,
                face_detected=True,
                snapshot_index=index,
                reason=SOURCE_LOW_CONF,
                latency_ms=latency_ms,
                match_distance=distance,
            )

        confidence = _confidence_from_distance(distance)
        LOGGER.info(
            "unmatched face at distance %.3f -> status 2/unknown (confidence %d%%)",
            distance,
            confidence,
        )
        return Verdict(
            status=STATUS_ALERT,
            confidence=confidence,
            identity=None,
            person_detected=True,
            face_detected=True,
            snapshot_index=index,
            reason=SOURCE_UNKNOWN,
            latency_ms=latency_ms,
            match_distance=distance,
        )

    def _low_conf_verdict(self, index: int, confidence: int, started: float) -> Verdict:
        return Verdict(
            status=STATUS_ALERT,
            confidence=confidence,
            identity=None,
            person_detected=True,
            face_detected=True,
            snapshot_index=index,
            reason=SOURCE_LOW_CONF,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            match_distance=None,
        )

    def _best_person_frame(self, frames: list[cv2.MatLike]) -> tuple[int, Detection | None]:
        """Index of the frame with the largest person box, and that box's detection.

        Largest, not merely present: on the no-usable-face path the person fills
        the most of the frame, which is the frame worth looking at.

        NOTE (D-057): the frame index this returns is no longer used to build a
        snapshot, because that path no longer publishes one. The returned
        detection is still used, for the reported `confidence`. See the note in
        `_no_face_verdict`.

        The winning detection is returned as well as the index so the caller can
        report its confidence without a second inference pass. That is not just
        tidiness: PROTOCOL.md 4.3 budgets trigger-to-reply at 1 s and the Pi's
        figure at 1080p has never been measured, so a redundant forward pass is
        a real risk to the one number that gates Phase 2.
        """
        best_index = 0
        best_detection: Detection | None = None
        best_area = -1
        for index, frame in enumerate(frames):
            result = self.detector.detect_person(frame)
            if not result.detections:
                continue
            largest = max(result.detections, key=lambda d: d.box[2] * d.box[3])
            if largest.box[2] * largest.box[3] > best_area:
                best_area, best_index, best_detection = largest.box[2] * largest.box[3], index, largest
        return best_index, best_detection


def annotate_snapshot(frame: cv2.MatLike, verdict: Verdict) -> cv2.MatLike:
    """Draw the verdict onto the stored snapshot.

    The stored image is what a human looks at later to decide whether an alert
    was real, possibly weeks afterwards. A bare frame gives no clue whether it
    was stored as an alert or why, and the confidence that drove the decision is
    not recoverable from the database row alone.
    """
    if verdict.status == STATUS_CLEAR:
        label, colour = "CLEAR", (0, 200, 0)
    elif verdict.status == STATUS_KNOWN:
        label, colour = f"KNOWN: {verdict.identity} ({verdict.confidence}%)", (0, 200, 0)
    else:
        label, colour = f"{str(verdict.reason or 'alert').upper()} ({verdict.confidence}%)", (0, 0, 255)

    out = frame.copy()
    cv2.putText(
        out, label, (16, 44), cv2.FONT_HERSHEY_SIMPLEX, 1.1, colour, 3, cv2.LINE_AA
    )
    cv2.putText(
        out,
        time.strftime("%Y-%m-%d %H:%M:%S"),
        (16, 76),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return out
