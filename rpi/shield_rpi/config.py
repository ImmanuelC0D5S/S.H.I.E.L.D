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
class BackendConfig:
    url: str
    token: str
    device_id: str
    poll_interval_s: float
    ack_deadline_s: float

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(f"backend.url must be http(s), got {self.url!r}")
        if not self.token:
            raise ConfigError("backend.token resolved empty; is SHIELD_API_TOKEN set?")
        if self.ack_deadline_s <= self.poll_interval_s:
            raise ConfigError(
                "backend.ack_deadline_s must exceed poll_interval_s "
                f"({self.ack_deadline_s} <= {self.poll_interval_s})"
            )


@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    detector: DetectorConfig
    recognizer: RecognizerConfig
    link: LinkConfig
    backend: BackendConfig
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
    back = _require(raw, "backend", "<root>")

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
        backend=BackendConfig(
            url=str(_require(back, "url", "backend")).rstrip("/"),
            token=str(_require(back, "token", "backend")),
            device_id=str(_require(back, "device_id", "backend")),
            poll_interval_s=float(back.get("poll_interval_s", 5)),
            ack_deadline_s=float(back.get("ack_deadline_s", 30)),
        ),
        source_path=config_path,
    )