# S.H.I.E.L.D Protocol Specification

Version: 2.0.0
Status: reviewed — Phase 0 gate passed
Supersedes: 1.0.0 (UART link replaced by Wi-Fi/TCP, see DECISIONS D-012)

**This document is the single source of truth for every wire format in S.H.I.E.L.D.**
Firmware, RPi code, backend, app and tests all follow this file. If code and this
document disagree, the document wins and the code is a bug.

---

## 1. Terminology

| Term | Meaning |
|---|---|
| Sensor node | ESP32-S3-DevKitC-1 running ESP-IDF firmware |
| Compute node | Raspberry Pi 4B running `shield_rpi`, also the Wi-Fi AP |
| Backend | FastAPI service, deployed behind the internet |
| Event | One intrusion detection, from trigger to verdict |
| Tier | Trigger confidence classification: `high` or `low` (§2.6) |

---

## 2. Sensor link — ESP32-S3 ↔ RPi (Wi-Fi)

The sensor node reaches the compute node over a **private Wi-Fi link**. The RPi
runs its own access point, so the pair works with no router, no ISP, and no
internet at all.

### 2.1 Topology

```
  ESP32-S3  ──Wi-Fi──▶  shield-net  ──Wi-Fi──▶  Raspberry Pi 4B
  (client,   192.168.50.10                192.168.50.1
   static)                                hostapd AP + shield_rpi server
                                                 │
                                                 │ cellular or Ethernet
                                                 ▼
                                             FastAPI backend → FCM → phone
```

| Property | Value |
|---|---|
| SSID | `shield-net` |
| Band | 2.4 GHz |
| Security | WPA3-PSK, key `SHIELD_WIFI_PSK` from `.env` / Kconfig |
| RPi AP address | `192.168.50.1` (static) |
| ESP32 address | `192.168.50.10` (static on the ESP32 side) |
| Transport | TCP, ESP32 is the client |
| ESP32 → RPi port | `8765/tcp` |
| RPi server bind | `0.0.0.0:8765` |

The uplink to the backend is a **separate** interface: cellular (SIM7600/LTE) or
Ethernet. The AP interface carries sensor traffic only. `shield-net` has no
internet path; a client on it cannot reach the backend.

**UART2 (IO43/IO44) is no longer used for the RPi link.** Those pins are free.
ESP32 console logging still uses USB-Serial-JTAG.

### 2.2 Transport choice: raw TCP, not MQTT

The link carries at most a few messages per second: a heartbeat every 2 s in each
direction, plus one request/response exchange per intrusion event. Raw TCP with a
length-prefixed frame is sufficient.

MQTT was rejected: it adds a broker process to supervise on the RPi, a client
library and a broker config on top of what is otherwise a `socket.accept()` and a
length check. The added surface — broker liveness, retained messages, `$share`
groups, reconnect semantics — buys topic routing and pub/sub fan-out that a
two-node point-to-point link has no use for. Topic routing could be faked with a
`type` field in ~5 lines. If a third node ever joins the link, revisit.

`mqtt` is not a dependency. `esp_netif` / `esp_tls` (only for TLS, which this
link does not use) and lwIP sockets are all the ESP32 needs.

### 2.3 Framing

Each message is one frame:

```
+--------+--------+------------------+--------+
| len(4) | flags  |   body (len B)   | tag(16) |
+--------+--------+------------------+--------+
   uint32   uint8     JSON, UTF-8      16 B
   big-end.
```

| Field | Size | Description |
|---|---|---|
| `len` | 4 | Body length in bytes, big-endian uint32. Max 8192. |
| `flags` | 1 | Bit 0 = JSON body (must be set). Bits 1–7 reserved, must be 0. |
| `body` | `len` | UTF-8 JSON object (§2.4). No trailing newline. |
| `tag` | 16 | HMAC-SHA256(§2.5), truncated to the first 16 bytes. |

A body larger than 8192 bytes is a protocol violation: drop the connection.

A `len` larger than 8192, or a `len` that would not complete within the read
timeout, means the stream is desynchronised. The receiver must **not** try to
resynchronise a TCP stream by scanning for magic bytes — there are none. It closes
the connection and the sender reconnects (§2.7).

### 2.4 Messages

Every message is a JSON object with these common fields:

