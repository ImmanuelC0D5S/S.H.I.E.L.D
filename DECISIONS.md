# S.H.I.E.L.D — Decisions

Format: date · decision · reason. Architecture never changes silently.

## D-001 · 2026-10-01 · Protocol-first development

`docs/PROTOCOL.md` is written and reviewed before any firmware or Python that
touches the wire.

**Reason:** the ESP32 (C) and RPi (Python) are written against a fixed packet
layout and an HTTP contract. If either side invents a field, the mismatch is
silent — a bad packet is indistinguishable from a bug. One document, written
first, is the only cheap defence.

## D-002 · 2026-10-01 · Trigger tier is carried on the wire

*Revised 2026-10-01 by D-012.* The sensor link carries the ESP32's trigger tier
(`high`/`low`) explicitly, in the `trigger` message's `tier` field.

**Reason:** v1.0.0 dropped it because the `TRIGGER` packet was fixed at 7 bytes
with no room for a field. D-012 replaced that binary frame with a length-prefixed
JSON message, so the constraint no longer exists and the information is worth
carrying: the backend records it in the event, and it is how sensor thresholds get
tuned later. The 4-byte `reply` widening stays unnecessary — `unknown` vs
`low_conf` remains distinguishable at the backend without telling the ESP32.

## D-003 · 2026-10-01 · No COBS or escaping on the UART link

**SUPERSEDED by D-012.** Retained for history: this decision applied to the UART
link, which no longer exists. The framing decision is now D-012.

## D-004 · 2026-10-01 · Reply over UART is sent before the network alert

The ESP32 `REPLY` goes out immediately after the verdict is computed. The
backend POST happens afterwards, on its own timeout.

**Reason:** fail-safe is the whole point of this system. The local alarm must not
inherit the latency or the failure mode of the internet. A failed POST is queued
to `events.jsonl` and retried on the next uplink; the buzzer has already been
armed.

## D-005 · 2026-10-01 · RPi polls the backend instead of receiving webhooks

`GET /api/events/{id}/status` every 5 s, up to the 30 s ack deadline.

**Reason:** the RPi sits behind carrier-grade NAT on cellular. It cannot receive
an inbound webhook, so the backend cannot push a verdict to it. Polling is the
only option that works on the failure path, which is exactly the path the SMS
fallback exists to serve.

**Amended 2026-10-01:** polling is event-scoped. The RPi polls only while an
event is unresolved and stops on `resolved`, on a verdict, or after 10 minutes.
There is no background poll loop. On a metered cellular uplink an idle node must
cost zero data, and a permanent 5 s poll loop would be the single largest
recurring data cost in the system.

## D-006 · 2026-10-01 · Snapshots authenticate by HMAC signature, not bearer token

`GET /api/snapshots/{event_id}?exp=…&sig=…`, signed with `SNAPSHOT_SIGNING_KEY`,
1 h expiry.

**Reason:** image loaders on both the Android app (`Coil`) and any preview path
cannot attach a bearer header reliably, and signed URLs can be cached, expire, and
be revoked independently. Keeps the 1 h TTL from being silently extended by a
long-lived API token.

## D-007 · 2026-10-01 · Client-generated `event_id` on the alert

The RPi mints the UUID and reuses it across retries; the backend treats a repeat
`event_id` as success rather than an error.

**Reason:** the alert POST crosses a flaky cellular link and will time out
sometimes. Without idempotency, a retry that actually succeeded server-side would
create a duplicate event and a duplicate push to the phone.

## D-008 · 2026-10-01 · `POST /api/alert` is multipart/form-data

`event` (JSON part) + optional `snapshot` (JPEG part).

**Reason:** a 720p JPEG is 100–250 KB; base64 inflates it by a third and forces
the whole body into memory on both ends. A separate part also lets the backend
stream the image to disk without parsing it out of a JSON string.

## D-009 · 2026-10-01 · Backend and RPi configs are separate trees

`backend/` and `rpi/` each get their own `pyproject.toml`; the root
`pyproject.toml` only holds shared ruff/pytest settings.

**Reason:** they have disjoint dependency sets (FastAPI + SQLAlchemy vs
ultralytics + face_recognition + pyserial) and disjoint deployment targets. A
single pinned dependency set would force the tiny backend to pin the computer
vision stack it never imports.

## D-010 · 2026-10-01 · Phase 1 lives in `tools/`, Phase 2 promotes it

The laptop harness is `tools/capture_dataset.py`, `tools/enroll_laptop.py`,
`tools/live_test.py`, and `tools/face_store.py` (the `.npy` store prototype).

**Reason:** Phase 1's gate is a *result* ("me = named, stranger = unknown") on a
laptop, not a module. Writing `shield_rpi/` first would build Phase 2 ahead of
its gate and bake unvalidated thresholds and camera handling into the shipped
package. The `tools/` scripts are disposable: Phase 2 re-implements the validated
logic as `shield_rpi/camera.py`, `detector.py`, `recognizer.py`, `enroll.py`, and
the `tools/` scripts are repointed or deleted then. The temporary duplication of
`.npy` load/save is the price, and it is small.

## D-011 · 2026-10-01 · Auto-annotate the face dataset with dlib's HOG detector

`tools/capture_dataset.py` labels each captured frame with
`dlib.get_frontal_face_detector()`.

**Reason:** a face detector trained on the camera's own footage needs ~200 boxes.
Hand-labelling 200 boxes costs an hour of clicking for zero engineering value.

*Revised 2026-10-01.* Originally OpenCV Haar cascades, on the assumption that
`cv2.CascadeClassifier` was stable API. **OpenCV 5.0 removed it entirely** — the
symbol is gone from the `cv2` namespace, confirmed against `opencv-python
5.0.0.93`. Haar was never going to survive the 5.x cleanup intact.

dlib is the better choice anyway: it is already installed (it backs
`face_recognition`), so there is no new dependency, and it is the *same* detector
enrolment uses to compute embeddings — so the dataset and the enrolled face
representations agree on what a face is. Being frontal-only is acceptable for a
self-captured dataset: a turned-away head is left unannotated and re-shot rather
than saved with a guessed box. If annotation quality proves too low and training
accuracy plateaus, revisit with MTCNN via `facenet-pytorch` rather than going back
to a removed OpenCV API.

## D-014 · 2026-10-01 · Windows uses `dlib-bin`; `setuptools<81` is mandatory

`dlib` has no Python 3.11 wheel on Windows and fails to compile without a full MSVC
toolchain. The laptop venv installs `dlib-bin==20.0.1` (prebuilt wheels) and then
`face-recognition --no-deps`. Separately, **setuptools must be pinned `<81`**:
`face_recognition_models` imports `pkg_resources`, which setuptools 81+ removed.
Without the pin, `face_recognition` fails with a misleading "please install
face_recognition_models" error even though the package is installed.

**Reason:** this cost roughly an hour of a failed multi-GB install to discover, and
the failure modes are both non-obvious — one is a compile error that looks like a
missing compiler, the other is an import error naming a package that *is*
installed. The install order and the pin are documented in
`rpi/requirements.txt` so the next machine does not repeat it. The RPi (Linux)
builds `dlib` from source with `build-essential cmake python3-dev` and does not
need `dlib-bin`.

## D-015 · 2026-10-01 · Keep OpenCV 5.x, do not downgrade

`opencv-python==5.0.0.93` is used as-is even though it removed APIs the original
spec assumed.

**Reason:** the only casualty found so far was Haar (D-011), already replaced.
Pinning back to OpenCV 4.x to preserve a removed classifier would hold the project
back from every other 5.x improvement — including the DNN face detector
(`cv2.FaceDetectorYN`), which is a better long-term annotator than Haar ever was,
and is worth revisiting if D-011's dlib annotator proves weak. YOLO inference runs
on torch, not OpenCV, so the upgrade carries little inference risk.

## D-012 · 2026-10-01 · Sensor link is Wi-Fi + TCP/JSON + HMAC, not UART

The ESP32 ↔ RPi link is raw TCP over a Wi-Fi link the RPi hosts itself
(`shield-net`, hostapd, RPi at `192.168.50.1`, ESP32 at `192.168.50.10`, port
8765). Frames are `len(4) || flags(1) || JSON body || HMAC-SHA256 tag(16)`.
Messages: `trigger`, `reply`, `heartbeat`, `heartbeat_ack`. 2 s heartbeat both
ways, 5 s reply timeout, local alarm on link loss. UART2 (IO43/IO44) is freed;
LD2410 on UART1 is untouched.

**Reason (link type).** Wiring a 4-wire UART between two boards is one more cable
to run, one more connector to fail, and — the real problem — it ties the sensor
node to a fixed position. Wi-Fi with the RPi as AP means the sensor can be moved
and re-paired without touching anything, and the same TCP socket carries
heartbeats, so liveness is observable for free. A cable is also a physical
security liability outdoors. The uplink stays cellular/Ethernet: `shield-net` has
no internet path, so the sensor link is isolated from the backend link by
construction.

**Reason (raw TCP, not MQTT).** Rejected MQTT. Traffic is a heartbeat every 2 s
plus one request/response per intrusion: a handful of messages per second at peak,
zero at rest. MQTT would add a broker process to supervise on the RPi, a broker
config, and a client library, to obtain topic routing and pub/sub fan-out for a
topology that is strictly point-to-point. Broker liveness, retained messages and
reconnect semantics become new failure modes for zero functionality we need. Topic
routing is one `type` field. `shield_rpi/link.py` is a `socket.accept()` and a
length check.

