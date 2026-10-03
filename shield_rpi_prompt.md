# SHIELD Raspberry Pi 4B — Firmware + Python Prompt

Write the compute-node software for S.H.I.E.L.D on a Raspberry Pi 4B (Raspberry Pi OS 64-bit, Python 3.11) with a 720p USB webcam.

## Pipeline
1. Listen on UART (`/dev/serial0`, 115200) for ESP32 packet `0xAA | 0x01 | ts 4B BE | XOR` (7 bytes).
2. On trigger, grab a burst of 5 frames at 1280x720.
3. Run YOLOv8 (`models/face.pt`, face detector) on each frame. No face, then also try `yolov8n.pt` class 0 (person).
4. No person and no face: reply `0xBB | 0 | 0 | XOR`, stand down.
5. Face found: crop, compute embedding, compare against `known_faces/*.npy` (cosine distance).
   - Match: reply status 1 (known), log event.
   - No match, high confidence: reply status 2, send alert.
   - Person but no usable face, or low confidence: reply status 2 after the Telegram/app timeout, and ask the user "Recognise this?".
6. Reply format: `0xBB | status | confidence 0-100 | XOR` (4 bytes).

## Alerting
- POST event + snapshot to the backend (`/api/alert`) with bearer token.
- Uplink: cellular modem (SIM7600 HAT or USB LTE dongle) with Wi-Fi as primary and automatic failover. Include a `net_manager.py` that checks connectivity and switches routes.
- SMS fallback via modem AT commands if the push is not acked in 30 s.

## Structure
```
shield_rpi/
  main.py            # event loop
  uart_link.py       # packet parse/build, checksum
  camera.py          # V4L2 capture, burst grab
  detector.py        # YOLO wrapper
  recognizer.py      # embeddings + matching
  enroll.py          # capture + encode a new person into known_faces/
  alerts.py          # backend POST, SMS fallback
  net_manager.py     # Wi-Fi / cellular failover
  config.yaml        # thresholds, paths, tokens via env
  shield.service     # systemd unit, auto-start, restart on failure
  requirements.txt
```

## Requirements
- Load models once at startup. Inference only on trigger.
- Target under 1 s trigger-to-reply on RPi 4B. Use `imgsz=640`, and export to NCNN if needed.
- Structured logging to `events.jsonl`. Rotate snapshots.
- Unit tests for the packet parser.

## Deliverables
All files above, plus a README with install and enrolment steps.
