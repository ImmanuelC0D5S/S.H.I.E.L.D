"""Configuration loading and validation tests.

Host-only, no hardware. Covers ${ENV} interpolation, path resolution relative to
the config file, and the validation that must refuse to start rather than fail
later with a confusing error.

Phase 2 gate evidence for `shield_rpi.config`.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from shield_rpi.config import ConfigError, load_camera_config, load_config

CONFIG_DIR = Path(__file__).resolve().parents[1] / "shield_rpi"


def write_config(tmp_path: Path, body: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


BASE = """
    camera:
      device: 0
      width: 640
      height: 480
    detector:
      face_model: ../models/face.pt
      person_model: yolov8n.pt
      imgsz: 640
      conf: 0.5
    recognizer:
      faces_dir: ../known_faces
      match_distance: 0.5
      alert_confidence: 50
    link:
      host: 192.168.50.1
      port: 8765
      hmac_key: ${SHIELD_LINK_KEY}
    backend:
      url: ${SHIELD_BACKEND_URL}
      token: ${SHIELD_API_TOKEN}
      device_id: ${SHIELD_DEVICE_ID}
"""


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHIELD_LINK_KEY", "k" * 32)
    monkeypatch.setenv("SHIELD_BACKEND_URL", "https://shield.example")
    monkeypatch.setenv("SHIELD_API_TOKEN", "tok")
    monkeypatch.setenv("SHIELD_DEVICE_ID", "shield-01")


# -- happy path -----------------------------------------------------------


def test_loads_shipped_config():
    config = load_config(CONFIG_DIR / "config.yaml")
    # The device index is host-specific and changes when a camera is plugged or
    # unplugged, so assert validity rather than a specific number (D-025).
    assert config.camera.device >= 0
    assert config.detector.imgsz == 640
    assert config.link.port == 8765
    assert config.backend.device_id == "shield-01"


def test_pad_box_covers_landmark_extent():
    """Padding must restore whole-face coverage (D-020).

    Whether a box covers the whole face cannot be eyeballed on every capture run,
    and a too-tight box silently teaches the detector to frame only the eyes - the
    exact failure that voided the first dataset. This pins the relationship that
    matters, using the real geometry measured on the built-in camera: the raw HOG
    box (223x223) against the 68-point landmark extent it must contain (~218x229).

    The landmark extent is the ground truth for "where the face actually is": it
    reaches the chin and the outer eye corners, which the HOG box trims. A padded
    box containing that extent on both axes restores forehead-to-chin, ear-to-ear
    framing. If padding is ever reduced, this test fails instead of the dataset
    quietly degrading.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    from capture_dataset import pad_box

    # Raw HOG box and landmark extent, both measured from a live 1280x720 frame
    # on device 0 (see D-020 / HANDOFF verification notes).
    hog = (457, 260, 223, 223)
    landmarks = (457, 259, 218, 229)

    lx, ly, lw, lh = landmarks
    px, py, pw, ph = pad_box(hog, 1280, 720, 0.25)

    overlap_x = min(px + pw, lx + lw) - max(px, lx)
    overlap_y = min(py + ph, ly + lh) - max(py, ly)

    assert overlap_x >= lw, f"padded box misses landmark width: {overlap_x} < {lw}"
    assert overlap_y >= lh, f"padded box misses landmark height: {overlap_y} < {lh}"
    # And it must stay on-image, or the normalised YOLO label goes out of 0..1.
    assert px >= 0 and py >= 0
    assert px + pw <= 1280 and py + ph <= 720


def test_camera_geometry_matches_deployment_target():
    """Shipped config targets PROTOCOL section 4.1 geometry: 1920x1080.

    Verified on hardware via tools/camera_check.py (D-028). Re-verify whenever
    the camera changes, and note the index shifts with plug order (D-025).
    """
    config = load_config(CONFIG_DIR / "config.yaml")
    assert (config.camera.width, config.camera.height) == (1920, 1080)
    assert config.camera.fourcc == "MJPG"