**Reason (JSON, not binary).** A binary frame would save a few hundred bytes per
message on a link with megabits of headroom. JSON is debuggable on both sides —
`curl`/`nc` against the RPi, `LOG_HEXDUMP` on the ESP32 — and adding a field
later needs no coordinated version bump. The cost is irrelevant at this rate;
HMAC covers integrity either way.

**Reason (HMAC, not XOR).** XOR detects corruption but is trivially forgeable:
anyone who reaches `shield-net` can recompute it and inject a `reply` with
`status: 0` (silencing a real alarm) or withhold one. WPA3-PSK authenticates the
link, but HMAC keeps it trustworthy if the PSK leaks and stops a compromised host
on the AP from forging sensor traffic. Tag verification happens before JSON parse,
so unauthenticated bytes never reach the decoder. The comparison is
constant-time on both sides.

**Consequence accepted:** the RPi hosts the AP, so an RPi reboot drops the link and
trips the ESP32's fail-safe alarm. An RPi reboot is audible. Tracked in `PLAN.md`;
a maintenance flag or link-down grace window is Phase 9 work.

## D-013 · 2026-10-01 · Link tests are host-side socket tests, not loopback

Removing UART removes the wire-level loopback test. The sensor link is instead
covered by `rpi/tests/test_link.py` running a real server and client on
`127.0.0.1`.

**Reason:** the link is now ordinary TCP, so its logic is testable without any
hardware at all — and *more* testable than a wire loopback was, because the tests
can inject the failure cases a 4-wire cable cannot produce: a body bit flip under
a valid length, a truncated frame, a dropped connection mid-frame, an oversized
`len`, three tag failures in a row. Those are exactly the fail-safe paths that
matter and were previously untestable. The trade is that Wi-Fi-layer faults
(packet loss, RSSI, AP association) are now outside the test suite; those get
covered by instrumenting the link and by hardware testing in Phase 5.
## D-016 · 2026-10-01 · Camera geometry lives only in `config.yaml`; size mismatch warns and continues

`camera.device`, `width`, `height`, `fourcc` and `warmup_frames` are read from
`config.yaml` via `shield_rpi/config.py`. No Python file contains a resolution
literal, and tests assert this. `Camera` requests MJPG then the target size, reads
back what the device actually granted, and **logs a warning and continues** on
mismatch rather than failing.

**Reason:** the same code must run against whatever camera is attached, now and
after the Zebronics arrives. A camera that refuses the requested size is still a
usable camera — a detector trained at one resolution runs at another, usually at a
small accuracy cost. Failing to open would mean no intruder detection at all, which
is a far worse outcome than a slightly degraded match. The *actual* size is what
gets reported to the backend, so the event record never misstates what was
analysed. Hardcoding geometry would have meant a code change per camera swap and
would have silently baked in the built-in laptop webcam. `tools/camera_check.py`
exists so geometry is established empirically rather than by assumption.

## D-017 · 2026-10-01 · Discard 10 warm-up frames after opening the camera

`Camera.open()` reads and throws away `warmup_frames` frames (default 10) before
returning the camera to a caller.

**Reason:** a just-opened camera has not settled its auto-exposure and
auto-white-balance; early frames are typically dark, blown out or colour-shifted.
Training a detector on those, or computing an enrolment embedding from them, yields
a worse representation than the steady state — and the steady state is what every
later detection is compared against. Ten frames is about a third of a second at
30 fps, enough for the AE and AWB loops to converge on this hardware. Tunable in
`config.yaml`.

## D-018 · 2026-10-01 · `load_camera_config` is separate from `load_config`

`load_config` validates the whole file including `${SHIELD_LINK_KEY}` and
`${SHIELD_API_TOKEN}`, so the running app refuses to start without its secrets.
`load_camera_config` reads only the camera section and needs none of them.

**Reason:** dataset capture and enrolment are Phase 1 work; the deployment secrets
do not exist until Phase 6 has a live backend. Requiring an HMAC key and a backend
token merely to open a camera would gate Phase 1 behind Phase 6. The split keeps
fail-closed behaviour where it matters — the app itself — without making the
capture tools unrunnable. Tests cover both halves.

## D-019 · 2026-10-01 · 1280x720 is the shipped default; the 640x480 reading was a probe artefact

An early probe reported the **laptop built-in** webcam as 640x480, and 640x480 was
briefly recorded as a hardware ceiling. It is not. **Verified on hardware: the
laptop built-in camera delivers 1280x720 at ~30 fps** over both MJPG (~29.7 fps) and
YUYV (~30.3 fps). 640x480 is only what the driver reports *before any geometry is
requested* — the mistake was reading back the default and calling it a maximum.

**Scope limit.** This result covers the laptop built-in camera **only**. The
Zebronics 720p webcam has **not been tested in any way** — not opened, not probed,
not captured from. Its name suggests 720p but nothing about its actual behaviour is
verified: its MJPG support, achievable frame rate, and whether it delivers 1280x720
after an explicit request are all unknown.

**Reason:** `config.yaml` ships at 1280x720, the PROTOCOL deployment target, with
no change to `docs/PROTOCOL.md`. Getting this wrong twice was instructive enough to
log: a read-back is only meaningful after a request has been made, which is exactly
the order `Camera.open()` negotiates in. `tools/camera_check.py` performs the
request-then-read-back sequence.

**Outstanding (Phase 1).** Re-verify on the Zebronics via `tools/camera_check.py`
when plugged in. Until that runs, every 720p claim in this project rests on one
camera. If the Zebronics disagrees, `config.yaml` follows the hardware — not the
other way round — and `PLAN.md` records the change.

## D-020 · 2026-10-01 · Auto-label boxes pad to the whole face, not the inner face

`capture_dataset.py` boxes come from `face_recognition.face_locations` — which runs
**HOG**, not CNN, since the call does not pass `model="cnn"` — then padded by 25% of the
box size and clipped to the frame. The previous dlib HOG detector produced boxes covering
only brow-to-nose. *Correction:* an earlier revision of this entry claimed the boxes came
from the CNN model; they do not. The padding, not the detector, is what restores whole-face
coverage, so the reasoning below still holds.

**Reason:** face detectors consistently return the *inner* face, because that is the
region with the contrast pattern they key on. Training a YOLO detector on such boxes
teaches it to crop faces the same way, and the recognition embedding is then computed
from a crop missing the forehead, chin and jawline - precisely the features that
separate one person from another. Padding restores whole-face coverage. Clipping to the
frame matters because a normalised YOLO label derived from an off-frame box goes out of
the 0..1 range and silently corrupts training.

Note the padding is applied to dataset boxes only. The enrolment and inference path uses
`face_recognition`'s own landmarks, which already cover the full face; padding those too
would change matching behaviour and is not implied here.

## D-021 · 2026-10-01 · Capture UI states the reason for every skipped frame

The capture window shows the skip reason ("no face detected - move into frame" vs
"face too small (Npx < Mpx) - move closer"), the saved-frame counter, and the key
controls. The counter advances only on a real write to disk.

**Reason:** a counter that sits at `0/40` with no explanation reads as a broken tool,
and the operator has no way to tell whether the camera, the detector or their own
position is at fault. Telling them to move closer turns a silent stall into one keypress
of progress. Counting frames *displayed* rather than frames *saved* would have hidden
the stall entirely, since the loop shows thousands of frames while saving none.

## D-022 · 2026-10-01 · The training set must include other people's faces

The detector must be trained on general faces (WIDER FACE or equivalent), not only on
the owner's captures. Phase 1's gate enforces **stranger detection >= 95%** in addition
to identification accuracy.

**Reason:** a detector trained solely on one person learns that person's appearance and
generalises poorly to anyone else. For an intrusion alarm this is the worst possible
direction to fail in: an undetected intruder produces *no event at all*, so the system
stays silent while someone is in the room, whereas a false positive at worst pages
somebody. Recognition cannot compensate, because it only runs on faces the detector
already found.

This is why identification accuracy alone is an inadequate gate. A model that called
everything "me" would score 100% on it. Stranger detection rate and the false-accept
count are the two numbers that actually constrain the failure mode that matters.

## D-023 · 2026-10-01 · Labels are reviewed by montage before training, not by inference

`tools/preview_labels.py` renders a randomised grid of captured frames with their YOLO
boxes drawn, cropped with context, captioned, and auto-flagged on aspect-ratio, size and
off-frame checks. Exit code is non-zero when anything is flagged.

**Reason:** a wrong auto-label is worse than a missing one - it teaches the wrong thing
quietly, and the symptom appears much later as poor real-world detection, by which point
the dataset is large and the cause is hard to trace. Reviewing frames one at a time does
not happen reliably; a single montage screen does. The audit exists so the check is
scriptable in CI or a pre-training hook rather than relying on discipline.

## D-024 · 2026-10-01 · `Camera.read` normalises packed YUYV frames to BGR

`read()` returns a 3-channel BGR frame regardless of the fourcc the device grants.
A camera that ignores the MJPG request returns packed YUY2 as a **2-channel** array
(Y,U,Y,V per pixel pair, unconverted), which crashed `imencode` and `cvtColor` with an
opaque `channels == 1 || channels == 3 || channels == 4` assertion.

**Reason:** fourcc is a config value and a camera may grant YUY2 regardless, so a
caller cannot assume BGR. Normalising once in `read()` means the detector,
`encode_jpeg`, `cvtColor` and the capture tools all work unchanged. The alternative -
converting at each call site - would mean every future consumer has to know about the
fourcc, and the next one added would forget. Verified on hardware: with the Zebronics
attached, both MJPG and YUYV now yield (720,1280,3) and encode cleanly.

## D-025 · 2026-10-01 · Two cameras coexist; `device` stays config, not a guess

