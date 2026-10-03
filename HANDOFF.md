# S.H.I.E.L.D — Handoff

**Protocol (do not re-derive):** Wi-Fi/TCP, **not** MQTT. RPi hosts WPA3 AP `shield-net`;
ESP32 connects to `192.168.50.1:8765`; frames are length-prefixed JSON + HMAC-SHA256;
heartbeat 2 s both ways; 5 s reply timeout; local fail-safe. `docs/PROTOCOL.md` v2.0.0 is
authoritative. `config.yaml` is **1920x1080** on **device 1** (D-028). Secrets are `${VAR}`
into `.env`.

## Done

- **Phase 0** passed. DECISIONS D-001…D-039. **225 tests pass, ruff clean repo-wide.**
- **Camera** `camera.py` — MJPG + readback, warn-and-continue, 10 warm-up, `as_bgr()` fixes
  packed YUYV (D-024). **1920x1080 on the Logitech Brio 100, device 1, verified on the host**
  (true 1080p, `matches_request=True`, 5-frame burst 0.17 s / ~29 fps).
- **Capture** `capture_dataset.py` — live skip reasons, per-frame provenance in
  `metadata.jsonl`, numbering continues across runs (no overwrite).
- **Dataset** 220 frames on **device 0** (old built-in camera, still 1280x720 — valid per
  D-028): train 200 (front 40, side 40, dim 40, angle-l 20, angle-r 20, mid-1m 20, near-0.5m
  20) + holdout 20. **Not recaptured on the Brio** — 720p and 1080p letterbox identically at
  `imgsz=640`, so recapture would cost the verified 220/220 rate for nothing.
- **Label format fixed** — capture wrote 4-field labels (no class id), which YOLO misreads
  silently. All 220 repaired, each verified against the pixel box in `metadata.jsonl`
  (`fixed=220 mismatch=0`). `FACE_CLASS_ID` now shared.
- **Pretrained detector (D-026/27)** `rpi/models/face_pretrained.pt` SHA256
  `37396AC6…F674E71`, `nc=1 names={0:'FACE'}`. **100% detection, 220/220**, conf p5 0.88.
- **Bootstrap labelling** `bootstrap_label.py` — train relabelled 200/200; holdout labels
  left on old padded-HOG boxes as a cross-check.
- **Recognition padding (step 5)** — centralised in `recogniser.crop_rgb`, called by both
  `holdout_test.py` and `live_test.py`. `enroll_laptop.py` rewritten to detect-then-crop
  with the same padding (it previously embedded the whole frame).
- Also: `preview_labels.py`, `holdout_test.py`, `camera_check.py`, `recogniser.py`,
  `face_store.py`, `live_test.py`.

## Blocked — needs the user

- **`rpi/known_faces/imman.npy` exists** (enrolled 08:54, 128-d, norm 1.0) — **but the gate
  fails at 55% identification, needs ≥90%.** Cause is measured, not guessed: the enrolment is on
  the **Brio 1080p**, the 20 holdout frames are on the **built-in 720p**, and the two enrolment
  means sit **0.26** apart — half the 0.5 threshold. Enrolling on the holdout's own camera scores
  **90%** (100% at 0.6). This is cross-camera generalisation, which was never the requirement
  (D-030). **Detection is unaffected: 20/20 detected, zero false accepts.**
- **Re-capture the holdout on the Brio** (`--person imman`, same session/distance, never
  enrolled on). The 200 *training* frames do **not** need recapture — 720p and 1080p letterbox
  identically at `imgsz=640` (D-028). Only the holdout does, because it doubles as the
  identification test set. **The gate now refuses to run until this is done** — it detects the
  old set's `device: 0` against `config.yaml`'s `device: 1` and exits `2` with an explanation,
  instead of reporting a misleading score (D-031).
- **`capture_dataset.py` now enforces `--max-face` (default 45% of the short edge)**, matching
  enrolment. If the preview says "move back", step further from the Brio before saving — an
  over-tight crop scores badly at the gate and would be indistinguishable from a model problem
  (D-031).
- **Do NOT raise `recognizer.match_distance` to 0.6 to make the gate pass.** It would take the
  saved enrolment 60% → 90%, but there is no stranger set, so the false-accept cost is
  unmeasured, and the gate also demands zero strangers identified as enrolled (D-030).
- **Holdout frames 16 and 17 are outliers under every enrolment** (0.57–0.65) — bad angle or
  occlusion. Re-shoot or drop them; they cap achievable accuracy at 90%.
- **No stranger set** → stranger detection ≥95% unmeasurable. Detection rate alone is not the
  gate.
- **No general-face data.** `datasets/WIDER_train/` has 12,880 images but **no**
  `wider_face_train_bbx_gt.txt`. Non-commercial licence; optional per D-026.
- `review/*.png` written but **unviewed** — this agent cannot read images (no image input). Face
  framing has been judged by face-box fraction instead. A human must still confirm the boxes are
  whole-face.

## Next