def test_defaults_are_filled_in(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    assert config.camera.fourcc == "MJPG"
    assert config.camera.warmup_frames == 10
    assert config.camera.burst_frames == 5
    assert config.link.max_body == 8192
    assert config.backend.poll_interval_s == 5.0


def test_relative_paths_resolve_against_config_file(tmp_path):
    """Launching from any working directory must find the model and faces."""
    config = load_config(write_config(tmp_path, BASE))
    expected_model = str((tmp_path / "../models/face.pt").resolve())
    expected_faces = str((tmp_path / "../known_faces").resolve())
    assert config.detector.face_model == expected_model
    assert config.recognizer.faces_dir == expected_faces
    assert Path(config.detector.face_model).is_absolute(), "model path must be absolute"
    assert ".." not in config.detector.face_model, "path must be normalised"


# -- env interpolation ----------------------------------------------------


def test_env_vars_are_interpolated(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    assert config.link.hmac_key == "k" * 32
    assert config.backend.url == "https://shield.example"
    assert config.backend.token == "tok"


def test_missing_env_var_refuses_to_start(tmp_path, monkeypatch):
    """A blank HMAC key must fail loudly, not silently disable verification."""
    monkeypatch.delenv("SHIELD_LINK_KEY")
    with pytest.raises(ConfigError, match="SHIELD_LINK_KEY is not set"):
        load_config(write_config(tmp_path, BASE))


def test_empty_backend_url_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIELD_BACKEND_URL", "")
    with pytest.raises(ConfigError, match="backend.url"):
        load_config(write_config(tmp_path, BASE))


# -- malformed input ------------------------------------------------------


def test_missing_file():
    with pytest.raises(ConfigError, match="config file not found"):
        load_config("/nonexistent/config.yaml")


def test_invalid_yaml(tmp_path):
    path = write_config(tmp_path, "camera: [unclosed\n  bad: :")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_empty_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="mapping at the top level"):
        load_config(write_config(tmp_path, ""))


def test_missing_section(tmp_path):
    body = BASE.split("    link:")[0]
    with pytest.raises(ConfigError, match=r"missing <root>\.link"):
        load_config(write_config(tmp_path, body))


def test_missing_key(tmp_path):
    body = BASE.replace("      device: 0\n", "")
    with pytest.raises(ConfigError, match="missing camera.device"):
        load_config(write_config(tmp_path, body))


# -- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("      device: -1\n      width: 640\n      height: 480\n", "camera.device must be >= 0"),
        ("      device: 0\n      width: 0\n      height: 480\n", "camera geometry must be positive"),
        ("      device: 0\n      width: 640\n      height: -480\n", "camera geometry must be positive"),
        ("      device: 0\n      width: 640\n      height: 480\n      warmup_frames: -1\n", "warmup_frames must be >= 0"),
        ("      device: 0\n      width: 640\n      height: 480\n      burst_frames: 0\n", "burst_frames must be >= 1"),
    ],
)
def test_camera_validation(tmp_path, override, message):
    body = BASE.replace("      device: 0\n      width: 640\n      height: 480\n", override)
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, body))


@pytest.mark.parametrize(
    ("current", "value", "message"),
    [
        ("      match_distance: 0.5", "0", "match_distance must be > 0"),
        ("      alert_confidence: 50", "150", "alert_confidence must be 0-100"),
    ],
)
def test_recognizer_validation(tmp_path, current, value, message):
    body = BASE.replace(current, f"{current.split(':')[0]}: {value}")
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, body))


def test_detector_conf_validation(tmp_path):
    body = BASE.replace("      conf: 0.5", "      conf: 1.5")
    with pytest.raises(ConfigError, match="detector.conf must be in"):
        load_config(write_config(tmp_path, body))


def test_link_port_validation(tmp_path):
    body = BASE.replace("      port: 8765", "      port: 99999")
    with pytest.raises(ConfigError, match="link.port must be"):
        load_config(write_config(tmp_path, body))


def test_backend_url_scheme_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIELD_BACKEND_URL", "ftp://nope")
    with pytest.raises(ConfigError, match="must be http"):
        load_config(write_config(tmp_path, BASE))


def test_ack_deadline_must_exceed_poll_interval(tmp_path):
    body = BASE.replace("      device_id: ${SHIELD_DEVICE_ID}",
                        "      device_id: ${SHIELD_DEVICE_ID}\n      poll_interval_s: 30\n      ack_deadline_s: 30")
    with pytest.raises(ConfigError, match="ack_deadline_s must exceed"):
        load_config(write_config(tmp_path, body))


def test_trailing_slash_stripped_from_url(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    assert config.backend.url == "https://shield.example", "no trailing slash"


# -- camera-only loading -------------------------------------------------


def test_camera_section_loads_without_secrets(tmp_path, monkeypatch):
    """Capture/enrol must work before the deployment secrets exist.

    Dataset capture and enrolment run in Phase 1; the backend token and link key
    only come into existence in Phase 6. Requiring them to open a camera would
    block Phase 1 behind Phase 6.
    """
    for name in ("SHIELD_LINK_KEY", "SHIELD_BACKEND_URL", "SHIELD_API_TOKEN", "SHIELD_DEVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    config = load_camera_config(CONFIG_DIR / "config.yaml")
    assert (config.width, config.height) == (1920, 1080)
    assert config.device >= 0


def test_camera_section_validates(tmp_path):
    body = BASE.replace("      device: 0\n      width: 640\n      height: 480\n", "      device: 0\n      width: 0\n      height: 480\n")
    with pytest.raises(ConfigError, match="camera geometry must be positive"):
        load_camera_config(write_config(tmp_path, body))


def test_camera_section_missing(tmp_path):
    with pytest.raises(ConfigError, match="missing the camera section"):
        load_camera_config(write_config(tmp_path, "detector: {}\n"))


def test_full_config_still_requires_secrets(tmp_path, monkeypatch):
    """load_config stays strict: the app must not run without its keys."""
    monkeypatch.delenv("SHIELD_LINK_KEY", raising=False)
    with pytest.raises(ConfigError, match="SHIELD_LINK_KEY is not set"):
        load_config(write_config(tmp_path, BASE))


# -- no hardcoded geometry in code ---------------------------------------


def test_config_module_holds_no_camera_geometry():
    """Geometry belongs in config.yaml only."""
    source = (CONFIG_DIR / "config.py").read_text(encoding="utf-8")
    for literal in ("1280", "720"):
        assert literal not in source, f"{literal} is hardcoded in config.py; use config.yaml"