With the Zebronics connected the laptop enumerates **two** cameras, both delivering
1280x720 at ~30 fps over MJPG. Device 0 is the built-in (near-black frames, mean luma
~7 - effectively dark/covered), device 1 is the external webcam (mean luma ~131, a lit
scene). Neither is a duplicate: mean absolute pixel difference between the two streams is
126/255.

**Reason:** the index is a property of the host's enumeration order and changes when a
camera is plugged or unplugged, so it is read from `config.yaml` and re-probed with
`tools/camera_check.py` rather than hardcoded per machine (extends D-016). Discovering
the external camera at device 1 is exactly the pending "re-verify on Zebronics" item in
`PLAN.md`: 1280x720 is confirmed on it. Which device should be the default for Phase 1
captures was left as a question for the owner rather than assumed, because the answer
depends on which camera the captures should come from.

## D-026 · 2026-10-02 · Use a pretrained face detector instead of training from scratch

**Superseded approach:** fine-tune YOLO from scratch on ~200 captured frames plus ~3,000
WIDER FACE images, via `tools/add_general_faces.py`.

**New approach, in order:**

1. Download `arnabdhar/YOLOv8-Face-Detection` `model.pt` via `huggingface_hub` to
   `rpi/models/face_pretrained.pt`. Verify it loads under `ultralytics`, reports
   `nc == 1` and `names == {0: "face"}`. Log its SHA-256.
2. Run it **unmodified** through `tools/holdout_test.py` and the stranger set. Report
   detection rate and identification rate as the baseline.
3. Use it to auto-label the 220 captured frames (`tools/bootstrap_label.py`), then review
   with `tools/preview_labels.py`.
4. Fine-tune **only if the gate fails**: `lr0=0.001`, `epochs=10..20`, `freeze=10`, on the
   captures plus ~500 general face images. Re-run the gate including stranger detection
   >= 95%.
5. Pad every detection box by 25% before cropping for recognition.

`tools/add_general_faces.py` becomes **optional** - retained because step 4 may need the
general images, not because it is on the critical path.

**Reason:** 220 self-captured frames cannot teach a face detector to generalise. The
classical recipe needs tens of thousands of labelled faces from many people, varied
ethnicity, age and pose, plus hard negatives; WIDER FACE supplies that scale but its
labels arrived as images only (no `wider_face_train_bbx_gt.txt` in this repo), and its
licence is non-commercial research, which blocks any shipped use. A detector already
trained on a large public face corpus gives a strong, licence-clean starting point, and
turns the question from "can 220 frames teach detection?" into "does this specific
detector clear the gate on this specific webcam?". That is the question the project
actually needs answered, and step 2 answers it before any training happens.

The ordering matters: measuring the pretrained model first makes fine-tuning an
evidence-driven decision rather than a ritual. If it already passes, we never train, and
we avoid spending the effort on a dataset we do not need. If it fails, step 4 is
justified by a measured shortfall with a known target.

**Padding at inference (step 5) applies to recognition crops, not detector training
labels.** The enrolment and inference paths crop the padded box so the embedding comes
from a forehead-to-chin, ear-to-ear region rather than the inner face; an inner-face crop
loses jawline and hairline, which is what separates one person from another (D-020, D-024).
Padding the *training* labels instead would change what the detector is asked to regress
away from the pretrained weights' own definition of a face, so the two are kept distinct.

## D-027 · 2026-10-02 · Pretrained detector verified; identification measurement blocked

**Detector:** `rpi/models/face_pretrained.pt`, supplied by the owner (placed in the repo
during this session, not downloaded by the tooling — `huggingface_hub` was installed
speculatively and turned out to be unnecessary).

```
SHA256  37396AC6A9601AB9F5177E4231B09D81CF6F65A7F22DB99EC3B36AB63F674E71
size    6,247,065 bytes
ultralytics  task=detect  nc=1  names={0: 'FACE'}  params=3,011,043  imgsz=640
```

**Deviation from D-026 step 1 as written:** the class name is `FACE` (upper case), not
`face`. Cosmetic — the model is a valid single-class face detector — but recorded rather
than quietly normalised, because `bootstrap_label.py` asserts `nc == 1` and prints
`names` so a wrong-model regression is visible instead of silent.

**Step 2, detection rate — measured, unmodified, on this camera:**

| set | frames | detected | rate | conf p5 / p50 | latency |
|---|---|---|---|---|---|
| train | 200 | 200 | **100.0%** | 0.88 / 0.92 | 125 ms/frame |
| holdout | 20 | 20 | **100.0%** | 0.90 / 0.92 | 66 ms/frame |

Box geometry, pretrained vs the padded-HOG labels it replaced: area ratio p50 **0.44**,
IoU p50 **0.44** — the pretrained boxes are substantially tighter, which is expected
since the old labels carried 25% padding on top of an inner-face HOG box.

**Step 2, identification rate and stranger detection — NOT MEASURED.** Blocked, not
skipped: `rpi/known_faces/` holds 0 embeddings so there is no owner to match against,
and no stranger image set exists. Detection rate alone is not the gate. Reported as
unmeasured rather than estimated.

**Coverage check (step 5 premise).** The pretrained box already contains **97–98%** of the
68-point landmark extent (x p50 97%, y p50 98%), versus the padded-HOG box at 100%. So the
pretrained detector already returns essentially whole-face boxes; the 25% recognition
padding is added margin, not a repair of a tight box. It is still applied, because the
recognition crop is not the detection box and identity discrimination is the thing being
protected.

**Applied.** `bootstrap_label.py` created; train relabelled 200/200 by the pretrained
detector with 0 unlabelled. Holdout labels deliberately **left on the old padded-HOG
boxes** so the two labelling conventions can be compared frame-by-frame rather than one
overwriting the other. Recognition padding is centralised in `recogniser.crop_rgb`, which
both `holdout_test.py` and `live_test.py` already call, so the gate and the live feed
cannot drift. `enroll_laptop.py` was changed to detect-then-crop with that same padding:
it previously called `face_encodings()` on the whole frame, running its own internal HOG
pass, so enrolled and live crops came from different regions.

**Step 4 not started** — it is conditional on the gate failing, and the gate has not run.

## D-028 — 2026-10-03 — Logitech Brio 100 adopted; capture geometry moved to 1920×1080

**Hardware.** Logitech Brio 100, `USB\VID_046D&PID_094C`, replacing the laptop built-in as
the capture sensor. Windows reports it as "Logitech Brio 100 1080p Camera Setup".

**Device index: 1, chosen on measurement not order.** Enumeration order is not a selection
criterion (D-025). A luma ≥ 20 gate was applied, matching the spirit of `camera_check.py`'s
`_MIN_LUMA` (25.0, stricter):

| index | luma @1080p | verdict |
|---|---|---|
| 0 | 3–7 | rejected — near-black at every geometry |
| 1 | 154–198 | **selected** |

Reproduced across two clean runs. Note the setup app (`Brio 100 1080p Camera Setup`) must be
**closed** before probing — while it was open, device 1 intermittently failed with "no frames
after open". A camera held by another app is not an absent camera.

**Geometry: 1280×720 → 1920×1080, config only.** `config.yaml` `device: 1`, `width: 1920`,
`height: 1080`. No Python file changed: `camera.py` and `config.py` contain zero resolution
literals, and `live_test.py` / `holdout_test.py` / `enroll_laptop.py` reach geometry only via
`Camera(load_camera_config(...))` — D-016 working as designed. The `--imgsz 640` detector
defaults stay at 640 and are correct there: both geometries are 16:9, so ultralytics
letterboxes both to the same 640×360 content area and a face occupies an identical fraction
of the detector input. **No retrain is required**, and none was done.

**Why enrolment moved too, rather than just the photos.** The temptation was to keep capture
at 720p and take only the enrolment photos at 1080p, on the grounds that more pixels makes
better embeddings — which is true. It was rejected because D-026 step 5 already fixed this
exact bug once: `enroll_laptop.py` used to embed the whole frame while the gate cropped, so
"enrolled and live crops came from different regions" and the gate stopped describing live
behaviour. `recogniser.crop_rgb` pads 25% *proportionally* (`recogniser.py:78`), so the 1080p
crop is geometrically equivalent to the 720p one at 2.25× the pixels — but a different
geometry on either side of the gate is the same class of drift, one level up. Geometry lives
in one file and moves as one unit.

**MJPG deliberately kept, despite the host readback contradicting it.** The Windows
DirectShow backend reports `YUY2` whatever fourcc is requested, and `set(CAP_PROP_FOURCC)`
returns in 0.00 s having done nothing — the request is ignored, not merely mis-reported.
That is evidence about DirectShow, not about the Linux/RPi V4L2 path, so `fourcc: MJPG` is
unchanged per D-024. **Do not "correct" this to YUYV from a host probe.** On the Pi, MJPG
matters more at 1080p than it did at 720p: uncompressed YUY2 costs ~4.1 MB/frame.

**Host throughput: a correction.** An early probe suggested device 1 sustained only 5.0 fps
with a ~215 ms worst-case read, which would have made a 5-frame burst cost ~1.0 s and blown
the whole latency budget. **That was contention with the still-open setup app, not the
camera.** Timed through the project's own `Camera` class on a quiet device:

```
open()            191 s   (see below)
burst() x2        0.17 s / 0.16 s for 5 frames each   -> ~29 fps
steady read       15-63 ms, mean 33.5 ms
```

So the burst is cheap and PROTOCOL §4.3's 1 s budget is not threatened by capture. The
`CAP_PROP_FPS` readback of `30.0` is plausible after all; it was the contention-window number
that was wrong. Any future probe must run in a single process with the setup app closed, and
should measure a burst rather than sampling reads in isolation.