| Field | Type | Description |
|---|---|---|
| `v` | integer | Protocol version. Always `2` in this document. |
| `type` | string | Message type, see below. |
| `id` | string | Message id, hex, 8 chars, unique per sender per boot. |
| `ts_ms` | integer | Sender monotonic milliseconds. Not wall-clock. |
| `sent_at` | string | Sender ISO-8601 UTC wall clock, for the event log only. |

| `type` | Direction | Purpose |
|---|---|---|
| `trigger` | ESP32 → RPi | Motion detected, run the pipeline |
| `reply` | RPi → ESP32 | Verdict |
| `heartbeat` | both | Liveness (§2.8) |
| `heartbeat_ack` | RPi → ESP32 | Liveness reply |

#### 2.4.1 `trigger` (ESP32 → RPi)

```json
{
  "v": 2,
  "type": "trigger",
  "id": "a3f19c02",
  "ts_ms": 1234567,
  "sent_at": "2026-10-01T09:14:03.512Z",
  "tier": "high",
  "sensors": ["pir", "mmwave"]
}
```

| Field | Type | Description |
|---|---|---|
| `tier` | string | `high` or `low` (§2.6). |
| `sensors` | array of string | Which sensors fired: `pir`, `mmwave`. |

#### 2.4.2 `reply` (RPi → ESP32)

```json
{
  "v": 2,
  "type": "reply",
  "id": "b7c40e11",
  "ts_ms": 550213,
  "sent_at": "2026-10-01T09:14:04.043Z",
  "trigger_id": "a3f19c02",
  "status": 2,
  "confidence": 82
}
```

| Field | Type | Description |
|---|---|---|
| `trigger_id` | string | `id` of the `trigger` this answers. |
| `status` | integer | 0, 1 or 2 (§2.4.3). |
| `confidence` | integer | 0–100. |

#### 2.4.3 `reply.status`

| Value | Name | Meaning | ESP32 action |
|---|---|---|---|
| 0 | `CLEAR` | No person in view, stand down | Silent, re-arm |
| 1 | `KNOWN` | Face matched an enrolled identity | Silent, re-arm |
| 2 | `ALERT` | Unknown person, or unusable/low-confidence face | **Buzzer alarm for 10 s** |

Mapping from RPi verdict to status:

| RPi verdict | `status` | Backend `status` |
|---|---|---|
| No person, no face | 0 | — (no alert) |
| Face matched an enrolled identity | 1 | — (no alert) |
| Face found, no match, confidence ≥ `alert_confidence` | 2 | `unknown` |
| Person but no usable face, or confidence < `alert_confidence` | 2 | `low_conf` |

The last two cases share `status: 2` but are distinguished by the backend
`status` string. The ESP32 does not need to tell them apart: both alarm.

`confidence` is advisory. The ESP32's behaviour is driven by `status` alone.

### 2.5 Authentication and integrity: HMAC

Every frame carries a truncated HMAC-SHA256 tag over the exact bytes that
precede it:

```
tag = HMAC_SHA256(key = SHIELD_LINK_KEY, msg = len || flags || body)[0:16]
```

- `SHIELD_LINK_KEY` is a shared secret. On the RPi it comes from `.env`; on the
  ESP32 from Kconfig (`CONFIG_SHIELD_LINK_KEY`) and `sdkconfig.defaults`. Minimum
  32 bytes of entropy. It is never logged, never in the AP PSK, and never the same
  value as `SHIELD_API_TOKEN`.
- Verification must be **constant-time**. A byte-by-byte `==` comparison leaks
  timing; use a constant-time compare on both sides.
- A frame whose tag does not verify is dropped and logged at WARNING. It is not
  answered. Three tag failures on one connection close it (§2.7).
- Tag check happens **before** JSON parse. An unauthenticated body is never
  parsed, so a malformed or hostile body cannot reach the JSON decoder.

Why HMAC rather than the XOR checksum used by v1.0.0: XOR is an error-detection
code. It catches corruption, but anyone on `shield-net` can forge a frame by
recomputing it, and an `ALERT` reply or a `trigger` can be injected or suppressed.
HMAC gives forgery resistance. `shield-net` is WPA3-PSK, which authenticates the
link itself, but HMAC is defence in depth: it keeps the link trustworthy if the
PSK leaks, and it means a compromised host on the AP still cannot forge sensor
traffic without the key.

### 2.6 Trigger tier

