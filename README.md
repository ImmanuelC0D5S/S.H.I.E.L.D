# S.H.I.E.L.D

Intruder detection system.

- **Sensor node** — ESP32-S3-DevKitC-1, Arduino-ESP32, **one sketch**
  (`firmware/esp32/shield_esp32/shield_esp32.ino`). PIR (IO4) + LD2410 mmWave
  radar (UART1 @ 256000, presence out IO5, compiled out by default) + LEDC buzzer
  (IO17, 2 kHz). Compiles; has never run on a board.
- **Sensor link** — Wi-Fi. The RPi runs the access point `shield-net`
  (`192.168.50.1:8765`); the ESP32 joins it and is a TCP client. Frames are
  length-prefixed JSON with a per-message HMAC-SHA256 tag. 2 s heartbeat both
  ways; if the link drops or a trigger goes unanswered for 5 s the ESP32 alarms
  locally. See DECISIONS D-012, D-042.
- **Compute node** — Raspberry Pi 4B, Python 3.11. 1920×1080 USB webcam, YOLOv8n
  face detector, `face_recognition` for identity.
- **Alerts** — **there is no server.** The RPi uploads the snapshot to a private
  Supabase Storage bucket and inserts one `events` row. A `pg_net` trigger calls a
  `send-alert` Supabase Edge Function, which pushes to an Android app over FCM.
  Uplink is cellular (SIM7600/LTE), Wi-Fi or Ethernet. The Firebase admin key
  lives only in Supabase function secrets — never on the Pi. See DECISIONS D-040,
  D-044.

## Layout

```
firmware/esp32/   Arduino-ESP32 sensor-node firmware — shield_esp32.ino (one file)
rpi/              Compute node, Python package shield_rpi
supabase/         migrations/ + send-alert Edge Function
backend/          EMPTY and abandoned (D-040). A test enforces it stays empty.
app/              Android app (Kotlin, Compose)
docs/             PROTOCOL.md — the wire contract, v2.2.0
deploy/           systemd unit for the compute node
tools/            Host-side dataset/enrolment/live-test harness + ops scripts
```

`HANDOFF.md` — current state and the exact next step.
`PLAN.md` — phases and gates.
`DECISIONS.md` — why things are the way they are.

## The one rule

Packet formats and API shapes are defined in **[`docs/PROTOCOL.md`](docs/PROTOCOL.md)**
and nowhere else. Code and tests follow that document. Change the document first,
log the reason in `DECISIONS.md`, then change every side together.

## Status

Phase 0 gate passed. Phase 1 in progress. **Phases 2, 3, 6 and 7 have code that
compiles and passes unit tests, but their gates are not passed** — nothing has run
on a Pi, on an ESP32, or on a phone. Phase 4 (ESP32 firmware) is written and
compiles for `esp32s3`, and has never been flashed. See `HANDOFF.md`.

373 Python tests and 13 Kotlin unit tests pass; `ruff check .` is clean.

## Note on the input specs

`shield-*.md` in this directory are the original requirement documents, kept
unmodified for reference. Where they conflict with `docs/PROTOCOL.md` the
protocol wins. Three conflicts were resolved rather than left open:

| Spec says | Protocol says | Decided |
|---|---|---|
| ESP32 ↔ RPi over HTTP `POST /trigger` | framed TCP + HMAC | protocol wins (D-042) |
| RPi `POST /alarm` to the ESP32 | framed TCP + HMAC | protocol wins; `alarm`/`alarm_ack` added to §2.4.4 (D-042) |
| RPi POSTs to a FastAPI backend | Supabase + Edge Function | Supabase wins (D-040) |

`shield_rpi/http_shim.py` serves a **loopback-only** `POST /trigger` so the
`curl`-driven done-when test still runs without the ESP32 present. It is bound to
`127.0.0.1` and refuses any other interface.

## Running it

```powershell
# Tests
$env:PYTHONPATH="rpi;tools"
.\rpi\.venv\Scripts\python.exe -m pytest -o addopts="--strict-markers"
python -m ruff check .

# Android (JAVA_HOME must be the Android Studio JBR; `java` on PATH is 1.8)
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
cd app; .\gradlew.bat :app:assembleDebug

# Compute node, laptop, no hardware
python -m shield_rpi.service --shim --fake-trigger
curl -X POST localhost:8811/trigger -d '{}'
```

Supabase setup is in `docs/PROTOCOL.md` §3.10. It is four steps and two of them
are manual.