**Known host-only defect: `Camera.open()` takes ~191 s on Windows.** Reproducible across
runs, and it is not the sensor:

```
cv2.VideoCapture(1)          64.7 s
set(CAP_PROP_FRAME_WIDTH)    64.3 s   <- forces a stream restart
set(CAP_PROP_FRAME_HEIGHT)   63.6 s   <- forces a second one
10 warm-up reads              1.1 s   (891 ms first, then 15-63 ms)
```

Each DirectShow geometry `set()` stalls ~64 s, so the width/height pair costs ~128 s of pure
negotiation. `set(CAP_PROP_FOURCC)` is free because it is ignored. **Not fixed in
`camera.py` on purpose:** reordering or coalescing the sets would be tuning for one backend's
bug, and `camera.py` is deliberately backend-agnostic (D-016/D-024). Expect enrolment and
`live_test.py` to appear hung for ~3 minutes at startup on this laptop; that is the
negotiation stall, not a crash. Whether the Linux V4L2 path shows it is unknown and untested.

**Protocol and tests updated to match.** `docs/PROTOCOL.md` §4.1 (burst geometry), §4.3 (the
1080p latency figure is explicitly marked unverified, with the fallback written down), and §7
(`camera.width`/`camera.height` defaults). `test_config.py` had two tests pinning the shipped
geometry to 1280×720 (`test_camera_geometry_matches_deployment_target`,
`test_camera_section_loads_without_secrets`); both now assert 1920×1080. 116 tests pass.

**Not changed.** The 220-frame dataset in `rpi/datasets/faces/` is still 1280×720 from the old
built-in camera, and its `metadata.jsonl` records that faithfully. Per D-027 the pretrained
detector already reads 100% of those frames, and 1080p is letterbox-equivalent at `imgsz=640`,
so no recapture is required. Re-shooting the dataset on the Brio would invalidate the
verified 220/220 detection rate and the holdout labels for no measured gain.

## D-029 — 2026-10-03 — Brio 100 frames far tighter than the built-in; enrolment gained a size gate

**Symptom.** A first enrolment attempt on the Brio 100 produced one photo and was abandoned:
"cam is too zoomed in". `rpi/known_faces/` still holds 0 embeddings, so nothing was enrolled.

**Measured, not eyeballed.** Face box as a percentage of frame height (resolution-independent),
from the pretrained detector over the actual enrolment photos:

| photo | geometry | face box | % frame height | padded crop |
|---|---|---|---|---|
| 20261002-162822-01 … -163702-01 (5×) | 1280×720 built-in | 172–259 px short edge | **30–46%** | 258×322 … 389×498 |
| 20261003-082013-01 | 1920×1080 Brio | 661 px short edge | **77%** | 991×1059 |

So the Brio at that desk distance frames roughly **twice as tight** as the built-in did. Not a
resolution artefact: the fraction of the frame the face occupies is scale-invariant, and this
agent cannot view images, so the face-box fraction is the measurement.

**There is no software fix for the field of view.** `CAP_PROP_ZOOM`, `PAN`, `TILT`, `ROLL` and
`FOCUS` all read **-1.0** on this backend — OpenCV's "control unsupported" sentinel — and
`set(CAP_PROP_ZOOM, …)` reads back -1.0 afterwards, i.e. the writes are inert. An earlier probe
in this session appeared to show zoom widening the frame from 98% to 87%; **that reading was
wrong** and is retracted. The controls do nothing, so the variation was the subject moving. The
Brio 100's lens is simply narrower than the laptop built-in's. **Fix: move back.** No config
change can widen it.

**The real defect this exposed: the crop degenerates, and nothing noticed.**

`pad_box` clips at the frame edge, so when the face is large the 25% recognition margin is
clipped away entirely and `crop_rgb` returns the whole frame:

```
enrolled photo  box (636,20,661,831)  -> crop  991x1059  = 98% of frame height
live at desk    box (15,11,994,1058)  -> crop 1257x1080  = 100% of frame height
good framing    box (700,200,400,500) -> crop  600x750   = 69%, margin intact
```

That last case is the point. dlib's encoder normalises the patch it is handed, so what matters
is the face's share of the crop: 67% in the good case, 98% in the degenerate one. An embedding
taken from the degenerate crop sits in a different part of the identity space than one the gate
will ever produce — which is **exactly the regression D-026 step 5 fixed once already**, where
`enroll_laptop.py` embedded the whole frame while the gate cropped. The 25% padding that
D-020/D-026/D-027 standardised on cannot help here, because it is the padding that gets clipped.

**Fix applied: `enroll_laptop.py` now refuses an unusably sized face and says which way to move.**

`framing_problem()` mirrors `capture_dataset.reason_no_face`, so both capture tools give the
same instruction rather than a frozen counter. Bounds are relative to the frame's **short edge**,
so they mean the same thing at 720p and 1080p:

- `--min-face`, default 60 px — same lower bound as `capture_dataset.py --min-face`.
- `--max-face`, default `MAX_FACE_FRACTION = 0.45` — a face whose short edge exceeds 45% of the
  frame's short edge is rejected. 0.45 sits above the built-in's measured 24–36% and well below
  the Brio's 61% short-edge reading, so it separates the two with margin on both sides.

The reason is drawn on the preview for 600 ms per rejection, per D-021.

**Why a max bound and not a padding-integrity check.** The invariant that actually matters is
"the 25% margin survives clipping", which could be asserted directly against `pad_box`. A
fraction is used instead because it is meaningful to the operator: the message says how many
pixels and which direction to move, and it can be tuned without knowing the pad ratio. The
tests below pin the invariant it is supposed to protect, so the two cannot drift apart silently.

**`capture_dataset.py` was left alone.** Its gate is a *minimum* face size, which serves training
(never label a speck) but says nothing about being too close. Extending it is defensible but was
not needed for this failure and was not done.

**Tests.** `rpi/tests/test_enrol_framing.py`, 7 cases: both measured Brio boxes rejected, all
five measured built-in boxes accepted, "move closer"/"move back" wording, the bound being
short-edge-relative and therefore identical at 720p/1080p/4K, and — the one that matters — that
the box the gate rejects is exactly the box that degenerates the crop, while an accepted box
keeps a real margin. 123 tests pass. Ruff clean apart from the pre-existing unused variable at
`tools/holdout_test.py:247`.

**Still true after all this: enrolment has not happened.** `rpi/known_faces/` is empty and the
Phase 1 gate remains unmeasurable until `enroll_laptop.py --name imman` is run at a working
distance. The 5 old 720p photos are sound and were left in place; the single 1080p photo is
badly framed and is kept only as the raw record the tool is designed to retain.

## D-030 — 2026-10-03 — Enrolment done, but the gate fails at 55%: the holdout is on the wrong camera

**Status.** `rpi/known_faces/imman.npy` now exists (shape `(128,)`, L2 norm 1.0), written
08:54 from the second of two 7-photo runs. The D-029 framing gate worked: the abandoned
08:20 photo (77% of frame height) is `REJECT`, all 14 photos from the two real runs pass.

**The gate fails.** `holdout_test.py` on the 20 holdout frames:

```
faces detected    : 20 (100.0%)
correct identity  : 11 (accuracy 55.0% of detected)     <- needs >= 90%
read as unknown   : 9
wrong identity    : 0
match distance    : mean 0.513, max 0.651               (threshold 0.50)
```

Detection is perfect and there are **zero false accepts**, so nothing is unsafe. But 45% of
never-enrolled frames of the owner read as unknown, and the distances sit right on the
threshold — a distribution straddling 0.51 against a 0.50 cut, not a set of clear misses.

**Cause, measured rather than guessed.** Three enrolment means built from the same raw photos,
each scored against the same 20 holdout frames:

| enrolment | n | intra-run spread | mean holdout dist | acc @0.5 | acc @0.6 |
|---|---|---|---|---|---|
| A today's, 7 @1080p Brio (**the saved one**) | 7 | 0.449 | 0.511 | **60%** | 90% |
| B both runs, 14 @1080p Brio | 14 | 0.471 | 0.504 | **65%** | 90% |
| C old set, 5 @720p built-in | 5 | 0.584 | 0.487 | **90%** | 100% |

And the distance between the means themselves:

```
A (Brio 1080p)  vs B (Brio 1080p)   0.077     same camera, different session
A (Brio 1080p)  vs C (built-in 720p) 0.259    <- half the match threshold
```

**So the enrolment mean and the holdout live in different parts of the identity space, and the
offset is 0.26 — not a marginal threshold problem.** C wins because the holdout was shot on the
built-in camera, and C was enrolled on the built-in camera. A loses because it was enrolled on
the Brio. This is D-028's cross-camera offset (0.166 for one frame at two resolutions) showing
up at 0.26 once different optics and a different session are stacked on top.

**Correction to D-028.** D-028 said the 220-frame dataset was unaffected by not recapturing, and
that no recapture was required. That is **true for detection** — 220/220 stands, and 720p and
1080p letterbox identically at `imgsz=640`. It is **false for identification**: the 20 holdout
frames double as the identification test set, so they are now evaluating a Brio enrolment against
a built-in holdout. That is cross-camera generalisation, which nothing in this project ever
claimed to do. The holdout needs recapturing on the Brio; the 200 training frames do not.

**Do not fix this by loosening `recognizer.match_distance`.** Raising 0.5 → 0.6 takes the saved
enrolment from 60% to 90%, which is exactly the gate criterion. It must not be done yet: the
gate also requires **zero strangers identified as enrolled**, and `rpi/known_faces/` has one
person in it while the stranger set does not exist. Loosening the threshold with no false-accept
evidence is the dangerous direction to fail — it would make the gate pass on paper while
weakening the only property that matters on an intrusion alarm. dlib's own default is 0.6, so
0.6 is defensible *once measured against strangers*, not before.

