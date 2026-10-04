"""Compute-node entrypoint. ``python -m shield_rpi.service``.

Wires the pieces PROTOCOL.md 2.7 and 4.2 describe and runs them:

* the framed TCP link server, which is the only real path in;
* the camera, detector, recogniser and pipeline from section 4.1;
* the Supabase alert publisher (upload + insert, no FCM - DECISIONS.md D-044);
* the verdict watcher, which polls while an event is pending and then stops;
* the offline queue drain on a timer.

The ordering in :meth:`Service.handle_frame` is the safety-critical part of
PROTOCOL.md 4.2: **the ``reply`` goes out before the network alert work
starts.** The buzzer must not wait on the internet. If Supabase is unreachable
the node still answers the sensor immediately and queues the alert for later, so
a dead uplink degrades to "the alert arrives late", never to "the buzzer never
sounded".

Usage::

    # On the Pi, with the ESP32 present:
    python -m shield_rpi.service

    # On the laptop, no hardware: drive it from another shell.
    python -m shield_rpi.service --shim --no-camera --fake-trigger
    curl -X POST localhost:8811/trigger -d '{}'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2

from shield_rpi.alerts import (
    STATUS_ALERT,
    AlertPublisher,
    AlertQueue,
    SupabaseClient,
)
from shield_rpi.camera import Camera, CameraError, encode_jpeg
from shield_rpi.config import Config, ConfigError, load_config
from shield_rpi.detector import Detector, DetectorError
from shield_rpi.http_shim import TriggerShim
from shield_rpi.link import LinkConnection, LinkServer, new_message
from shield_rpi.pipeline import Pipeline, Verdict, annotate_snapshot
from shield_rpi.recognizer import Recogniser
from shield_rpi.verdict import VerdictWatcher

LOGGER = logging.getLogger("shield.service")

#: PROTOCOL.md 2.4.1 / 2.4.2 status codes.
STATUS_CLEAR = 0
STATUS_KNOWN = 1
STATUS_ALERT_STATUS = 2

#: How often the offline queue is drained. Short enough that a queue cleared by
#: a reconnect is picked up promptly, long enough not to hammer a dead uplink.
QUEUE_DRAIN_INTERVAL_S = 15.0

#: Linear factor applied to the stored snapshot each time it still exceeds the
#: byte cap after the max-edge clamp.
SNAPSHOT_SHRINK = 0.8


def load_env(path: str | Path) -> None:
    """Load ``.env`` into the environment without overwriting real variables.

    Deliberately hand-rolled rather than pulling in python-dotenv: the format is
    ``KEY=value``, and ``os.environ.setdefault`` keeps a value the operator
    exported for this one run, which is how systemd overrides are tested.
    """
    env_path = Path(path)
    if not env_path.is_file():
        LOGGER.warning("%s not found; relying on the ambient environment", env_path)
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if value.endswith("\r"):
            value = value[:-1]
        os.environ.setdefault(name, value)


class Service:
    """Everything the compute node runs, in one object so tests can drive it."""

    def __init__(self, config: Config, *, shim_port: int = 8811) -> None:
        self.config = config
        self.client = SupabaseClient(config.supabase)
        self.queue = AlertQueue(
            config.supabase.queue_dir,
            self.client,
            max_attempts=config.supabase.queue_max_attempts,
        )
        self.publisher = AlertPublisher(
            client=self.client,
            queue=self.queue,
            cooldown_s=config.alert.cooldown_s,
        )
        self.detector = Detector(config.detector)
        self.recogniser = Recogniser(config.recognizer)
        self.pipeline = Pipeline(config, self.detector, self.recogniser)
        self.verdicts = VerdictWatcher(config.verdict, self.client)
        self.link = LinkServer(
            config.link.host,
            config.link.port,
            config.link.hmac_key,
            frame_timeout_s=config.link.frame_timeout_s,
        )
        self.camera: Camera | None = None
        self.shim: TriggerShim | None = None
        self._shim_port = shim_port
        self._stop = threading.Event()
        self._active: LinkConnection | None = None
        self._worker: threading.Thread | None = None
        self.last_event_log: dict[str, Any] | None = None

    # -- frames ------------------------------------------------------------

    def handle_frame(self, message: dict[str, object], connection: LinkConnection) -> None:
        """Dispatch one verified frame from the sensor node."""
        message_type = message.get("type")

        if message_type == "heartbeat":
            connection.send(new_message("heartbeat_ack", echo=message.get("id")))
            return

        if message_type == "alarm_ack":
            LOGGER.info("sensor acknowledged the buzzer: %s", message.get("reason"))
            return

        if message_type != "trigger":
            LOGGER.warning("ignoring unknown message type %r", message_type)
            return

        # PROTOCOL.md 4.2: answer first, alert second. The pipeline itself runs
        # here (it is the reply the node is waiting for) but the network work is
        # handed to a worker thread.
        try:
            reply = self.run_pipeline(message)
        except CameraError as exc:
            LOGGER.error("camera unavailable: %s", exc)
            connection.send(
                new_message(
                    "reply",
                    trigger_id=message.get("id"),
                    status=STATUS_ALERT_STATUS,
                    confidence=0,
                )
            )
            return
        except Exception:  # noqa: BLE001 - the node must always get an answer
            LOGGER.exception("pipeline failed")
            connection.send(
                new_message(
                    "reply",
                    trigger_id=message.get("id"),
                    status=STATUS_ALERT_STATUS,
                    confidence=0,
                )
            )
            return

        try:
            connection.send(
                new_message(
                    "reply",
                    trigger_id=message.get("id"),
                    status=reply["status"],
                    confidence=reply["confidence"],
                )
            )
        except Exception:  # noqa: BLE001 - nothing useful left to do
            LOGGER.exception("could not send reply")

        if reply["status"] != STATUS_ALERT_STATUS:
            self.record_event({"type": "reply", "trigger_id": message.get("id"), **reply})
            return

        self._start_alert_work(reply)

    def _start_alert_work(self, reply: dict[str, Any]) -> None:
        """Publish the alert and then wait for the verdict, off the link thread.

        PROTOCOL.md 4.2 again: the network alert must not delay the next frame,
        and the verdict poll can legitimately take ``verdict.timeout_s``.
        """

        def worker() -> None:
            try:
                event_id = self.publisher.publish(
                    reply["jpeg"],
                    status=STATUS_ALERT,
                    confidence=reply["confidence"],
                    device_id=self.config.supabase.device_prefix,
                    identity=reply["identity"],
                    trigger_tier=reply.get("tier"),
                    latency_ms=int(reply["latency_ms"]),
                    person_detected=reply["person_detected"],
                    face_detected=reply["face_detected"],
                    event_id=reply["event_id"],
                )
                self.record_event({"type": "alert_published", "event_id": event_id})
                result = self.verdicts.watch(event_id, self._active)
                self.record_event(
                    {"type": "verdict", "event_id": event_id, "escalation": result.escalation.value}
                )
            except Exception:  # noqa: BLE001
                LOGGER.exception("alert worker failed")
            finally:
                self._worker = None

        if self._worker is not None and self._worker.is_alive():
            LOGGER.warning("an alert is already pending; dropping this one")
            return
        self._worker = threading.Thread(target=worker, name="shield-alert", daemon=True)
        self._worker.start()

    # -- pipeline ----------------------------------------------------------

    def run_pipeline(self, message: dict[str, object] | None = None) -> dict[str, Any]:
        """Run one detection pass and, on an alert, prepare the snapshot.

        Returns the reply body plus, for an alert, the encoded JPEG so the caller
        can publish it without re-reading the camera.
        """

        tier = str(message.get("tier")) if message else None

        if not self.client.is_armed():
            LOGGER.info("disarmed via system_state; replying status 0")
            return {
                "status": STATUS_CLEAR,
                "confidence": 0,
                "identity": None,
                "person_detected": False,
                "face_detected": False,
                "latency_ms": 0.0,
                "event_id": str(uuid.uuid4()),
                "jpeg": b"",
                "tier": tier,
            }

        if self.publisher.in_cooldown():
            LOGGER.info("inside the %.0fs cooldown; replying status 0", self.config.alert.cooldown_s)
            return {
                "status": STATUS_CLEAR,
                "confidence": 0,
                "identity": None,
                "person_detected": False,
                "face_detected": False,
                "latency_ms": 0.0,
                "event_id": str(uuid.uuid4()),
                "jpeg": b"",
                "tier": tier,
            }

        camera = self._require_camera()
        frames = camera.burst()
        verdict: Verdict = self.pipeline.classify(frames)

        LOGGER.info(
            "verdict status=%d confidence=%d identity=%s reason=%s latency=%.0fms "
            "(PROTOCOL.md 4.3 budget is 1000ms, UNMEASURED at 1080p on the Pi)",
            verdict.status,
            verdict.confidence,
            verdict.identity,
            verdict.reason,
            verdict.latency_ms,
        )
        if verdict.latency_ms > 1000.0:
            LOGGER.warning(
                "trigger-to-reply took %.0fms, over the 1s budget. PLAN.md records this as "
                "unmeasured at 1080p; measure it before assuming the budget holds.",
                verdict.latency_ms,
            )

        event_id = str(uuid.uuid4())
        reply: dict[str, Any] = {
            "status": verdict.status,
            "confidence": verdict.confidence,
            "identity": verdict.identity,
            "person_detected": verdict.person_detected,
            "face_detected": verdict.face_detected,
            "latency_ms": verdict.latency_ms,
            "event_id": event_id,
            "tier": tier,
        }

        if verdict.status == STATUS_ALERT_STATUS:
            reply["jpeg"] = self._encode_snapshot(frames[verdict.snapshot_index], verdict)
        else:
            reply["jpeg"] = b""

        self.record_event({"type": "verdict", "event_id": event_id, **{
            k: v for k, v in reply.items() if k != "jpeg"
        }})
        return reply

    def _encode_snapshot(self, frame, verdict: Verdict) -> bytes:
        """Encode the stored snapshot within the byte cap.

        Two stages, in this order. First the long edge is clamped to
        ``alert.snapshot_max_edge_px`` - that is a quality decision, not a
        fallback: the stored image is what a human looks at later to decide
        whether an alert was real (PLAN.md, "the 1080p latency question"). Only
        if that is still over the cap does it shrink, because
        ``alerts.upload_snapshot`` refuses anything larger and an oversized
        snapshot is a lost intrusion event.
        """
        annotated = annotate_snapshot(frame, verdict)
        max_edge = self.config.alert.snapshot_max_edge_px
        height, width = annotated.shape[:2]
        if max(width, height) > max_edge:
            scale = max_edge / max(width, height)
            annotated = cv2.resize(
                annotated,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
            LOGGER.info("snapshot clamped from %dx%d to fit %dpx", width, height, max_edge)

        quality = self.config.alert.snapshot_jpeg_quality
        limit = self.config.supabase.max_snapshot_bytes

        candidate = annotated
        for _ in range(4):
            jpeg = encode_jpeg(candidate, quality)
            if len(jpeg) <= limit:
                return jpeg
            height, width = candidate.shape[:2]
            new_w, new_h = int(width * SNAPSHOT_SHRINK), int(height * SNAPSHOT_SHRINK)
            LOGGER.warning(
                "snapshot %d bytes over the %d cap; retrying at %dx%d",
                len(jpeg),
                limit,
                new_w,
                new_h,
            )
            candidate = cv2.resize(annotated, (new_w, new_h), interpolation=cv2.INTER_AREA)
            quality = max(60, quality - 10)

        jpeg = encode_jpeg(candidate, quality)
        if len(jpeg) > limit:
            LOGGER.error(
                "snapshot is still %d bytes after 4 reductions; publishing anyway and "
                "letting the storage layer reject it rather than losing the event",
                len(jpeg),
            )
        return jpeg

    # -- lifecycle ---------------------------------------------------------

    def _require_camera(self) -> Camera:
        if self.camera is None:
            raise CameraError("camera is not open; start the service without --no-camera")
        return self.camera

    def open_camera(self) -> None:
        camera = Camera(self.config.camera)
        camera.open()
        self.camera = camera

    def start(self, *, open_camera: bool = True, shim: bool = False, fake_trigger: bool = False) -> None:
        """Start every background thread and block until stopped."""
        if open_camera:
            self.open_camera()

        threads: list[threading.Thread] = []

        link_thread = threading.Thread(
            target=self.link.serve_forever, args=(self.handle_frame,), name="shield-link", daemon=True
        )
        link_thread.start()
        threads.append(link_thread)

        queue_thread = threading.Thread(target=self._drain_loop, name="shield-queue", daemon=True)
        queue_thread.start()
        threads.append(queue_thread)

        if shim or fake_trigger:
            self.shim = TriggerShim(
                trigger_fn=self._shim_trigger,
                health_fn=self.health,
                state_fn=self.state,
                port=self._shim_port,
            )
            self.shim.start()

        if fake_trigger:
            trigger_thread = threading.Thread(
                target=self._fake_trigger_loop, name="shield-fake", daemon=True
            )
            trigger_thread.start()
            threads.append(trigger_thread)

        LOGGER.info("shield service running; ctrl-c or SIGTERM to stop")
        try:
            while not self._stop.is_set():
                time.sleep(0.3)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self.shim is not None:
            self.shim.stop()
        self.link.shutdown()
        if self.camera is not None:
            self.camera.close()
        LOGGER.info("shield service stopped")

    def request_stop(self, *_args: object) -> None:
        self._stop.set()

    # -- helpers -----------------------------------------------------------

    def _drain_loop(self) -> None:
        while not self._stop.wait(QUEUE_DRAIN_INTERVAL_S):
            if len(self.queue) == 0:
                continue
            try:
                result = self.queue.drain()
                if any(result.values()):
                    self.record_event({"type": "queue_drain", **result})
            except Exception:  # noqa: BLE001
                LOGGER.exception("queue drain failed")

    def _fake_trigger_loop(self) -> None:
        """Synthesise a trigger every 30s. For laptop bring-up only.

        ``--fake-trigger`` is explicitly a development affordance; the ESP32 is
        the only thing that should be producing triggers in a real deployment.
        """
        while not self._stop.wait(30.0):
            try:
                self._shim_trigger({})
            except Exception:  # noqa: BLE001
                LOGGER.exception("fake trigger failed")

    def _shim_trigger(self, body: dict[str, Any]) -> dict[str, Any]:
        tier = body.get("tier")
        reply = self.run_pipeline({"tier": tier} if tier else None)
        payload = {k: v for k, v in reply.items() if k != "jpeg"}
        if reply["status"] == STATUS_ALERT_STATUS:
            self._start_alert_work(reply)
        return payload

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "sensor_online": self.link.sensor_online,
            "frames_in": self.link.stats.frames_in,
            "link_faults": self.link.stats.faults,
            "queue_depth": len(self.queue),
            "camera": (
                str(self.camera.actual) if self.camera is not None else None
            ),
        }

    def state(self) -> dict[str, Any]:
        return {
            "armed": self.client.is_armed(),
            "enrolled": self.recogniser.names,
            "cooldown_s": self.config.alert.cooldown_s,
            "verdict_timeout_s": self.config.verdict.timeout_s,
            "supabase_url": self.config.supabase.url,
            "last_event": self.last_event_log,
        }

    def record_event(self, record: dict[str, Any]) -> None:
        """Append one line to ``events.jsonl`` (gitignored).

        PROTOCOL.md 2.8 requires a ``link_down`` record on the sensor heartbeat
        timing out; everything here is written to the same stream so the local
        log and the database can be diffed when the two disagree.
        """
        self.last_event_log = record
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "device_id": self.config.supabase.device_prefix,
            **record,
        }
        try:
            with Path("events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, default=str) + "\n")
        except OSError as exc:
            LOGGER.warning("could not append to events.jsonl: %s", exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m shield_rpi.service",
        description="S.H.I.E.L.D compute node. See docs/PROTOCOL.md.",
    )
    parser.add_argument("--config", help="path to config.yaml (default: the shipped one)")
    parser.add_argument(
        "--env-file", default=".env", help="dotenv file to load before starting"
    )
    parser.add_argument(
        "--no-camera",
        action="store_true",
        help="do not open the camera; only /trigger via the shim will fail",
    )
    parser.add_argument(
        "--shim",
        action="store_true",
        help=f"serve the loopback trigger shim on 127.0.0.1:8811 (default port {8811})",
    )
    parser.add_argument("--shim-port", type=int, default=8811)
    parser.add_argument(
        "--fake-trigger",
        action="store_true",
        help="synthesise a trigger every 30s; laptop bring-up only",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    load_env(args.env_file)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOGGER.error("configuration problem: %s", exc)
        return 2

    try:
        service = Service(config, shim_port=args.shim_port)
    except DetectorError as exc:
        LOGGER.error("detector unavailable: %s", exc)
        return 2

    signal.signal(signal.SIGINT, service.request_stop)
    signal.signal(signal.SIGTERM, service.request_stop)

    try:
        service.start(
            open_camera=not args.no_camera, shim=args.shim, fake_trigger=args.fake_trigger
        )
    except CameraError as exc:
        LOGGER.error("cannot start: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
