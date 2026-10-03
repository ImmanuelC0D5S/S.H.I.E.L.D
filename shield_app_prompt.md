# SHIELD Alert App — Prompt

Build an Android app (Kotlin, Jetpack Compose, min SDK 26) that receives intruder alerts from the S.H.I.E.L.D system over cellular data.

## Flow
- RPi 4B detects an intruder, then POSTs to a small backend. The backend sends an FCM push to the phone.
- Push payload: `event_id`, `timestamp`, `status` (`unknown` | `low_conf`), `confidence` (0-100), `snapshot_url`.
- Works on mobile data only. No same-network assumption.

## Screens
1. **Alert** (full-screen intent, even on lock screen): snapshot, time, confidence, buttons `Known` / `Intruder`. Button tap calls `POST /api/events/{id}/verdict`.
2. **History**: list of past events with thumbnail, time, verdict.
3. **Settings**: backend URL, API token, enrolled-device name, test-alert button.

## Requirements
- FCM high-priority notifications, with a dedicated alert channel (loud sound, bypass DND if permitted).
- Foreground-safe handling when the app is killed.
- Retrofit + OkHttp, Coil for images, Room for local history.
- Bearer token auth on every request. Token stored in EncryptedSharedPreferences.
- SMS fallback: if the push is not acked in 30 s, backend triggers an SMS via the RPi cellular modem.

## Backend (minimal)
- FastAPI, endpoints: `POST /api/alert` (from RPi), `POST /api/events/{id}/verdict`, `GET /api/events`.
- Stores snapshots on disk, serves via signed URLs.
- Verdict is relayed back to the RPi (poll or webhook).

## Deliverables
Project structure, all Kotlin files, `AndroidManifest.xml`, Gradle files, FastAPI `main.py`, and a README with FCM setup steps.