`tier` is `high` when PIR **and** mmWave presence both occur within 2 s of each
other. It is `low` when either alone fires. A `low` trigger that turns out to be
a known face still produces `status: 1` — tier never causes an alarm by itself.
Tier is metadata for the event record and for tuning the sensor thresholds.

### 2.7 Connection lifecycle

**RPi server** (`shield_rpi/uart_link.py`'s successor, `shield_rpi/link.py`):

1. Listen on `0.0.0.0:8765`. One sensor node is expected.
2. Accept a connection. A second concurrent connection is rejected and closed.
3. Read frames in a loop. Per-frame read timeout: `CONFIG_SHIELD_READ_TIMEOUT_S`,
   default 5 s.
4. On a valid frame: dispatch, respond if the type expects a reply.
5. On `len` > 8192, an incomplete frame at timeout, or a tag failure: count the
   fault. At 3 faults on a connection, close it.
6. Loop back to 2. A dropped connection is normal (RPi reboot, AP restart) and
   must never take the server down.

**ESP32 client:**

1. Join `shield-net`, then connect to `192.168.50.1:8765`.
2. Send `heartbeat` every 2 s (§2.8).
3. On send failure or a closed connection: reconnect with backoff
   (500 ms → 1 s → 2 s → 5 s, capped at 5 s), forever. Never give up.

### 2.8 Heartbeat and liveness

| Constant | Value | Owner |
|---|---|---|
| Heartbeat interval | 2 s | ESP32 |
| `heartbeat_ack` reply | immediate | RPi |
| Missing-heartbeat threshold | 6 s (3 missed) | both |
| Reply timeout | 5 s | ESP32 |

**RPi side.** If no frame at all has arrived for 6 s, the sensor is considered
down. The RPi logs WARNING once per transition, writes a `link_down` record to
`events.jsonl`, and does not process triggers. The pipeline is never left in a
half-open state.

**ESP32 side — fail safe.** If the link drops, or a `trigger` goes unanswered for
5 s, the ESP32 alarms locally and keeps retrying:

1. Link down, or no `reply` within 5 s of a `trigger`.
2. Sound the buzzer for 10 s. Log `local_fail_safe_alarm` with the cause.
3. Retry the `trigger` every 5 s while the link is up, up to 3 times.
4. Keep retrying the connection indefinitely. A later `reply` still updates the
   event log; the alarm is not cancelled, because by then the intruder may have
   left unobserved.

**Consequence to accept:** the RPi rebooting takes `shield-net` down, so the
ESP32 alarms. An RPi reboot is therefore audible. Tracked as an open item in
`PLAN.md`; the fix (a maintenance flag or a link-down grace window) is Phase 9.

### 2.9 Timing and safety constants

| Constant | Default | Owner | Notes |
|---|---|---|---|
| PIR/radar debounce | 200 ms | ESP32 | Per-sensor |
| Both-sensors window | 2 s | ESP32 | Both inside ⇒ `tier: high` |
| Reply timeout | 5 s | ESP32 | No reply ⇒ retry, then local alarm |
| Trigger retries | 3 | ESP32 | Then keep reconnecting |
| Cooldown | 10 s | ESP32 | After each event |
| Alarm duration | 10 s | ESP32 | On status 2 or fail safe |
| Buzzer frequency | 2 kHz | ESP32 | LEDC PWM |
| Radar baud | 256000 | ESP32 | LD2410 on UART1 — unchanged, not the sensor link |
| Frame timeout | 5 s | RPi | Per-frame read timeout |
| Max frame body | 8192 B | both | |

**Fail-safe rule.** Absence of information is treated as a threat: no link, no
reply, or a dead RPi must end in an alarm, never in silence.

### 2.10 Host-side testing

The link has no UART, so there is no loopback wire test. Its tests are host-side
Python socket tests in `rpi/tests/test_link.py`:

- Frame encode/decode round trip, including `len`, `flags` and the tag.
- Tag verification rejects a single flipped bit in `body`, `len` or `flags`.
- Constant-time compare used (tested by construction, not by timing).
- Oversized `len` closes the connection.
- A server and client on `127.0.0.1` exercise the real state machine: trigger →
  reply, duplicate trigger → same reply, dropped mid-frame → server survives,
  three tag failures → connection closed.
- Heartbeat and heartbeat_ack cadence.

These run on the host with no hardware. See DECISIONS D-013.

---

## 3. Backend HTTP API

Base path: `/api`. All requests and responses are `application/json` unless
stated otherwise.

The backend is reached over cellular or Ethernet only. The sensor link (§2) never
carries backend traffic.

### 3.1 Authentication

Every `/api/**` route except `GET /healthz` requires:

```
Authorization: Bearer <SHIELD_API_TOKEN>
```

A missing or wrong token returns `401`. The RPi and the Android app send the same
shared token; it lives in `.env` on the RPi and in `EncryptedSharedPreferences` on
the phone. Snapshots (§3.6) are the exception: they are authorised by their
signature, not by the bearer token, because they are fetched by the image loader.

### 3.2 `GET /healthz`

Liveness probe. No auth.

`200`

```json
{ "status": "ok", "version": "2.0.0" }
```

### 3.3 `POST /api/alert`

Called by the RPi for every `ALERT` verdict, immediately after the `reply` is sent.
**Content type is `multipart/form-data`.**

| Part | Content-Type | Required | Description |
|---|---|---|---|
| `event` | `application/json` | yes | Event record, §3.3.1 |
| `snapshot` | `image/jpeg` | no | Full frame, max 512 KB |

Rationale for multipart: a 720p JPEG is 100–250 KB and base64-inflates by a third;
JSON would force a large body into memory on both sides.

##### 3.3.1 `event` part

| Field | Type | Required | Description |
|---|---|---|---|
| `event_id` | string (UUID4) | yes | Client-generated. Makes retries idempotent. |
| `device_id` | string | yes | e.g. `shield-01` |
| `timestamp` | number | yes | Unix seconds, UTC, fractional allowed |
| `uptime_ms` | integer | no | ESP32 boot-relative ms from the `trigger` |
| `status` | string | yes | `unknown` or `low_conf` |
| `confidence` | integer 0–100 | yes | Detector confidence |
| `trigger` | string | no | `high` or `low`, from `trigger.tier` (§2.6) |
| `sensors` | array of string | no | `["pir", "mmwave"]` |
| `person_detected` | boolean | yes | |
| `face_detected` | boolean | yes | |
| `identity` | string or null | no | Matched enrolled name, else `null` |
| `latency_ms` | integer | no | RPi trigger-to-reply time |

`status` values, and the `reply.status` they pair with:

| `status` | `reply.status` | Meaning |
|---|---|---|
| `unknown` | 2 | Face present, no match. Push now. |
| `low_conf` | 2 | Person present, face unusable or confidence too low. Push, and ask the user "Recognise this?". |

`201` (created) or `200` (duplicate `event_id` already stored — treated as
success so RPi retries are safe):

```json
{
  "event_id": "3f2c...-...",
  "snapshot_url": "https://shield.example/api/snapshots/3f2c...?exp=1735689600&sig=9a1c...",
  "snapshot_url_expires": 1735689600,
  "push_sent": true
}
```

Errors: `400` malformed, `401` bad token, `413` snapshot over 512 KB,
`422` schema violation.

##### 3.3.2 Retry semantics

The RPi retries `POST /api/alert` up to 3 times with exponential backoff
(2 s, 4 s, 8 s) and the **same** `event_id`. The backend must not create a
second event for a repeated `event_id`.

### 3.4 `GET /api/events`

History, newest first. Used by the app's History screen.

| Query param | Default | Description |
|---|---|---|
| `limit` | 50 | 1–200 |
| `before` | — | Event id cursor; return events older than this |

`200`

```json
{
  "events": [ { "...": "EventOut" } ],
  "next_before": "8a71..."
}
```

`EventOut` is the `event` schema plus:

| Field | Type | Description |
|---|---|---|
| `event_id` | string | |
| `verdict` | string or null | `known`, `intruder`, or `null` |
| `verdict_at` | number or null | Unix seconds |
| `snapshot_url` | string or null | Signed URL, expires after 1 h |
| `snapshot_url_expires` | number or null | Unix seconds |

### 3.5 `POST /api/events/{event_id}/verdict`

Called by the app when the user taps `Known` or `Intruder`.

```json
{ "verdict": "known", "source": "app" }
```

`verdict` is `known` or `intruder`. `source` is `app` for now.

`200` returns `EventOut` with `verdict` and `verdict_at` filled in.

Errors: `401`, `404` unknown `event_id`, `422` bad verdict.

The verdict is relayed to the RPi by polling — see §3.7.

### 3.6 Snapshots

```
GET /api/snapshots/{event_id}?exp=<unix>&sig=<hex>
```

`sig = HMAC_SHA256(SNAPSHOT_SIGNING_KEY, f"{event_id}:{exp}")`, truncated to 32
hex characters. `exp` must be in the future; URLs are valid for 1 h.

`200` `image/jpeg` · `403` bad or expired signature · `404` no snapshot stored.

### 3.7 RPi polling: ack, verdict, SMS fallback

**Polling is event-scoped, not continuous.** The RPi polls only while an event is
pending, and stops as soon as the event is resolved. There is no background poll
loop when nothing is happening — an idle sensor node costs zero uplink data.

Polling starts immediately after `POST /api/alert` succeeds, every
`poll.interval_s` (default 5 s), up to the 30 s ack deadline.

```
GET /api/events/{event_id}/status
```

`200`

```json
{
  "event_id": "3f2c...",
  "resolved": false,
  "acked": true,
  "acked_at": 1735689312,
  "sms_required": false,
  "verdict": null,
  "verdict_at": null
}
```

| Field | Type | Description |
|---|---|---|
| `resolved` | boolean | Nothing left to poll for. See the stop condition below. |
| `acked` | boolean | The app received and acknowledged the push |
| `acked_at` | number or null | Unix seconds |
| `sms_required` | boolean | Push unacked at the 30 s deadline ⇒ send SMS |
| `verdict` | string or null | `known` or `intruder`, once the user has answered |
| `verdict_at` | number or null | Unix seconds |

Flow:

1. RPi posts the alert. Backend pushes via FCM.
2. The app shows a full-screen alert and calls `POST /api/events/{id}/ack`
   when the screen is displayed.
3. If `acked` is still false at 30 s, `sms_required` flips true and the RPi
   sends an SMS through the cellular modem (§5.3).
4. When the user taps a verdict button the app calls
   `POST /api/events/{id}/verdict`; the RPi's next poll observes `verdict`.

**Stop condition.** The RPi stops polling when `resolved` is true, when
`verdict != null`, or after 10 minutes, whichever comes first. One poll 5 s after
the verdict arrives is enough to observe it; there is no reason to keep asking.
The backend sets `resolved: true` once `acked && verdict != null && !sms_required`.

##### 3.7.1 `POST /api/events/{event_id}/ack`

Called by the app. Body `{}` or `{"source": "app"}`. `200` returns the same
object as §3.7.

### 3.8 `GET /api/events/{id}/snapshot_url`

Refreshes an expired signed URL. Auth: bearer token.

---

## 4. RPi event pipeline

### 4.1 Steps

1. Valid `trigger` received (§2.7).
2. Grab a burst of 5 frames at 1920×1080 (D-028; was 1280×720). Both are 16:9,
   so the detector input is unchanged at `imgsz=640`.
3. Run the face detector on each frame (`imgsz=640`).
4. If no face was found in any frame, fall back to the person detector
   (class 0 of the person model).
5. No person and no face ⇒ `status: 0`.
6. Face found ⇒ crop, compute the embedding, cosine-compare against
   `known_faces/*.npy`.
7. Match ⇒ `status: 1`.
8. No match above the threshold ⇒ `status: 2` / `unknown`.
9. Person but no usable face, or confidence below the threshold ⇒
   `status: 2` / `low_conf`.

### 4.2 Reply-before-alert ordering

The `reply` is sent **before** the network alert. The alarm must not wait on the
internet: reply first, then POST. If the POST fails after 3 attempts the event is
queued to `events.jsonl` and retried on the next successful uplink; the ESP32 is
never left waiting.

### 4.3 Latency budget

Trigger-to-reply under 1 s on a Raspberry Pi 4B at `imgsz=640`. Measured and
reported as `latency_ms` in the alert. If sustained latency exceeds 1 s, export
the detector to NCNN before considering a smaller model.

**The 1080p figure is unverified.** 1280×720 was measured on hardware; 1920×1080
has not, because the host probe (§D-028) shows the Brio 100 sustaining only
~5 fps with a ~215 ms worst-case read *on the laptop, over DirectShow, handing
back uncompressed YUY2*. At 5 fps a 5-frame burst costs ~1.0 s on its own, which
would consume the entire budget before any inference runs. That number is not
evidence about the RPi — the Pi is a USB 2 host with a Linux V4L2 path where
MJPG may actually engage — but it makes the 1080p budget genuinely open.

Phase 2 must therefore **measure** trigger-to-reply at 1920×1080 on the Pi and
record the result here. If it exceeds 1 s, the fallback is: capture a single
1920×1080 still on trigger for the snapshot, and keep the continuous/burst path
at 1280×720. Do not assume 1080p end-to-end; do not assume it is impossible
either. Measure it.

---

## 5. Cellular / uplink

### 5.1 Route selection

Wi-Fi or Ethernet is primary where available, cellular is the failover.
`net_manager.py` probes connectivity and switches routes. The active route is
recorded in the event log.

The sensor AP (`shield-net`, §2.1) is **not** an uplink route. It carries no
internet path.

### 5.2 Modem

SIM7600 HAT or USB LTE dongle, driven with AT commands on a serial port. Pins,
port and baud are configuration, not constants.

### 5.3 SMS fallback

Triggered when `sms_required` is true (§3.7). The message must contain at
minimum the event time and the `event_id` short form so the user can match it to
the app.

---

## 6. LD2410 mmWave radar frame (ESP32 side)

The ESP32 parses LD2410 presence frames on UART1 at 256000 baud. LD2410 is a
third-party protocol owned by the vendor; it is summarised here only because the
parser depends on it. Presence output is a fixed 23-byte frame:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | header `0xAA 0xAA` |
| 2 | 2 | length, LE |
| 4 | 1 | `0x02` (presence target) |
| 5 | 1 | target state, `0x01` = present |
| 6 | 2 | moving target distance, LE, cm |
| 8 | 2 | stationary target distance, LE, cm |
| 10 | 13 | reserved |
| 23 | 1 | checksum, XOR of the preceding 22 bytes |

Total 23 bytes including the checksum. Values `0xFFFF` mean "no target".

This is the only remaining fixed binary frame in the system, and it is
vendor-defined. The sensor link (§2) no longer uses binary framing at all.

---

## 7. Configuration keys

Defaults live in `rpi/shield_rpi/config.yaml` and `firmware/esp32/sdkconfig.defaults`.
Secrets live only in `.env` (gitignored; `.env.example` is tracked) and in
Kconfig, never in source.

| Key | Default | Where |
|---|---|---|
| `link.ssid` | `shield-net` | ESP32 Kconfig, RPi hostapd |
| `link.psk` | — | `.env` / Kconfig only |
| `link.host` | `192.168.50.1` | ESP32 |
| `link.port` | `8765` | both |
| `link.hmac_key` | — | `.env` / Kconfig only |
| `link.heartbeat_s` | `2` | ESP32 |
| `link.reply_timeout_s` | `5` | ESP32 |
| `link.frame_timeout_s` | `5` | RPi |
| `link.max_body` | `8192` | both |
| `camera.device` | `/dev/video0` | RPi |
| `camera.width` | `1920` | RPi |
| `camera.height` | `1080` | RPi |
| `camera.burst_frames` | `5` | RPi |
| `detector.imgsz` | `640` | RPi |
| `detector.conf` | `0.5` | RPi |
| `recognizer.match_distance` | `0.5` | RPi |
| `recognizer.alert_confidence` | `50` | RPi |
| `backend.url` | — | RPi (`.env`) |
| `backend.token` | — | `.env` only |
| `poll.interval_s` | `5` | RPi |
| `poll.ack_deadline_s` | `30` | RPi |

ESP32 Kconfig additions for Phase 4:

| Symbol | Default |
|---|---|
| `CONFIG_SHIELD_WIFI_SSID` | `shield-net` |
| `CONFIG_SHIELD_WIFI_PSK` | — |
| `CONFIG_SHIELD_LINK_HOST` | `192.168.50.1` |
| `CONFIG_SHIELD_LINK_PORT` | `8765` |
| `CONFIG_SHIELD_LINK_KEY` | — |

---

## 8. Change control

Any change to §2 or §3 is an architecture change: update this document first, add
a `DECISIONS.md` entry with the date and reason, then update firmware, RPi, backend
and app together. A one-sided change breaks the system silently.