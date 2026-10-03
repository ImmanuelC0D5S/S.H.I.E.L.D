# S.H.I.E.L.D — Agent Kickstart

You are building S.H.I.E.L.D: an intruder-detection system. Read this fully, then follow it.

## System
- **Sensor node:** ESP32-S3-DevKitC-1 (ESP-IDF, C). PIR + mmWave radar (LD2410, UART1) + buzzer. Talks to RPi over UART.
- **Compute node:** Raspberry Pi 4B (Python 3.11). 720p USB webcam. YOLOv8n face detector + `face_recognition` for identity.
- **Alerts:** Cellular uplink (SIM7600 / LTE dongle, Wi-Fi primary) to a FastAPI backend, then FCM push to an Android app.
- Detailed specs live in: `shield_esp32_prompt.md`, `shield_rpi_prompt.md`, `shield_app_prompt.md`, `shield_step1_yolo_face.md`. Treat them as requirements.

## Repo layout
```
firmware/esp32/   rpi/   backend/   app/   docs/   tools/
HANDOFF.md  PLAN.md  DECISIONS.md  README.md
```

## Session protocol (mandatory)
1. **Start:** read `HANDOFF.md`, then `PLAN.md`. Do not re-ask what they already answer.
2. **Work:** one phase at a time, in order below.
3. **End:** overwrite `HANDOFF.md` with: done, in progress, next step (exact command/file), blockers, how to run/test. Keep under 40 lines.
4. Log any non-obvious choice in `DECISIONS.md` (date, decision, reason). Never silently change the architecture.

## Phase order
Each phase has a **gate**. Do not start the next phase until the gate passes and is recorded in `PLAN.md`.

| # | Phase | Gate |
|---|---|---|
| 0 | Repo scaffold, `PLAN.md`, `docs/PROTOCOL.md` (UART packets + backend API) | Protocol doc reviewed |
| 1 | YOLO face detector on laptop + 720p webcam, enroll, recognize | Me = named, stranger = unknown |
| 2 | RPi modules: `camera`, `detector`, `recognizer`, `enroll` | Same result on RPi 4B, under 1 s |
| 3 | `uart_link` (both sides) with host unit tests, loopback test | Tests pass, bad checksum rejected |
| 4 | ESP32: PIR, radar parser, buzzer, trigger logic | Serial log shows correct events |
| 5 | ESP32 and RPi integration over real UART | PIR then RPi reply then buzzer works end to end |
| 6 | Backend (FastAPI) + `alerts.py` from RPi | Test alert + snapshot stored |
| 7 | Android app: FCM, alert screen, verdict buttons | Phone gets alert on mobile data |
| 8 | Cellular modem, failover, SMS fallback | Alert arrives with Wi-Fi unplugged |
| 9 | Hardening: systemd, log rotation, watchdogs, README | Cold boot runs unattended |

## Coding practices
- **Single source of truth:** packet formats and API shapes defined only in `docs/PROTOCOL.md`. Code and tests follow it.
- **Small steps:** commit per logical change, conventional commits (`feat:`, `fix:`, `docs:`, `test:`). Never commit broken builds.
- **Test first for logic:** packet parsing, checksum, decision logic get unit tests that run on the host (no hardware).
- **Config, not constants:** pins, thresholds, timeouts, paths in Kconfig / `config.yaml`. Secrets only in `.env` (gitignored, with `.env.example`).
- **Fail safe:** no reply from RPi means alarm. Bad packet means drop and log. Never crash on I/O errors.
- **Minimal edits:** change only what the task needs. No drive-by refactors or renames.
- **Dependencies:** pin versions. Justify any new dependency in `DECISIONS.md`.
- **Typing and style:** Python with type hints and `ruff`. C with `-Wall -Wextra`, no dynamic allocation in ISRs.
- **Logging:** structured, leveled. No `print` in committed code.
- **Hardware honesty:** if something cannot be verified without the device, say so in `HANDOFF.md`. Do not claim it works.

## Rules of engagement
- If a requirement is ambiguous or hardware-dependent, ask one focused question, then continue with other work.
- Do not skip a gate. Do not build ahead.
- Keep replies short: what changed, what was tested, what is next.

## First actions
1. Create the repo scaffold and the three tracking docs.
2. Write `docs/PROTOCOL.md` from the existing prompts.
3. Start Phase 1.