**Frames 16 and 17 are outliers under every variant** (0.57–0.65, failing at 0.5 even for the
best enrolment). Whatever the camera fix, these two look like poor frames — bad angle or
occlusion — and would cap achievable accuracy at 90%. Worth re-shooting or dropping rather than
tuning around.

**Also fixed: a real crash on the stranger path.** `holdout_test.py:247` called
`collect_stranger_images` without importing it — `NameError` every run, which killed the gate
*after* the holdout report printed, so the stranger criteria never ran. The function exists at
`dataset_meta.py:90`; the import was simply missing. I had earlier characterised the ruff
warning on that line as a cosmetic unused variable and dismissed it. It was this bug, and the
"pre-existing lint noise" was hiding a gate that could never complete. Import added; ruff clean
across the repo.

**Next, in order:**

1. Re-capture the holdout split on the Brio at 1080p — same session and distance as the
   enrolment, `--person imman`, never enrolled on.
2. Re-run the gate. Expect identification near C's 90%+ because enrolment and test are finally on
   the same camera.
3. Build the stranger set, then and only then consider `match_distance`.
4. Re-shoot or drop holdout frames 16/17.

## D-031 — 2026-10-03 — Close the two ways the gate can report a wrong number

**Status.** Two guards added, 136 tests pass (was 123), ruff clean repo-wide.

D-030 diagnosed a gate result that was wrong in a way nobody could see: a Brio
enrolment scored against a built-in 720p holdout, and the output read as a
mediocre `55.0%` rather than as a broken test. Two things made that possible, and
both are now closed.

**1. A holdout can no longer span cameras, or sit on the wrong one.**
`holdout_test.holdout_camera_problem()` reads the `device` field that
`capture_dataset.py` already writes into `metadata.jsonl` for every frame, and
refuses two conditions before any scoring happens:

- the holdout spans more than one `device` → hard error;
- the holdout's `device` differs from `camera.device` in `config.yaml` → hard error.

Both return `NOT_READY` (exit 2) rather than proceeding. Verified against the live
dataset:

```
ERROR holdout was captured on camera device 0 but config.yaml selects device 1;
      the gate would be scoring camera transfer rather than recognition
      - recapture the holdout on the configured camera (see D-030)
exit=2
```

The same 20 frames that previously produced a confident-looking `55.0%` now stop
the run before the detector is even loaded. A missing `device` in metadata is
treated as advisory rather than fatal, because old sets predate the field and a
gate that refuses to run on legacy data is worse than one that warns; device 0 is
an honoured index throughout, so no falsy check quietly skips the built-in camera.

This is the invariant that was actually violated: **the holdout must be shot on the
camera the system will deploy on.** Checking it against `config.yaml` rather than
against a hardcoded index means the guard follows the config when the camera
changes, instead of needing its own edit.

**2. `capture_dataset.py` can no longer store an over-tight crop.**
D-029 added `MAX_FACE_FRACTION` to `enroll_laptop.py`, but the dataset tool only
ever enforced the *lower* bound (`--min-face`). So a holdout captured at a desk
distance — where the Brio's narrower lens puts the face at ~77% of frame height,
the exact framing that was rejected at enrolment — would have been saved silently,
and the resulting gate failure would have been indistinguishable from a model
problem. `--max-face` now defaults to `MAX_FACE_FRACTION = 0.45`, matching
enrolment, and `reason_no_face()` refuses a too-large box with the same
"move back" instruction the enrolment tool gives. `--max-face 1.0` disables it.

The HUD line became `min-face: 60px   max-face: 45%`, and the unusable-frame
counter reads `framing` rather than `too small`, since it now counts both bounds.

Both changes are deliberately conservative about existing behaviour: the
`frame_w`/`frame_h` arguments to `reason_no_face` are optional, so the five
pre-existing two-argument callers keep their lower-bound-only behaviour, and the
device-`None` path still warns instead of failing. 13 new tests in
`rpi/tests/test_holdout_camera_and_framing.py` pin both guards, including the
device-0 case and the disable path.

**Not done here.** No threshold was moved. `match_distance` stays at 0.5 until a
stranger set exists to measure what a looser value would let through (D-030), and
the 200 training frames are still fine — only the holdout needed recapturing.

## D-032 — 2026-10-03 — The 1080p switch made the capture loop unusable: 0.4 fps

**Status.** Preview detects at 640px; labels still written at full resolution.
147 tests pass (was 136), ruff clean.

**Symptom.** The holdout capture "isn't saving anything and feels too slow."

**Measured cause.** `face_recognition`'s HOG detector was being run on the whole
1080p frame. Cost scales with pixel count:

| frame | ms/frame | fps |
|---|---|---|
| 1920x1080 | 2686 | 0.4 |
| 1280x720 | 1304 | 0.8 |
| 960x540 | 687 | 1.5 |
| 640x360 | 144 | 6.9 |

At **0.4 fps** the loop body took 2.7 s, and the loop samples the keyboard exactly
once per iteration via `cv2.waitKey(1)`. So the loop polled for `s` roughly twice a
second: the preview crawled, and short `s` presses were dropped between polls. Both
reported symptoms are the same defect. D-028 moved this project to 1080p on the
strength of a camera probe and never re-checked the annotation path that the new
resolution made four times more expensive.

**Fix — two passes, deliberately asymmetric.** `detect_faces()` gained
`detect_width`, which downscales before detection and rescales the returned boxes
back to full-resolution coordinates. The loop uses it for the status line and the
drawn box, so the preview runs at roughly 7 fps and stops eating keypresses.

The label is *not* taken from that cheap pass. On `s`, the frame is re-detected at
native resolution and the largest usable box from *that* pass is what gets written.
So no stored annotation changed, and the extra ~2.7 s is paid once per saved frame
instead of once per displayed frame. Verified on a real 1920x1080 capture: the
downscale returns `(796, 178, 697, 697)` against full-res `(798, 182, 692, 692)` —
under 1% on every edge, well inside the 5% tolerance pinned by
`test_downscaled_boxes_land_in_full_resolution_coordinates`.

**Why the recheck is not optional.** On the reference photo the full-resolution pass
found **two** faces and the 640px preview found **one** — HOG loses the 78px face
once it is 26px. If the *largest* face were the one dropped, `biggest` would become a
different face and the label would name the wrong person, silently. The recheck
removes that failure mode by construction, and if the frame passes the preview but
fails the recheck it is refused with a log line rather than saved. This is the same
class of bug as D-031: the number looks fine and measures the wrong thing.

**Trade-off accepted.** Small faces can be missed by the *preview* only, which costs
a momentarily absent box on screen and nothing else. `--detect-width 0` restores
native detection everywhere, and `--detect-width` is on the command line for anyone
who wants to tune it. `usable_boxes()` was extracted from the loop so both passes
share one definition of "usable" and neither can drift; ruff's `B023` caught the
first attempt as a loop-variable closure, which was a real late-binding hazard.

**Also fixed in the same area.** `usable_boxes()` checks both bounds in
full-resolution pixels, so acceptance no longer depends on the resolution detection
ran at — previously the "too small" message was driven by the same list the save
decision used, and the two could disagree once the two passes existed.

**Not changed.** `min_face`, `pad`, `--max-face`, the detector's `imgsz=640`, and the
saved label format. HOG remains the model; `model="cnn"` gives tighter boxes but
costs seconds per frame, which is the problem being fixed here.

## D-033 — 2026-10-03 — "ready - press S" was a lie: two decisions, and the second overruled the first

**Status.** One assessment per frame, consumed by both the HUD and the save.
Save-path detector is now the pretrained YOLO at 640. 172 tests pass (was 147),
ruff clean.

**Reported symptom.** At 1080p the preview showed a green box, `faces: 1`, and
"ready - press S to save"; pressing `S` incremented the *no face* counter (15 → 22)
and saved nothing.

**Cause.** D-032 made the loop fast by detecting twice: a cheap downscaled pass drew
the box and the status line, then a full-resolution recheck ran on `s` and could
overwrite `biggest` with `None`. The recheck's verdict landed *after* the HUD had
already promised a save, and its only output was a `LOGGER.warning` in a console the
operator was not watching. So the visible state and the acted-on state were decided by
different code, and the operator acted on the wrong one. The "no face" increments came
from subsequent iterations, not from the pressed frame — the counter was reporting
detector flicker while the actual refusal was the silent recheck.

**Fix 1 — one decision, one frame.** `assess_frame()` returns a frozen `Assessment`
holding the frame, the boxes, the usable boxes, the best box, the reason string, the
luma and the detector description. The HUD and the save both read that one object, and
`view.frame is frame` — the displayed pixels *are* the saved pixels, asserted rather
than assumed. The two-pass recheck is deleted, not reconciled.

**Fix 2 — dlib is out of the save path.** `--annotator` defaults to `yolo`: the
project's own `face_pretrained.pt` at `imgsz=640`, the same detector the gate scores
with. Measured on a 1920x1080 frame:

| detector | ms/frame |
|---|---|
| dlib HOG, native 1080p | 2686 |
| YOLO `face_pretrained.pt` @640 | 118 |

23x cheaper, and it removes the resolution-divergence that caused the bug in the first
place. `--annotator dlib` is kept so the existing dataset stays reproducible, and it
logs a warning saying the labels will not come from the model the gate scores with. A
missing `--face-model` raises rather than falling back to dlib, so the save path can
never silently become the slow one. The HUD and startup log now both print
`detector: face_pretrained.pt (ultralytics YOLO) imgsz=640`.

