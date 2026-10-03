# S.H.I.E.L.D — Plan

Intruder detection: ESP32-S3 sensor node (PIR + LD2410 mmWave + buzzer) → RPi 4B
(YOLOv8n face detect + face_recognition identity) → FastAPI backend → FCM push to
an Android app. Cellular uplink with Wi-Fi primary.

The sensor link is **Wi-Fi**, not UART: the RPi hosts the AP `shield-net`, the
ESP32 is a TCP client. See DECISIONS D-012.

`docs/PROTOCOL.md` is the single source of truth for all wire formats.

## Phase gates

| # | Phase | Gate | Status |
|---|---|---|---|
| 0 | Scaffold, `PLAN.md`, `docs/PROTOCOL.md` | Protocol doc reviewed | **passed** (v2.0.0, Wi-Fi link) |
| 1 | YOLO face detector on laptop + webcam, enroll, recognize | Me = named, stranger = unknown | in progress — **provisional pass on built-in camera**; gate not yet met (dataset + training + enrolment outstanding) |
| 2 | RPi modules: `camera`, `detector`, `recognizer`, `enroll` | Same result on RPi 4B, under 1 s | pending — **must measure 1920×1080 latency first, see below** |
| 3 | Sensor link `shield_rpi/link.py` with host-side socket tests | Tests pass, bad HMAC rejected | pending |
| 4 | ESP32: PIR, radar parser, buzzer, trigger logic | Serial log shows correct events | pending |
| 5 | ESP32 + RPi integration over real Wi-Fi | PIR → RPi reply → buzzer, end to end | pending |
| 6 | Backend (FastAPI) + `alerts.py` from RPi | Test alert + snapshot stored | pending |
| 7 | Android app: FCM, alert screen, verdict buttons | Phone gets alert on mobile data | pending |
| 8 | Cellular modem, failover, SMS fallback | Alert arrives with Wi-Fi unplugged | pending |
| 9 | Hardening: systemd, log rotation, watchdogs, hostapd autostart, README | Cold boot runs unattended | pending |

A phase is not started until the previous gate is **recorded as passed** in this
table. Hardware-dependent gates must say exactly what was verified on hardware
and what was not.

## Phase 0 checklist

- [x] Repo scaffold: `firmware/esp32`, `rpi`, `backend`, `app`, `docs`, `tools`
- [x] `PLAN.md`, `DECISIONS.md`, `HANDOFF.md`, `README.md`
- [x] `.gitignore`, `.env.example`, root `pyproject.toml` (ruff + pytest config)
- [x] Phase 1 laptop harness: `tools/capture_dataset.py`, `enroll_laptop.py`,
      `live_test.py`, `face_store.py` (DECISIONS D-010, D-011)
- [x] `docs/PROTOCOL.md` **v2.0.0**: sensor link (Wi-Fi/TCP/JSON/HMAC), backend
      API, LD2410 frame, config keys, test strategy
- [x] **Protocol doc reviewed — gate passed.** Approved with the UART → Wi-Fi
      change: raw TCP rather than MQTT, per-frame HMAC, event-scoped polling.
- [ ] git repo initialised

## Phase 1 checklist

- [x] `rpi/.venv` on Python 3.11, CV stack installed and imports verified (D-014, D-015)
- [x] `rpi/requirements.txt` pinned, including the Windows install order
- [x] `shield_rpi/config.py` + `config.yaml`: all geometry/keys moved out of code,
      secrets interpolated from env, `${VAR}` missing → refuse to start
- [x] `shield_rpi/camera.py`: MJPG request, read back actual size, warn-and-continue
      on mismatch, discard 10 warm-up frames, burst grab, never crash on I/O error
- [x] `rpi/tests/test_camera.py` + `test_config.py`: 51 tests, all passing on host
- [x] `tools/camera_check.py`: enumerate devices, probe 1280x720, report usable index
- [x] **Resolution verified on hardware: 1280x720 @ ~30 fps works on BOTH cameras**
      — the laptop built-in (device 0) and the Zebronics webcam (device 1), each over
      MJPG and YUYV. The earlier 640x480 reading was the driver's power-on default,
      not a ceiling. `config.yaml` ships at the PROTOCOL target of 1280x720.
- [x] **Re-verified on Zebronics via `tools/camera_check.py`.** Confirmed 1280x720
      at 30.0 fps over MJPG; a 30-frame burst sustained 30.5 fps. It enumerates as
      **device 1**, and both cameras are live simultaneously.
- [x] `config.yaml` `camera.device: 1` — captures default to the Zebronics. Device 0
      returned near-black frames at first check (luma ~7) but later read ~110, so it
      may simply have been uncovered or in use; both are usable, and the index shifts
      with plug order (D-025).
- [x] `Camera.read` normalises packed YUYV to BGR (D-024) — both cameras hand back a
      2-channel YUY2 array that crashed `imencode`/`cvtColor` with an opaque assertion.
- [x] **Camera replaced: Logitech Brio 100, capture now 1920×1080 (D-028).** Supersedes the
      three entries above, which stay as the record of what the built-in and the Zebronics
      actually did. Device index re-picked on a luma ≥ 20 gate, not enumeration order:
      device 0 rejected (luma 3–7, near-black), **device 1 selected** (luma 154–198).
      Verified through `Camera`: true 1920×1080, `matches_request=True`, 5-frame burst in
      0.17 s (~29 fps). **Close the "Brio 100 1080p Camera Setup" app before probing** — while
      it was open the sensor intermittently returned no frames and the capture rate read a
      bogus 5 fps. Known host-only wart: `Camera.open()` stalls ~191 s on Windows in
      DirectShow negotiation, so the tools look hung for ~3 minutes at startup; not a crash,
      and not reproduced on Linux. **No retrain** — 720p and 1080p are both 16:9 and
      letterbox identically at `imgsz=640`, and the existing 220-frame 720p dataset is
      unaffected.