1. **Re-capture holdout on the Brio, then re-run the gate** (see Blocked). The preview now
   runs at ~7 fps instead of 0.4, so `S` presses register (D-032).
2. Build a stranger set (faces that are *not* imman); run the gate:
   `holdout_test.py --face-model rpi/models/face_pretrained.pt --strangers <dir>`
3. Only then consider `match_distance`, with the stranger false-accept rate in hand.
4. Fine-tune (step 4, **only if** the gate still fails): lr0 0.001, epochs 10–20, freeze 10, on
   these captures + ~500 general faces. Re-run the gate.

## Capture performance and the save path (D-032, D-033)

`capture_dataset.py` computes **one** `Assessment` per frame; the HUD and the save both
read it, so "ready - press S" cannot be shown for a frame the save then refuses
(`view.frame is frame` — displayed pixels are the saved pixels). Do not reintroduce a
second detection pass for speed; that is what produced the silent-refusal bug.

- `--annotator yolo` (default) labels with `rpi/models/face_pretrained.pt` at
  `--imgsz 640` — the same detector the gate scores with, and 118 ms/frame against
  dlib's 2686 ms at 1080p. `--annotator dlib` reproduces the legacy dlib-padded labels
  and logs a warning saying so.
- `--pad` defaults per annotator: 25% for dlib (inner-face HOG box), 0% for yolo (already
  whole-face). Explicit `--pad` wins.
- `--detect-width` is dlib-only and defaults to 0 (native).
- Refused saves are dumped to `rpi/datasets/_rejected/` as
  `NNNN_<reason>_box<x>_<y>_<w>_<h>_luma<NN>.jpg` and the reason is stamped on screen
  for 1 s. Check this directory after any capture that refuses frames.
- Two silent bugs are pinned by tests and worth remembering: ultralytics wants **BGR**
  (not RGB) and reports `xyxy` as **(x1, y1, x2, y2)** (not top/left/bottom/right).
  Either mistake returns plausible-looking boxes rather than raising.
- **`tools/capture_probe.py`** settles "nothing saved" without a GUI or a keypress: it
  assesses N frames with the same `assess_frame` and prints luma, box and verdict per
  frame. If it says `ready`, the frames are fine and the cause is the keypress — click the
  preview window once for keyboard focus. Use it before re-running an interactive capture.
- **The save block must stay inside the capture loop.** It was dedented out once, which
  made `saved` advance only after the loop ended, so the loop could never exit on
  `--count` and every `S` press was consumed and discarded (D-034).
  `rpi/tests/test_capture_loop_shape.py` asserts the AST shape; re-introducing the dedent
  fails three of its checks. Do not "tidy" that indentation.
- **The ~191 s camera-open stall is not reproducible.** A later open took 1.6 s at the
  same geometry (D-034). Measure before quoting it as a fixed cost.

## Traps

- Detection rate is **not** the gate; identification and stranger rejection are.
- `camera.device` is host enumeration order and shifts on plug/unplug.
- Device 0 (laptop built-in) reads **near-black, luma 3–7, at every geometry** — rejected on
  that measurement, not assumed broken (D-025, D-028). Re-probe rather than trusting it.
- **Close "Logitech Brio 100 1080p Camera Setup" before touching the camera.** With it open,
  device 1 intermittently returns no frames and throughput probes read a bogus ~5 fps instead
  of ~29. An early measurement in this session was wrong for exactly this reason.
- `Camera.open()` takes **~191 s on Windows** — DirectShow stalls ~64 s per geometry `set()`
  (width, then height). Expect `enroll_laptop.py` / `live_test.py` to look hung for ~3 minutes
  at startup. Deliberately not worked around in `camera.py`; untested on Linux.
- DirectShow **ignores** `CAP_PROP_FOURCC` and reports `YUY2` regardless. Do not "fix"
  `config.yaml` to YUYV from a host probe — `MJPG` is for the RPi's V4L2 path (D-024, D-028).
- PROTOCOL §4.3's 1 s trigger-to-reply has **never been measured at 1080p**. Phase 2 must
  measure it; if it overruns, the fallback is a 1080p still on trigger plus 720p for the
  burst path. See PLAN.md "the 1080p latency question".
- **The Brio 100 frames about twice as tight as the laptop built-in** (face at 61% vs 24-36% of
  the frame's short edge). **There is no software zoom** — `CAP_PROP_ZOOM`/`PAN`/`TILT`/`FOCUS`
  all read -1.0 (unsupported) and writes are inert. Move back; no config change helps (D-029).
- **A face that fills the frame silently breaks the recognition crop.** `pad_box` clips the 25%
  margin at the frame edge, so `crop_rgb` degenerates to the whole frame and the embedding is
  taken from a differently-shaped region than the gate's — the D-026 step-5 regression again.
  `enroll_laptop.py` now rejects it (`--max-face`, default 45% of the short edge) and says
  "move back". **This agent cannot view images**; if a capture still looks wrong, check the
  logged face-box size rather than trusting the preview.