**Fix 3 — refusals are evidence.** Every save refused now writes the frame to
`rpi/datasets/_rejected/` as
`NNNN_<reason>_box<x>_<y>_<w>_<h>_luma<NN>.jpg` and holds the reason on screen for 1 s,
stamped onto the frame rather than left to a status line that the next iteration
overwrites. A refusal nobody can read is a refusal nobody can act on — which is how this
ran twenty times unnoticed.

**Two bugs found while verifying, both mine, both silent.**

- *Colour space.* `assess_frame` was handing **RGB** to ultralytics, which expects
  **BGR**. This does not raise — it returns a worse face. On a real photo the same
  frame gave `(245, 938, 506, 142)` via RGB and `(936, 251, 1325, 756)` via BGR.
  `FaceDetector.boxes_for` now receives the camera's native BGR and each detector owns
  its conversion: ultralytics reads BGR, dlib converts to RGB.
- *Coordinate order.* ultralytics reports `xyxy` as `(x1, y1, x2, y2)`; it was being
  unpacked as `(top, left, bottom, right)`. Four plausible numbers come out either way,
  so this produced a garbled rectangle rather than an error — and garbled rectangles
  pass the size bounds often enough to look like faces. The corrected box on the
  reference photo is `(936, 251, 389, 505)`: portrait, 36% of frame height, and the
  spurious 53x64 detection beside it is now correctly refused as too small.

Both were caught by writing the agreement tests *after* the refactor, which is the
argument for having written them. `test_yolo_boxes_are_not_transposed` asserts a real
face box is portrait; `test_yolo_receives_bgr_not_rgb` compares against a direct BGR
call.

**`--pad` default is now annotator-dependent**: 25% for dlib (inner-face box), 0% for
yolo (already whole-face). An explicit `--pad` still wins. This changes label geometry
for future captures — existing labels are untouched and remain dlib-padded, which is
the D-020 geometry the pretrained model was trained on.

**Caveat worth stating.** Labelling with the model being trained introduces a
dependence the dlib path did not have: re-annotating with a different `--face-model`
silently changes what "a face" means across the dataset. That is a real cost of making
the dataset and the gate agree, and it is why `--pad` is pinned rather than left loose.

## D-034 — 2026-10-03 — The save block was outside the capture loop: S saved one frame per run, not per press

**Status.** Save block moved inside the loop. 179 tests pass (was 172), ruff clean.

**Reported symptom.** "I have clicked S a million times - it's not working." The log:

```
INFO  split=holdout ... target=20. keys: s save, n skip, q quit
INFO  saved holdout_front-light_0005 (1/20)
INFO  wrote rpi\datasets\faces\data.yaml
WARNING only 1/20 frames captured - rerun to continue
```

Exactly one frame, then exit.

**Cause — an indentation slip, and it is old.** The capture loop's save block sat at
8 spaces while the loop body was at 12:

```python
    while saved < args.count:
        ...
        biggest = view.best

    stem = f"{split}_{args.condition}_{highest + saved + 1:04d}"   # <- outside
    cv2.imwrite(...)                                                # <- outside
    saved += 1                                                      # <- outside
```

`saved` was incremented *after* the loop finished, so the loop condition
`while saved < args.count` was never satisfied by saving anything. Consequences, in
order:

1. The loop could only ever exit via `break` — on `q` or `Esc`. It could not exit on
   `--count`.
2. Every `S` press reached `if key not in ("s", "a"): continue` → passed → then
   `continue`d. The press was consumed and discarded. Repeated presses did nothing,
   which is exactly what "clicked S a million times" describes.
3. On quit, the block after the loop wrote **one** frame from whatever `frame` and
   `biggest` happened to be in scope, then reported `1/20` and told the operator to
   rerun.

So the tool worked exactly once per run, by accident, on the way out. This predates
D-032 and D-033 — it was not introduced by the refactor, and every test in the suite
passed while it was present, because the bug needs a camera, a window and a keypress
to manifest.

**Why the earlier diagnosis was incomplete.** D-033 fixed the HUD disagreeing with the
save, and both of those were real. But "nothing saved" had a second, independent cause
sitting underneath, and it was the one actually producing the symptom. Attributing a
no-save report entirely to the two-pass recheck was wrong: that bug could make a save
*refuse*, but it could not explain a run that saved precisely one frame and exited.

**Fix.** The save block is inside the loop, and `cv2.imwrite` now takes `view.frame` —
the exact object `assess_frame` judged — rather than the loop's own `frame` variable,
so the D-033 guarantee survives the move. `--count` now terminates the loop.

**Regression test, and it is a shape test.** `rpi/tests/test_capture_loop_shape.py`
parses the tool's AST and asserts that the `stem` assignment, `saved += 1`, the
`cv2.imwrite` call, `write_yolo_label`, `append_metadata` and the `saved %s (%d/%d)`
log all sit inside the `while saved < ...` body. Behaviour cannot be tested here
without hardware, but the *shape* can, and the shape is what broke.

The test was verified by re-introducing the dedent on a scratch copy: three of the
assertions fail, and they pass again once it is removed. A guard that cannot be shown
to fail is not a guard.

**Also true, and worth recording because it changes the diagnosis.** The camera opened
in **1.6 s** on this run (`camera 1 ready at 1920x1080 @ 30.0fps`), not the ~191 s
stall of D-028. The stall is not reproducible on every open and should not be treated
as a fixed cost. Anyone reading an earlier note that "every capture takes three minutes
before the window appears" should re-measure before believing it.

**Dataset state.** `images/holdout/` held 5 files: `0001`–`0004` are the original
device-0 720p captures and `0005` is the first Brio frame. Those four must be archived
before the gate will run at all — D-031's camera-consistency guard refuses a holdout
that spans devices, which is exactly the mixed set now on disk.

## D-035 - 2026-10-03 - metadata.jsonl accumulates ghost rows; the gate saw 43 holdout frames when 5 existed

**Symptom.** After archiving the 20 old device-0 holdout frames, the gate still
refused to run: `holdout spans 2 cameras (devices 0, 1)`. Five valid device-1
frames were on disk, so the mixed-camera claim was not about the images - it was
about the log.

**Cause.** `append_metadata` **appends**. It never removes the row for a file that was
archived or re-captured. The file had therefore drifted badly out of sync:

| source | rows | stems |
|---|---|---|
| device 0, 1280x720, archived | 20 | `0001`-`0020` |
| device 1, 1920x1080, on disk | 23 | `0001`, `0004`, `0005`-`0025` |

Two distinct faults, both invisible from the filesystem alone:

1. **Ghost rows.** 20 rows referenced files that no longer existed.
2. **Stem collisions.** `0001` and `0004` appeared under *both* devices. Recapture had
   restarted numbering at `0001` while the old files were still present, so two
   different physical frames claimed the same name. `load_metadata` resolves collisions
   by taking the last row, which means the winner depended on append order, not on
   which file was actually on disk.

Total: 43 holdout rows for 5 real frames.

**Why this matters beyond the confusing message.** The camera-consistency guard added in
D-031 reads *metadata*, not the filesystem. So a stale row can veto a perfectly valid
holdout (what happened here), and - worse - a future capture that reused a stem whose
last row belonged to the old camera would make the guard reject a genuinely
single-camera set. Silent, order-dependent, and it fails safe only by accident.

**Repair.** Rewrite `metadata.jsonl` keeping only rows whose `file` still exists under
the dataset root, last-wins per file. Backup at `rpi/datasets/faces/metadata.jsonl.bak`.

Result: 43 holdout rows -> 5, all device 1. 205 rows total: 200 bootstrapped WIDER
train frames plus the 5 Brio holdout frames.

**The `device -1` in the log is not a bug.** `training_cameras()` reported
`device -1, device 1`, which looked wrong. All 200 train rows are
`condition: bootstrapped` with `actual: []` - they are WIDER frames (D-026), never
captured on a device, so `-1` is the intended sentinel. The holdout is the only real
capture and it is entirely device 1. Left as-is deliberately.

**Lesson.** `metadata.jsonl` is an append-only log, but the gate treats it as ground
truth about the filesystem. Those two contracts cannot both hold indefinitely. Either
metadata must be pruned when files move, or the gate must intersect metadata with the
files actually present. Pruning was done by hand here; it should become a tool
(`tools/repair_metadata.py`) before the next archive/recapture cycle, because the next
person to hit this will not connect the gate error to a stale log.

## D-036 - 2026-10-03 - Holdout split three ways; the reported failures were not the ones blamed

**Status.** `holdout-hard` 4/4 (100%). `holdout2` 12/15 (80%). Pooled 16/19 (84.2%).
Gate still FAIL on identification accuracy. 203 tests pass, ruff clean.

### The premise was wrong, and the data said so plainly

Reported: "the 4 failed holdout frames were taken in a different place, farther away,
with strong sunlight and a colour shift."

Measured, per frame, from the images themselves:

| group | frames | face width | mean luma Y | mean R-B | unknowns |
|---|---|---|---|---|---|
| far | 4 | 164-166 px | **113.5** | **+36.8** | **0** |
| close | 15 | 232-298 px | 79.3 | +59.8 | 3 |

Three separate facts, each contradicting the story:

1. **The far frames are the brightest set, and they all pass.** 4/4, max distance
   0.486 - comfortably inside the 0.50 threshold. The set described as the failing
   one is the best-performing set in the dataset.
2. **The colour shift is in the close group, not the far one.** R-B is +59.8 close
   vs +36.8 far. A high R-B is a warm/red cast, so whatever colour shift exists is
   in `holdout2`. The far set is comparatively colour-neutral.