- [ ] Capture ~200 labelled frames across conditions. **Box quality matters more than
      count**: boxes must cover the whole face (forehead-to-chin, ear-to-ear), not just
      the eyes. Review with `python tools/preview_labels.py --split train --count 20`
      before training; fix or delete anything it flags.
- [ ] **Add other people's faces to the training set** (see below). Required.
- [ ] Train: `yolo detect train model=yolov8n.pt data=rpi/datasets/faces/data.yaml epochs=50 imgsz=640 batch=16`
- [ ] Copy best weights to `rpi/models/face.pt`
- [ ] Enrol self (7 photos), enrol at least one other person
- [ ] `python tools/holdout_test.py` — gate (all four must hold):
      me detected >=95%, me named with >=90% accuracy on never-trained-on frames,
      **strangers detected >=95%**, and zero strangers identified as enrolled

### Phase 1 · why general faces are required

Training only on your own captures gives a detector that finds you and misses everyone
else. On an intrusion alarm that is the dangerous direction to fail: a stranger walks in,
the detector finds nothing, no event fires, and the system stays silent. Recognition
cannot rescue it, because recognition only runs on faces the detector already found.

`tools/add_general_faces.py` converts WIDER FACE (recommended: 32k images, 393k faces,
61 event categories, heavy scale/pose/occlusion variation) or an existing YOLO-format face
set into `images/train` alongside your captures, tagged `condition=general` in
`metadata.jsonl` so they can be counted or excluded later.

```powershell
# get WIDER FACE train images + annotations, then:
python tools\add_general_faces.py --source wider `
  --wider-annotations <path>\wider_face_split_bbx_train.txt `
  --wider-images <path>\WIDER_train\images --max-images 3000 --min-face 40
```

Two caveats worth knowing: WIDER boxes are tight, while your captures are padded 25%, so
the two box styles differ — check a few general frames with `preview_labels.py` to be sure
they do not fight each other. And WIDER FACE is non-commercial research use (some
redistributions CC BY-NC-ND), which is fine for a personal system and not fine if this is
ever shipped or sold.

### Phase 2 · the 1080p latency question (must be measured, not assumed)

Phase 1 moved capture from 1280×720 to **1920×1080** on the Logitech Brio 100 (D-028). That
geometry was verified on the **laptop only**. The 1 s trigger-to-reply gate (PROTOCOL §4.3) has
**never been measured at 1080p on the Pi**, and 1280×720 was the geometry it was written for.

The laptop numbers are encouraging but not transferable. Through `Camera`, the Brio sustains
~29 fps and a 5-frame burst costs 0.17 s — so capture is not the bottleneck. What is unknown is
whether the Pi can get MJPG at 1080p at all: on the Windows host DirectShow ignores the
fourcc request and hands back uncompressed YUY2 (~4.1 MB/frame), and the Pi's Linux V4L2 path
may or may not do better. YUY2 at 1080p over USB 2 will not hold 30 fps.

**Phase 2 checklist item, before the gate can be called:**

- [ ] Measure trigger-to-reply at 1920×1080 on the Pi 4B at `imgsz=640`. Record
      `latency_ms`, the granted fourcc, and the frame rate actually achieved in
      `metadata.jsonl`-style provenance. Do not infer the Pi's behaviour from the laptop.

**If it exceeds 1 s, the fallback is a split geometry — do not drop back to 720p wholesale:**

- 720p for the continuous / burst path, where frame count and latency matter.
- A single **1920×1080 still, grabbed on trigger only**, for the snapshot that gets uploaded
  and stored with the event. One frame at 1080p is ~4 MB over the bus once, which the budget
  can absorb; five per trigger cannot.

The reason to prefer this over plain 720p: the stored snapshot is what a human looks at later
to decide whether an alert was real, so it should be the best the sensor can give. The reason
not to assume it: PROTOCOL §3 caps snapshots at 512 KB, so a 1080p still must be re-encoded
before upload, and that encode cost belongs in the measurement.

Whichever way it lands, update PROTOCOL §4.1/§4.3 and §7 with the measured number and the
geometry actually shipped. Do not leave §4.3 describing a configuration nobody measured.

## Open questions

Non-blocking, to resolve in later phases:

- **RPi reboot trips the ESP32 alarm.** The RPi hosts the AP, so rebooting it drops
  the link and the fail-safe rule fires. Options for Phase 9: a maintenance flag
  readable from the backend, a link-down grace window of ~15 s, or accepting that
  reboots are audible. Needs a decision before Phase 5 can be called done.
- **Static IP for the ESP32** — set in firmware, or a DHCP reservation in
  hostapd? Firmware-static is simpler and survives losing the AP config.
- ESP32 GPIO for the LD2410 OUT pin is IO5 per the original spec; confirm on the
  board. Note that `shield_esp32_prompt.md` still describes the UART link and
  IO43/44: it is superseded by `docs/PROTOCOL.md` v2.0.0 and is kept unmodified as
  the original input artifact.
- Face dataset source for the YOLO detector (Roboflow "face detection" vs WIDER
  FACE conversion) — affects Phase 1 training time.
- Telegram fallback appears in the RPi prompt but not in the app spec. Not in the
  protocol; dropping unless wanted.