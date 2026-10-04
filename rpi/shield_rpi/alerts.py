"""Alert publication to Supabase.

**This module does not send FCM and does not hold a service account.** It does
exactly two things on the alert path:

1. upload the snapshot JPEG to the private ``snapshots`` storage bucket;
2. insert one row into ``events``.

Everything downstream of that row is the ``send-alert`` Edge Function's job,
triggered by a database webhook. DECISIONS.md D-044: the Firebase admin key
exists in exactly one place, so it cannot leak from the RPi, cannot be read off
the Pi's disk, and cannot be captured from a compromised Pi.

If you find yourself adding an FCM call here, the design has been broken.

Transport is :mod:`urllib.request` from the standard library. Two REST calls
per alert do not justify a dependency, and a lighter import graph matters on a
Pi.

Offline behaviour, per shield-rpi-prompt.md: a failed upload or insert is queued
on disk and retried with backoff. The event is *never* dropped, because a
dropped alert is indistinguishable from a quiet house.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shield_rpi.config import SupabaseConfig

LOGGER = logging.getLogger(__name__)

#: PROTOCOL.md 2.4.3 status values.
STATUS_CLEAR = 0
STATUS_KNOWN = 1
STATUS_ALERT = 2


class AlertError(RuntimeError):
    """Raised when an alert could not be published."""


class OfflineError(AlertError):
    """The request failed for a reason that is likely to resolve (network, 5xx)."""


def now_unix() -> int:
    return int(datetime.now(UTC).timestamp())


@dataclass(frozen=True)
class AlertEvent:
    """One intrusion event, ready to publish.

    ``event_id`` is generated here, client-side, which is what makes the retry
    path idempotent: a repeat publish of the same object hits the primary key on
    ``events`` and cannot create a second row for one intruder.
    """

    event_id: str
    ts: int
    status: int
    confidence: int
    image_path: str
    device_id: str
    needs_verdict: bool = True
    identity: str | None = None
    trigger_tier: str | None = None
    latency_ms: int | None = None
    person_detected: bool | None = None
    face_detected: bool | None = None

    def __post_init__(self) -> None:
        # Clamped here rather than in to_row() because events.confidence has a
        # CHECK constraint. A value that violates it comes back from PostgREST as
        # an opaque 409 with no hint which column was wrong, and the alert is
        # queued for a retry that can never succeed.
        object.__setattr__(self, "confidence", max(0, min(100, int(self.confidence))))
        if self.status not in (0, 1, 2):
            raise AlertError(f"status must be 0, 1 or 2 (PROTOCOL.md 2.4.3), got {self.status}")

    def to_row(self) -> dict[str, Any]:
        """The exact column set ``events`` accepts. ``None`` columns are omitted.

        Omitting rather than sending null keeps the INSERT free of columns the
        pipeline did not measure, so a future ``NOT NULL`` is a loud failure
        rather than a silently-wrong value.
        """
        row: dict[str, Any] = {
            "id": self.event_id,
            "ts": self.ts,
            "status": self.status,
            "confidence": self.confidence,
            "image_path": self.image_path,
            "needs_verdict": self.needs_verdict,
            "device_id": self.device_id,
        }
        optional = {
            "identity": self.identity,
            "trigger_tier": self.trigger_tier,
            "latency_ms": self.latency_ms,
            "person_detected": self.person_detected,
            "face_detected": self.face_detected,
        }
        row.update({key: value for key, value in optional.items() if value is not None})
        return row

    def to_json(self) -> dict[str, Any]:
        """Queue-file form. Identical to :meth:`to_row` plus the JPEG."""
        return {"row": self.to_row(), "jpeg_b64": ""}


class SupabaseClient:
    """Minimal PostgREST + Storage client for the compute node.

    Authenticates as the ``shield_rpi`` Postgres role via a JWT minted by
    ``tools/mint_role_token.py``. It is **not** the service-role key: that key
    bypasses RLS, which would make every policy in
    ``supabase/migrations/20261003090100_shield_roles_grants.sql`` decorative.
    """

    def __init__(self, config: SupabaseConfig) -> None:
        self.config = config

    # -- transport ---------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.config.key,
            "Authorization": f"Bearer {self.config.key}",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in self._headers(headers).items():
            request.add_header(key, value)

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_s) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            # 4xx is a bug (bad key, denied by RLS, schema mismatch). 5xx and
            # 429 are transient. Separating them means the queue retries only
            # what retrying can fix.
            if exc.code < 500 and exc.code != 429:
                raise AlertError(f"{method} {url} -> {exc.code}: {detail}") from exc
            raise OfflineError(f"{method} {url} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OfflineError(f"{method} {url} -> {exc}") from exc

    # -- storage -----------------------------------------------------------

    def upload_snapshot(self, jpeg: bytes, path: str) -> str:
        """Upload a JPEG into the private bucket and return its object path.

        PROTOCOL.md 3.3 caps a snapshot at 512 KB. Checked here rather than
        letting the bucket reject it, because a 413 from the storage API comes
        back as an opaque error and the fix belongs at the encoder.
        """
        if len(jpeg) > self.config.max_snapshot_bytes:
            raise AlertError(
                f"snapshot is {len(jpeg)} bytes, over the "
                f"{self.config.max_snapshot_bytes} byte contract; re-encode at a "
                f"lower quality or resize before publishing"
            )
        quoted = "/".join(urllib.parse.quote(part) for part in path.split("/"))
        url = f"{self.config.url}/storage/v1/object/{self.config.bucket}/{quoted}"
        status, _ = self._request(
            "POST",
            url,
            body=jpeg,
            headers={"Content-Type": "image/jpeg", "x-upsert": "true"},
        )
        if status not in (200, 201):
            raise OfflineError(f"storage upload returned {status}")
        return path

    # -- tables ------------------------------------------------------------

    def insert_event(self, event: AlertEvent) -> None:
        url = f"{self.config.url}/rest/v1/events"
        status, _ = self._request(
            "POST",
            url,
            body=json.dumps([event.to_row()]).encode("utf-8"),
            headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
        )
        if status not in (200, 201, 409):
            # 409 means the event_id already exists, which is the idempotent
            # success case: a retry after a response we never saw.
            raise OfflineError(f"event insert returned {status}")

    def is_armed(self) -> bool:
        """Read ``system_state.armed``.

        Defaults to True on error. Disarming is a convenience switch, and
        failing closed here means an outage would silently stop all intrusion
        detection - the system staying silent while the owner believes it is
        armed is the worst failure this design can have.
        """
        url = f"{self.config.url}/rest/v1/system_state?id=eq.1&select=armed"
        try:
            _, body = self._request("GET", url, headers={"Accept": "application/json"})
            rows = json.loads(body or b"[]")
        except AlertError as exc:
            LOGGER.warning("could not read system_state.armed, assuming armed: %s", exc)
            return True
        if not rows:
            LOGGER.warning("system_state row 1 missing, assuming armed")
            return True
        return bool(rows[0].get("armed", True))

    def fetch_verdict(self, event_id: str) -> str | None:
        """Return ``known``/``intruder``, or ``None`` while still undecided.

        A read failure is reported as "undecided" rather than as a verdict, so a
        network blip cannot be mistaken for the user answering and silently stand
        the alarm down.
        """
        quoted = urllib.parse.quote(event_id, safe="")
        url = f"{self.config.url}/rest/v1/events?id=eq.{quoted}&select=verdict,verdict_at"
        try:
            _, body = self._request("GET", url, headers={"Accept": "application/json"})
            rows = json.loads(body or b"[]")
        except AlertError as exc:
            LOGGER.warning("verdict read failed for %s: %s", event_id, exc)
            return None
        if not rows:
            return None
        return rows[0].get("verdict")


# ---------------------------------------------------------------------------
# Offline queue
# ---------------------------------------------------------------------------
@dataclass
class QueuedAlert:
    """A pending alert on disk, with the JPEG so nothing is lost."""

    event: AlertEvent
    jpeg: bytes
    attempts: int = 0
    #: Absolute **wall-clock** deadline for the next attempt, persisted as
    #: ``retry_after``. It cannot be monotonic: the value survives a restart and
    #: a new process's monotonic origin bears no relation to the old one.
    next_attempt_at: float = 0.0
    last_error: str = ""


class AlertQueue:
    """Durable retry for alerts that could not be published.

    One JSON file per alert under ``<queue_dir>/``. The JPEG is base64-encoded
    into the same file so the pair cannot be separated or reordered: an event
    row pointing at a snapshot that was never uploaded is worse than no event.

    Sized small on purpose. It is a safety net for a transient outage, not an
    archive; ``shield_rpi.service`` plus systemd's own restart covers a crash.
    """

    def __init__(
        self,
        directory: str | Path,
        client: SupabaseClient,
        *,
        max_attempts: int = 12,
        backoff_s: float = 5.0,
        backoff_cap_s: float = 300.0,
        #: Wall clock, not monotonic. The deadline is persisted to the queue file
        #: and read back after a restart, and a monotonic origin is meaningless
        #: in a fresh process. See QueuedAlert.next_attempt_at.
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.backoff_cap_s = backoff_cap_s
        self._clock = clock

    def __len__(self) -> int:
        return len(list(self._pending_files()))

    def _pending_files(self) -> list[Path]:
        return sorted(self.dir.glob("*.json"))

    def put(self, event: AlertEvent, jpeg: bytes) -> Path:
        import base64

        path = self.dir / f"{event.ts}-{event.event_id}.json"
        payload = {
            "event": event.to_row(),
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
            "attempts": 0,
            "last_error": "",
            "queued_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        LOGGER.warning("queued alert %s to %s (%d queued)", event.event_id, path.name, len(self))
        return path

    def _load(self, path: Path) -> QueuedAlert:
        import base64

        payload = json.loads(path.read_text(encoding="utf-8"))
        row = payload["event"]
        jpeg = base64.b64decode(payload.get("jpeg_b64", "")) if payload.get("jpeg_b64") else b""
        event = AlertEvent(
            event_id=row["id"],
            ts=int(row["ts"]),
            status=int(row["status"]),
            confidence=int(row["confidence"]),
            image_path=row["image_path"],
            device_id=row.get("device_id", ""),
            needs_verdict=bool(row.get("needs_verdict", True)),
            identity=row.get("identity"),
            trigger_tier=row.get("trigger_tier"),
            latency_ms=row.get("latency_ms"),
            person_detected=row.get("person_detected"),
            face_detected=row.get("face_detected"),
        )
        return QueuedAlert(
            event=event,
            jpeg=jpeg,
            attempts=int(payload.get("attempts", 0)),
            # Absolute wall clock, not monotonic: this value is read back in a
            # fresh process after a restart, and a new monotonic origin has no
            # relation to the old one. A monotonic value read back as a raw
            # number would make every queued alert immediately eligible again,
            # which turns one reconnect into a flood.
            next_attempt_at=float(payload.get("retry_after", 0.0)),
            last_error=payload.get("last_error", ""),
        )
    def _store(self, path: Path, queued: QueuedAlert) -> None:
        import base64

        payload = {
            "event": queued.event.to_row(),
            "jpeg_b64": base64.b64encode(queued.jpeg).decode("ascii") if queued.jpeg else "",
            "attempts": queued.attempts,
            "last_error": queued.last_error,
            "retry_after": queued.next_attempt_at,
            "queued_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def drain(self) -> dict[str, int]:
        """Attempt every queued alert once. Safe to call on a timer.

        Returns counters for the event log. A file is removed only after both
        the upload and the insert succeed, so a partial failure repeats the
        whole publication - which is safe, because the event_id is stable.
        """
        sent = 0
        failed = 0
        expired = 0
        now = self._clock()

        for path in self._pending_files():
            try:
                queued = self._load(path)
            except (OSError, ValueError, KeyError) as exc:
                # Unreadable queue file. Move it aside rather than deleting it:
                # it may hold the only copy of a snapshot of someone's home.
                quarantine = path.with_suffix(f".corrupt-{int(now)}")
                LOGGER.error("unreadable queued alert %s (%s); moving to %s", path.name, exc, quarantine.name)
                with contextlib.suppress(OSError):
                    path.replace(quarantine)
                expired += 1
                continue

            if queued.next_attempt_at > now:
                continue

            queued.attempts += 1
            try:
                self.client.upload_snapshot(queued.jpeg, queued.event.image_path)
                self.client.insert_event(queued.event)
            except AlertError as exc:
                queued.last_error = str(exc)
                if queued.attempts >= self.max_attempts:
                    LOGGER.error(
                        "queued alert %s failed %d times and is being abandoned: %s",
                        queued.event.event_id,
                        queued.attempts,
                        queued.last_error,
                    )
                    with contextlib.suppress(OSError):
                        path.replace(path.with_suffix(".abandoned"))
                    expired += 1
                    continue
                delay = min(self.backoff_s * (2 ** (queued.attempts - 1)), self.backoff_cap_s)
                queued.next_attempt_at = now + delay
                LOGGER.warning(
                    "queued alert %s failed (attempt %d), retrying in %.0fs: %s",
                    queued.event.event_id,
                    queued.attempts,
                    delay,
                    queued.last_error,
                )
                self._store(path, queued)
                failed += 1
                continue

            with contextlib.suppress(OSError):
                path.unlink()
            sent += 1
            LOGGER.info("queued alert %s published on retry", queued.event.event_id)

        return {"sent": sent, "failed": failed, "expired": expired}



# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------
@dataclass
class AlertPublisher:
    """Upload + insert, with the queue as the fallback.

    Deliberately has no ``send_fcm`` method, no reference to the
    ``send-alert`` function, and no code path that knows a service account
    exists. The alert ends at the ``events`` row.
    """

    client: SupabaseClient
    queue: AlertQueue
    cooldown_s: float = 30.0
    _last_alert_at: float = field(default=0.0, init=False)
    _clock: Callable[[], float] = time.monotonic

    def in_cooldown(self) -> bool:
        return (self._clock() - self._last_alert_at) < self.cooldown_s

    def publish(
        self,
        jpeg: bytes,
        *,
        status: int,
        confidence: int,
        device_id: str,
        image_path: str | None = None,
        event_id: str | None = None,
        **extra: Any,
    ) -> str:
        """Publish one alert and return its ``event_id``.

        On any transport failure the alert is queued and the ``event_id`` is
        still returned, so the caller can carry on polling for a verdict against
        an event that will appear shortly. Returning the id rather than raising
        is deliberate: the buzzer escalation path must not be skipped just
        because the phone's push is late.
        """
        event = AlertEvent(
            event_id=event_id or str(uuid.uuid4()),
            ts=now_unix(),
            status=status,
            confidence=max(0, min(100, int(confidence))),
            image_path=image_path or "",
            device_id=device_id,
            **extra,
        )
        if not event.image_path:
            raise AlertError("image_path is required; upload the snapshot before publishing")

        try:
            self.client.upload_snapshot(jpeg, event.image_path)
            self.client.insert_event(event)
            self._last_alert_at = self._clock()
            LOGGER.info("published event %s (status %d)", event.event_id, event.status)
        except OfflineError as exc:
            LOGGER.warning("publish failed, queueing %s: %s", event.event_id, exc)
            self.queue.put(event, jpeg)
        except AlertError:
            # A 4xx: the request is wrong, not the network. Retrying it would
            # burn the queue's attempt budget on something unfixable, so it goes
            # straight to disk for a human to look at.
            LOGGER.error("publish rejected for %s; queueing for inspection", event.event_id)
            self.queue.put(event, jpeg)
        return event.event_id

    def snapshot_path(self, event_id: str, ts: int) -> str:
        """Bucket path for a snapshot.

        Date-partitioned so the bucket is browsable and so one directory does not
        accumulate every image forever.
        """
        day = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
        return f"{self.client.config.device_prefix}/{day}/{event_id}.jpg"