3. **There were 3 failures, not 4**, and all three are in the close group.

So the far/sunlit hypothesis does not merely lack support - it is inverted. Chasing
it would have meant "fixing" the set that already works.

### What the width column shows instead

```
holdout_front-light_0032.jpg  d=0.515  w=252px h=331px aspect=0.761
holdout_front-light_0035.jpg  d=0.589  w=248px h=346px aspect=0.717
holdout_front-light_0040.jpg  d=0.622  w=241px h=255px aspect=0.945
```

The three failures are not size outliers - 252, 248 and 241 px sit inside the
close group's own 232-298 px range, and their luma (77.3) and R-B (+58.8) match
that group's mean almost exactly (79.3, +59.8). Lighting and colour do not explain
them.

**0040 is a genuine outlier, in shape.** Its aspect is 0.945 where every other
owner frame is 0.717-0.761: a 241x255 box is nearly square, i.e. wider than tall,
which no upright head produces. The recognition crop is padded from this box, so
0040 is almost certainly a partial face - clipped at the chin or forehead - and a
crop missing forehead and jawline is exactly the failure D-020 documented. That is
one bad frame, not a recogniser weakness, and the honest response is to look at the
image, not to retune the threshold.

That leaves 0032 (0.515, a 0.015 miss) and 0035 (0.589) as the real signal, and
0035 is unexplained by size, brightness, colour or aspect. **No image has been
looked at yet.** Every statement above is pixel and metadata arithmetic; nobody has
confirmed what pose or expression those two frames contain. That is the next step,
and it is a two-minute job for a human.

### Split layout

| split | frames | role |
|---|---|---|
| `holdout-hard` | 4 | preserved verbatim, never retaken or deleted |
| `holdout2` | 15 | the routine close set |
| `holdout` | 0 | empty; kept so older instructions still resolve |

`holdout-hard` keeps "holdout" in its name deliberately: `assert_holdout_is_clean`
substring-matches `holdout` against `data.yaml`'s train/val keys, so a split named
"difficult" would slip past the leak guard. A test now pins that.

`holdout-hard` is preserved as instructed and will never be used for tuning. With
4 frames it cannot be: one frame is 25 points of accuracy. It is a fixed regression
witness, not a measurement.

### Gate reports per split, and pools for the decision

`--splits` selects; default is every gate split with frames on disk. Each split gets
its own block, then a pooled block that the gate actually decides on.

Pooling recomputes rates from summed counts rather than averaging per-split rates.
This is not pedantry - with the current data, averaging the rates gives
(100% + 80%)/2 = **90.0%**, which *passes*. The true figure is 16/19 = **84.2%**,
which fails. A 4-frame split would have carried the same weight as a 15-frame one
and the gate would have passed on a number that does not exist. Pinned by
`test_aggregate_weights_by_frame_count_not_split_count`.

### Face width on unknown frames

`score_images` now records width, height and aspect for every miss and logs them
inline. "It read unknown" becomes "it read unknown *at 0.622 with a 241x255 box*",
which is a diagnosis rather than a mood. The table is suppressed for strangers,
where reading unknown is the pass condition - listing 50 of them buries the
owner's three real misses.

### Enrolment may never read a held-out frame

`assert_not_gate_split` refuses any path with a gate-split component, and
`enroll_laptop.py` calls it on both `--photo-dir` and `--faces-dir` before opening
the camera. This is not theoretical: the tempting optimisation here is "average all
31 existing enrolment photos", and a mistyped `--photo-dir` pointing at
`images/holdout2` would silently produce a **passing** gate that measured nothing.
That is the most expensive failure mode in this project, so it is refused rather
than warned about. `review/holdout_720p_builtin` stays allowed - it contains the
substring but is not a split, and blocking it would break archiving.

### Enrolment now prompts the four conditions

`CONDITION_PROMPTS` cycles indoor lamp -> daylight window -> ~1 m -> ~2 m, shown on
the preview, logged per photo, and summarised afterwards with a warning naming any
condition never captured.

The reason is that `enroll_laptop.py` averages only the photos from the current run
and overwrites the single `.npy`. The present embedding is the mean of one 7-photo
run shot inside about a minute - a narrow centroid. `load_all` globs `*.npy` with
name = stem, so several files would register as several *different people*; one
centroid per person is the design, and the only lever is the spread of conditions
behind it.

Note the tension worth being honest about: `holdout-hard` was captured at ~2 m and
passes 4/4, while failures sit in the ~1 m set. So "~2 m" is not obviously the
missing axis. The prompts cover all four anyway, because guessing which condition
matters from four frames is how the previous hypothesis went wrong.

### Addendum - the operator looked at the three frames, and the axis is pose

Visual inspection of the three unknown frames:

| frame | distance | what the operator saw | box aspect |
|---|---|---|---|
| `0032` | 0.515 | looking to their right (yaw right) | 0.761 |
| `0035` | 0.589 | looking to their left (yaw left) | 0.717 |
| `0040` | 0.622 | chin tilted **up** (pitch up) | **0.945** |

So the failure axis is **head pose - yaw and pitch - not lighting and not distance**.
The operator's note that only `0040` "feels really different" is also consistent:
`0032` and `0035` sit at normal head proportions and are ordinary pose variation,
whereas `0040`'s near-square box is a genuine outlier.

**This confirms the aspect-ratio inference.** `0040` was flagged as a partial face
from pixel arithmetic alone - a 241x255 box wider than tall, which no upright head
produces. Tilt the chin up and the visible face foreshortens vertically while the
jaw-to-temple span stays put, so the box goes square. The prediction held up against
the one thing arithmetic cannot check.

**The four prompts written earlier targeted the wrong axis.** `CONDITION_PROMPTS`
was lamp / window / 1 m / 2 m, all four built on the lighting-and-distance
hypothesis that these very frames refute. It now carries four more:

```
turn your head about 30 degrees to YOUR LEFT,  keep eyes forward
turn your head about 30 degrees to YOUR RIGHT, keep eyes forward
chin tilted UP about 20 degrees, as if looking at something above
chin tucked DOWN about 20 degrees, as if looking at something below
```

Eight prompts total, so `--photos 8` covers each exactly once. The original four
keep their leading order, which a test pins - they were requested explicitly, and
they are still cheap even though the evidence no longer points at them.

**Per-condition distance report added to enrolment.** `enroll_laptop.py` now reports
each photo's distance to the centroid it just wrote, grouped by the condition it was
shot under, and flags anything past 0.40. A pose photo sitting far from the centroid
means that pose is not actually represented - visible at enrolment time instead of
three gate runs later, which is the same lesson D-035 learned about chasing
per-run aggregate metrics instead of individual data points.

### What this does not fix, stated plainly

The dlib 128-d embedding is **known to degrade under yaw and pitch**. Enrolling pose
variation widens the centroid so the gate's own 0.50 threshold still works, but it
does not remove the underlying pose sensitivity. If a pose-complete enrolment still
rejects `0032`/`0035`, then more photographs are the wrong remedy and the fix is a
pose-robust embedding - ArcFace/InsightFace - which is a model change, not a data
change. It is explicitly *not* a reason to raise the threshold, which would trade
owner recall for stranger rejection at exactly the wrong place.

Two further honest limits. `holdout-hard` is a ~2 m set that passes 4/4; it contains
no head-pose variation, so it says nothing about pose robustness and cannot be used
to tune this. And with 19 frames, repeated re-enrol/re-score cycles are tuning
against noise - one re-enrolment, then judge.

## D-037 - 2026-10-03 - The operating envelope is defined, and one frame is excluded rather than deleted

**Status.** `holdout_front-light_0040.jpg` excluded with a recorded reason, file
still on disk. Dev gate now passes: 17/18 = 94.4%, 0 false accepts. 211 tests pass.

### The request, and why it was not simply obeyed

The frame failed, and the proposal was to delete it. Deleting it was declined, and
the reason is arithmetic rather than principle:

```
before:  17/19 = 89.5%   FAIL
after:   17/18 = 94.4%   PASS
```

One file moves the gate from fail to pass. A metric that a single image can toggle
is not measuring the recogniser; it is measuring whether the inconvenient photo was
kept. Every accuracy figure this project has produced would become unfalsifiable the
moment deleting a failure were allowed, so the action needed a mechanism that
distinguishes the two cases - not a refusal on taste.

### The distinction the mechanism draws

```
excluding  a frame the deployment will never produce
deleting   a frame that failed - identical in the result, opposite in meaning
```

The second is only distinguishable from the first by a record of intent. So
exclusion is now a metadata field with a mandatory reason, applied by
`tools/holdout_envelope.py`, and it has **no delete option at all** - pinned by
`test_envelope_tool_has_no_delete_option`, which greps the tool for
`os.remove`/`unlink`/`rmtree` so a future edit cannot quietly add one.

`holdout_test.py` prints every exclusion, with its reason, before it scores
anything, and reports the exclusion share of the split. Past 20% it warns that the
percentage rests on a much smaller sample and overstates real-world accuracy. The
denominator only ever shrinks by a decision a reviewer can be shown.

### Why 0040 is legitimately out of envelope

The operator established the deployment constraint: the camera mounts **at or below
eye level**, so users face it rather than looking up at it. The frame has the
subject's chin tilted up roughly 60 degrees. At eye level that is a subject looking
at the ceiling, not at the device - a case the installed system will not encounter.

This is a statement about the *deployment*, made before the number was consulted. It
is the kind of scope decision a product owner is entitled to make; what would not be
legitimate is discovering the frame failed and then reasoning backwards to a scope
that happens to exclude it. The order matters, and it is the reason the exclusion
records the reasoning rather than just a flag.

