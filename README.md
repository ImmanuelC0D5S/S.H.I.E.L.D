# S.H.I.E.L.D

Intruder detection system.

- **Sensor node** — ESP32-S3-DevKitC-1, ESP-IDF (C). PIR (IO4) + LD2410 mmWave
  radar (UART1 @ 256000, presence out IO5) + LEDC buzzer (IO17, 2 kHz).
- **Sensor link** — Wi-Fi. The RPi runs the access point `shield-net`
  (`192.168.50.1:8765`); the ESP32 joins it and is a TCP client. Frames are
  length-prefixed JSON with a per-message HMAC-SHA256 tag. 2 s heartbeat both
  ways; if the link drops or a trigger goes unanswered for 5 s the ESP32 alarms
  locally. See DECISIONS D-012.
- **Compute node** — Raspberry Pi 4B, Python 3.11. 720p USB webcam, YOLOv8n face
  detector, `face_recognition` for identity.
- **Alerts** — RPi POSTs to a FastAPI backend over cellular (SIM7600/LTE, Wi-Fi or
  Ethernet primary) which pushes to an Android app over FCM.

## Layout

```
firmware/esp32/   ESP-IDF sensor-node firmware (C)
rpi/              Compute node, Python package shield_rpi
backend/          FastAPI alert service
app/              Android app (Kotlin, Compose)
docs/             PROTOCOL.md — the wire contract
tools/            Host-side dataset/enrolment/live-test harness
```

`HANDOFF.md` — current state and the exact next step.
`PLAN.md` — phases and gates.
`DECISIONS.md` — why things are the way they are.

## The one rule

Packet formats and API shapes are defined in **[`docs/PROTOCOL.md`](docs/PROTOCOL.md)**
and nowhere else. Code and tests follow that document. Change the document first,
log the reason in `DECISIONS.md`, then change every side together.

## Status

Phase 0 gate passed (protocol v2.0.0). Phase 1 in progress. Nothing verified on
hardware yet — see `HANDOFF.md`.

## Note on the input specs

`shield_*.md` in this directory are the original requirement documents, kept
unmodified for reference. Where they conflict with `docs/PROTOCOL.md` — notably the
UART sensor link and the IO43/44 pin assignments — the protocol wins.