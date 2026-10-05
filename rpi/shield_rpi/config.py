"""Typed configuration for S.H.I.E.L.D.

Single source of truth is ``docs/PROTOCOL.md`` section 7. Defaults live in
``config.yaml``; secrets live only in ``.env`` and are interpolated here as
``${VAR}``.

No camera geometry is hardcoded in Python. If a number appears in code it is a
protocol constant, not a device setting.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or inconsistent."""


def _substitute_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` in strings from the environment.

    A missing variable raises rather than yielding an empty string: a config that
    silently expands to a blank token or key is worse than one that refuses to
    start.
    """
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                return os.environ[name]
            except KeyError as exc:
                raise ConfigError(f"{name} is not set; add it to .env") from exc

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


@dataclass(frozen=True)
class CameraConfig:
    device: int
    width: int
    height: int
    fourcc: str = "MJPG"
    warmup_frames: int = 10
    burst_frames: int = 5
    stale_after_s: float = 2.0

    def __post_init__(self) -> None:
        if self.device < 0:
            raise ConfigError(f"camera.device must be >= 0, got {self.device}")
        if self.width <= 0 or self.height <= 0:
            raise ConfigError(f"camera geometry must be positive, got {self.width}x{self.height}")
        if len(self.fourcc) != 4:
            raise ConfigError(f"camera.fourcc must be 4 chars, got {self.fourcc!r}")
        if self.warmup_frames < 0:
            raise ConfigError("camera.warmup_frames must be >= 0")
        if self.burst_frames < 1:
            raise ConfigError("camera.burst_frames must be >= 1")


@dataclass(frozen=True)
class DetectorConfig:
    face_model: str
    person_model: str
    imgsz: int
    conf: float

    def __post_init__(self) -> None:
        if not 0.0 < self.conf <= 1.0:
            raise ConfigError(f"detector.conf must be in (0, 1], got {self.conf}")
        if self.imgsz < 32:
            raise ConfigError(f"detector.imgsz must be >= 32, got {self.imgsz}")


@dataclass(frozen=True)
class RecognizerConfig:
    faces_dir: str
    match_distance: float
    alert_confidence: int

    def __post_init__(self) -> None:
        if self.match_distance <= 0:
            raise ConfigError("recognizer.match_distance must be > 0")
        if not 0 <= self.alert_confidence <= 100:
            raise ConfigError("recognizer.alert_confidence must be 0-100")


@dataclass(frozen=True)
class LinkConfig:
    host: str
    port: int
    heartbeat_s: float
    reply_timeout_s: float
    frame_timeout_s: float
    max_body: int
    hmac_key: str

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"link.port must be 1-65535, got {self.port}")
        if self.reply_timeout_s <= 0 or self.heartbeat_s <= 0:
            raise ConfigError("link timeouts must be > 0")
        if self.max_body < 64:
            raise ConfigError("link.max_body must be >= 64")
        if not self.hmac_key:
            raise ConfigError("link.hmac_key resolved empty; is SHIELD_LINK_KEY set?")


@dataclass(frozen=True)
class SupabaseConfig:
    """Where alerts are published. DECISIONS.md D-040.

    ``key`` is the ``shield_rpi`` role's bearer token, not the service-role key.
    The service-role key bypasses RLS, which would make the policies in
    ``supabase/migrations/20261003090100_shield_roles_grants.sql`` decorative.

    ``anon_key`` is a separate, non-secret project key and is **not** optional in
    practice. PostgREST validates the ``apikey`` header against the project's own
    keys and rejects anything else with 401 "Invalid API key" -- including a
    perfectly valid role token. The role travels in ``Authorization`` instead, so
    the two headers carry different things and neither can stand in for the other.
    """

    url: str
    key: str
    bucket: str
    device_prefix: str
    anon_key: str = ""
    timeout_s: float = 10.0
    max_snapshot_bytes: int = 524288
    queue_dir: str = ""
    queue_max_attempts: int = 12

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(f"supabase.url must be http(s), got {self.url!r}")
        if not self.key:
            raise ConfigError(
                "supabase.key resolved empty; is SUPABASE_RPI_TOKEN set "
                "(mint it with tools/mint_role_token.py)?"
            )
        if not self.anon_key:
            raise ConfigError(
                "supabase.anon_key resolved empty; it is the project key PostgREST "
                "expects in the apikey header and is NOT the role token"
            )
        if not self.bucket:
            raise ConfigError("supabase.bucket must not be empty")
        if self.timeout_s <= 0:
            raise ConfigError("supabase.timeout_s must be > 0")
        if self.max_snapshot_bytes <= 0:
            raise ConfigError("supabase.max_snapshot_bytes must be > 0")
        if self.queue_max_attempts < 1:
            raise ConfigError("supabase.queue_max_attempts must be >= 1")


@dataclass(frozen=True)
class AlertConfig:
    """How eager the alert path is."""

    cooldown_s: float = 30.0
    #: PROTOCOL.md 3.3 caps a snapshot at 512 KB; above that the storage bucket
    #: rejects the upload with an opaque 413.
    snapshot_jpeg_quality: int = 80
    #: Longest edge of the stored snapshot. The stored image is what a human
    #: looks at later to decide whether an alert was real, so it is kept sharp
    #: rather than shrunk to save bytes (PLAN.md, "the 1080p latency question").
    snapshot_max_edge_px: int = 1920

    def __post_init__(self) -> None:
        if self.cooldown_s < 0:
            raise ConfigError("alert.cooldown_s must be >= 0")
        if not 1 <= self.snapshot_jpeg_quality <= 100:
            raise ConfigError("alert.snapshot_jpeg_quality must be 1-100")
        if self.snapshot_max_edge_px < 320:
            raise ConfigError("alert.snapshot_max_edge_px must be >= 320")


@dataclass(frozen=True)
class VerdictConfig:
    """How long to wait for the user to answer before escalating.

    PROTOCOL.md 3.7: the poll stops as soon as a verdict appears, when the
    event is resolved, or after 10 minutes, whichever comes first.
    """

    timeout_s: float = 60.0
    poll_interval_s: float = 2.0
    hard_stop_s: float = 600.0

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ConfigError("verdict.timeout_s must be > 0")
        if self.poll_interval_s <= 0:
            raise ConfigError("verdict.poll_interval_s must be > 0")
        if self.hard_stop_s < self.timeout_s:
            raise ConfigError(
                "verdict.hard_stop_s must be >= verdict.timeout_s "
                f"({self.hard_stop_s} < {self.timeout_s})"
            )


@dataclass(frozen=True)
class SerialConfig:
    """The UART demo path. ``shield-serial-bridge-prompt.md``.

    Deliberately optional. ``port`` is empty by default so a normal deployment
    never opens a serial port, and the base install does not need pyserial --
    the import happens inside ``SerialBridge._open``, so a missing dependency is
    only an error for someone who actually asked for ``--serial``.
    """

    port: str = ""
    baud: int = 115200
    reconnect_s: float = 2.0
    read_timeout_s: float = 0.2

    def __post_init__(self) -> None:
        if not self.port:
            return  # serial mode is off
        if self.baud <= 0:
            raise ConfigError(f"serial.baud must be > 0, got {self.baud}")
        if self.reconnect_s <= 0:
            raise ConfigError("serial.reconnect_s must be > 0")
        if self.read_timeout_s <= 0:
            raise ConfigError("serial.read_timeout_s must be > 0")

    @property
    def enabled(self) -> bool:
        return bool(self.port)


@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    detector: DetectorConfig
    recognizer: RecognizerConfig
    link: LinkConfig
    supabase: SupabaseConfig
    alert: AlertConfig
    verdict: VerdictConfig
    #: Absent from config.yaml is fine; the default is serial mode off.
    serial: SerialConfig = field(default_factory=SerialConfig)
    source_path: Path = field(default=DEFAULT_CONFIG_PATH)


def load_camera_config(path: str | Path | None = None) -> CameraConfig:
    """Load only the camera section.

    Dataset capture and enrolment run long before the deployment secrets exist,
    and neither needs the link key or the backend token. Requiring unrelated
    secrets to open a camera would mean those tools cannot run until Phase 6 has
    produced a live backend, so the camera section is readable on its own.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "camera" not in raw:
        raise ConfigError(f"{config_path} is missing the camera section")

    cam = _substitute_env(raw["camera"])
    return CameraConfig(
        device=int(_require(cam, "device", "camera")),
        width=int(_require(cam, "width", "camera")),
        height=int(_require(cam, "height", "camera")),
        fourcc=str(cam.get("fourcc", "MJPG")),
        warmup_frames=int(cam.get("warmup_frames", 10)),
        burst_frames=int(cam.get("burst_frames", 5)),
        stale_after_s=float(cam.get("stale_after_s", 2.0)),
    )


def _require(section: dict[str, Any], key: str, where: str) -> Any:
    if key not in section:
        raise ConfigError(f"missing {where}.{key} in config")
    return section[key]


def _resolve_path(raw: str, base: Path) -> str:
    """Resolve a config-relative path to an absolute, normalised path.

    Normalising matters: a bare ``base / "../models"`` keeps a literal ``..``
    segment, which works but shows up verbatim in log lines and error messages.
    """
    path = Path(raw)
    return str(path if path.is_absolute() else (base / path).resolve())


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate configuration.

    Relative paths in the file are resolved against the config file's directory,
    so the app works regardless of the working directory it was launched from.
    """
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{config_path} is empty; it must contain a mapping at the top level")
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")

    raw = _substitute_env(raw)
    base = config_path.parent

    cam = _require(raw, "camera", "<root>")
    det = _require(raw, "detector", "<root>")
    rec = _require(raw, "recognizer", "<root>")
    link = _require(raw, "link", "<root>")
    supa = _require(raw, "supabase", "<root>")
    alert = raw.get("alert") or {}
    verd = raw.get("verdict") or {}
    ser = raw.get("serial") or {}

    return Config(
        camera=CameraConfig(
            device=int(_require(cam, "device", "camera")),
            width=int(_require(cam, "width", "camera")),
            height=int(_require(cam, "height", "camera")),
            fourcc=str(cam.get("fourcc", "MJPG")),
            warmup_frames=int(cam.get("warmup_frames", 10)),
            burst_frames=int(cam.get("burst_frames", 5)),
            stale_after_s=float(cam.get("stale_after_s", 2.0)),
        ),
        detector=DetectorConfig(
            face_model=_resolve_path(str(_require(det, "face_model", "detector")), base),
            person_model=_resolve_path(str(_require(det, "person_model", "detector")), base),
            imgsz=int(_require(det, "imgsz", "detector")),
            conf=float(_require(det, "conf", "detector")),
        ),
        recognizer=RecognizerConfig(
            faces_dir=_resolve_path(str(_require(rec, "faces_dir", "recognizer")), base),
            match_distance=float(_require(rec, "match_distance", "recognizer")),
            alert_confidence=int(_require(rec, "alert_confidence", "recognizer")),
        ),
        link=LinkConfig(
            host=str(_require(link, "host", "link")),
            port=int(_require(link, "port", "link")),
            heartbeat_s=float(link.get("heartbeat_s", 2)),
            reply_timeout_s=float(link.get("reply_timeout_s", 5)),
            frame_timeout_s=float(link.get("frame_timeout_s", 5)),
            max_body=int(link.get("max_body", 8192)),
            hmac_key=str(_require(link, "hmac_key", "link")),
        ),
        supabase=SupabaseConfig(
            url=str(_require(supa, "url", "supabase")).rstrip("/"),
            key=str(_require(supa, "key", "supabase")),
            anon_key=str(_require(supa, "anon_key", "supabase")),
            bucket=str(supa.get("bucket", "snapshots")),
            device_prefix=str(supa.get("device_prefix", "shield")),
            timeout_s=float(supa.get("timeout_s", 10)),
            max_snapshot_bytes=int(supa.get("max_snapshot_bytes", 524288)),
            queue_dir=(
                _resolve_path(str(supa["queue_dir"]), base)
                if supa.get("queue_dir")
                else str((base / "../runs/pending").resolve())
            ),
            queue_max_attempts=int(supa.get("queue_max_attempts", 12)),
        ),
        alert=AlertConfig(
            cooldown_s=float(alert.get("cooldown_s", 30)),
            snapshot_jpeg_quality=int(alert.get("snapshot_jpeg_quality", 80)),
            snapshot_max_edge_px=int(alert.get("snapshot_max_edge_px", 1920)),
        ),
        verdict=VerdictConfig(
            timeout_s=float(verd.get("timeout_s", 60)),
            poll_interval_s=float(verd.get("poll_interval_s", 2)),
            hard_stop_s=float(verd.get("hard_stop_s", 600)),
        ),
        serial=SerialConfig(
            # A YAML `port:` with nothing after it parses as None, not "".
            # Coerce rather than calling .strip() on None, which is the shape
            # that actually ships since the section is written with port: "".
            port=str(ser.get("port") or "").strip(),
            baud=int(ser.get("baud", 115200)),
            reconnect_s=float(ser.get("reconnect_s", 2)),
            read_timeout_s=float(ser.get("read_timeout_s", 0.2)),
        ),
        source_path=config_path,
    )