`0035` (yaw ~30 degrees) stays **in** the gate. At or below eye level people glance
sideways routinely, so that pose is squarely in envelope - and it is the one genuine
remaining failure, at d=0.554.

### holdout-hard is immune

`partition_envelope` returns `holdout-hard` unscored-and-unaltered, always. That
split exists to be a fixed regression witness; if it could be excluded it would be
hollowed out one justified exception at a time until it proved nothing, which is
the same failure as deletion with extra steps. The tool refuses a `holdout-hard`
frame by name. Pinned by
`test_partition_refuses_to_exclude_anything_from_holdout_hard`.

### What this PASS is, and what it is not

It is a **dev-set** pass. `holdout2` has now driven prompt design, embedding changes
and two exclusion decisions, so it is no longer an untouched holdout and its score
overstates real-world accuracy by an unknown amount. The operator chose exactly that:
treat `holdout2` as dev, then capture a fresh evaluation set.

The other half is arithmetic that no exclusion changes. Eighteen frames cannot
resolve a 90% threshold: 17/18 is 94.4% and there is no value between it and the
next frame down. The Wilson 95% interval on 17/18 is roughly 74-99%. **A single
frame still decides this gate**, which is the strongest argument for the larger
evaluation set and the weakest possible basis for declaring the system accurate.

Next: freeze the enrolment, capture a clean evaluation set of at least 20 frames
(preferably 30-40) on the configured camera, and score it exactly once.

## D-038 - 2026-10-03 - One source of truth for split names, and the split that must stay uncapturable

**Status.** `--split holdout2` works. `--split holdout-hard` is refused by
construction. 216 tests pass.

### The bug, and why it was not a typo

```
capture_dataset.py: error: argument --split: invalid choice: 'holdout2'
  (choose from 'train', 'val', 'holdout', 'strangers')
```

`HOLDOUT2` had been in `dataset_meta.GATE_SPLITS` since D-036, and the gate scored
it happily. But `capture_dataset.py` never imported that constant - it carried its
own tuple:

```python
choices=("train", "val", "holdout", STRANGERS)   # HOLDOUT2 missing
```

So the tool that *creates* gate frames could not name the split the gate *reads*.
The split names lived in two places, one of which was updated and the other not.
That is the whole defect; there was no logic problem, and every individual line was
correct.

### Fix: derive, do not restate

`CAPTUREABLE_SPLITS = (TRAIN, VAL, HOLDOUT, HOLDOUT2)` in `dataset_meta`, and
`choices=(*CAPTUREABLE_SPLITS, STRANGERS)`. A test now greps `capture_dataset.py`
for the old hardcoded tuple and fails if it reappears, so the two lists cannot
quietly diverge again.

### holdout-hard is excluded from CAPTUREABLE_SPLITS, on purpose

`GATE_SPLITS` and `CAPTUREABLE_SPLITS` are deliberately *not* the same tuple. The
gate scores three splits; capture may write to two of them.

`HOLDOUT_HARD` is the fixed regression witness (D-036). It is valuable precisely
because it never changes - 4 frames that have said the same thing through every
enrolment and threshold revision since. Adding captures to it would let a slow
series of "just a few more frames" dilute it until it measured nothing, which is
deletion with extra steps and no audit trail. Since it is absent from the choices
tuple, argparse rejects it structurally:

```
capture_dataset.py: error: argument --split: invalid choice: 'holdout-hard'
```

Making it a *rejected* value rather than a value that raises a polite error is
deliberate: an option that appears in `--help` invites use, and the whole point is
that this door has no handle on it.

### A second, quieter leak closed in the same pass

The never-train-on warning was gated on `if split == "holdout":`. With `holdout2`
now capturable, that warning would **not** have fired for it - so the one message
telling an operator these frames must never be trained on would have been silently
skipped for the split most likely to be recaptured, and in favour of the split
nobody was about to use. It is now `if split in GATE_SPLITS:`.

This is the same class of bug as the choices tuple: a name-specific condition where
the intent was a category. Two of them, in one tool, both found by asking "what
breaks when a new split name appears?" - which is exactly the question that D-036's
split split should have prompted the first time.

`data.yaml` also only ever wrote `train` and `val`, so `holdout2` could not leak into
training; its comment named only `images/holdout` and is now corrected to cover every
gate split. Verified rather than assumed - the assertion was worth checking precisely
because it was the kind of thing that is usually true.

## D-039 - 2026-10-03 - Frame names must be unique across the whole dataset

**Status.** 9 clobbered metadata rows reconstructed; all three gate splits renamed
to carry their split; `load_metadata` and `append_metadata` now refuse collisions.
Fresh evaluation set restored: **30 frames, 29 correct = 96.7%**, GATE PASS.
225 tests pass.

### What happened

Capturing 30 frames into `--split holdout` corrupted two other splits:

```
[holdout2] 0026-0030: matched 'imman' but expected '' - WRONG IDENTITY
[holdout2] per condition:  imman 8/9 | unknown 0/5
ERROR holdout-hard frames exist but none were captured with --person
```

`expected ''` was the tell. Five frames had no expected identity, and holdout-hard
had lost *every* identity - not a recognition regression, a bookkeeping one.

### Cause

All three gate splits shared the `holdout_` filename prefix, and 9 frame numbers
overlapped:

```
images/holdout-hard/holdout_front-light_0021.jpg
images/holdout/holdout_front-light_0021.jpg      <- same basename, different file
```

`load_metadata` indexes rows **by bare filename**:

```python
name = str(record.get("file", "")).rsplit("/", 1)[-1]
records[name] = record          # last row wins, silently
```

The 30 new rows were appended last, so they replaced 4 `holdout-hard` and 5
`holdout2` rows. Those frames kept their images and labels but lost their `person`,
`split` and `device` - which is why the gate reported five frames as WRONG IDENTITY
(it compared against an empty expected name) and why `holdout-hard` could not be
scored at all.

This is the same root cause as D-035, one level up. There, append-only metadata
accumulated rows for files that no longer existed; here it accumulated rows for
files that *do* exist, under a key that cannot tell them apart. Append-only plus a
too-weak key is a silent data-loss machine, and the two halves had to be fixed
together.

### Why it was not caught sooner

Three inspections had passed: row count matched image count, `holdout-hard` had
scored 4/4 hours earlier, and 9 of the 30 new numbers were outside the overlapping
range. The gate *did* catch it - `expected ''` and the holdout-hard hard-stop are
both this project's own assertions firing - but it could only report the symptom,
because by then the underlying rows were gone. The gate was the last line of
defence, not the first.

### Fix, in three parts

**1. Repair.** The pre-capture metadata was intact in `metadata.jsonl.bak-envelope`
(219 rows, zero collisions - itself proof the dataset was clean a moment earlier).
The 30 new rows were re-appended from the corrupted file, and the 19 clobbered rows
restored from the backup.

**2. Rename, so the shape of the data matches what the tools produce today.**
`capture_dataset.py` derives each stem from its split (`prefix =
f"{split}_{condition}_"`), so legacy frames were renamed to match:

```
holdout2/holdout_front-light_0026.jpg      -> holdout2/holdout2_front-light_0026.jpg
holdout-hard/holdout_front-light_0021.jpg  -> holdout-hard/holdout-hard_front-light_0021.jpg
```

39 files plus labels. After this every split owns a distinct prefix, and future
captures cannot collide by construction. Verified rather than assumed: 249 rows, 0
duplicate basenames, disk and metadata agree exactly.

**3. Refuse collisions structurally.**

- `find_basename_collisions` / `assert_unique_basenames` / `MetadataCollisionError`
  in `dataset_meta`.
- `load_metadata` checks the **raw rows before indexing**. The first attempt checked
  *after*, which was useless - indexing had already collapsed the duplicate, leaving
  nothing to detect. That is precisely how the original bug survived every
  inspection, and the guard initially reproduced it.
- `append_metadata` refuses *before writing*, so a rejected append is a no-op and
  costs the operator nothing.
- `capture_dataset.py` runs the check in `main()` **before the camera opens**, so a
  clash costs a session rather than a dataset.
- `holdout_test.py` catches it and exits `NOT_READY` (2) instead of printing a
  plausible wrong number.

Verified by forging a real cross-split duplicate: both the gate and the capture tool
refused with the same message and the gate exited 2 without scoring anything. The
first forgery attempt used the *same* path twice and correctly did **not** fire -
a redundant row is not a collision, and that distinction is pinned by
`test_identical_repeated_rows_are_not_a_collision`.

### The clean result

```
[holdout]      30 frames  29 correct  96.7%   mean 0.439  max 0.501
[holdout2]     14 frames  13 correct  92.9%   mean 0.452  max 0.554
[holdout-hard]  4 frames   4 correct 100.0%   mean 0.462  max 0.471
[POOLED]       48 frames  46 correct  95.8%
strangers      50/50 detected, 0 false accepts
GATE: PASS
```

The evaluation set's distribution is visibly tighter than either dev set - mean 0.439
against 0.452 and 0.462 - and its single miss, `0012` at d=0.501, is a hair over
threshold rather than the pose failures that dominated the dev sets. That is the
honest number, on 30 frames that had never influenced a single decision.

### One self-inflicted artefact, recorded

Verifying the guard required running a capture, and the first forged "collision" was
not one (same path twice), so the pre-flight correctly allowed it - the camera opened
and captured `holdout_front-light_0031.jpg`, which then had no metadata row after the
restore and showed up as a WRONG IDENTITY. Removed, along with 18 empty `envelope`
keys the rename had added. Worth stating plainly: the test data was briefly wrong
because I verified a guard by exercising the real camera path rather than a fixture,
and the first attempt at that verification was itself flawed.
