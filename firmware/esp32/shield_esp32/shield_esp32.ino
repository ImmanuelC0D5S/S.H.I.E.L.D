/*
 * shield_esp32.ino -- S.H.I.E.L.D sensor node (ESP32-S3-DevKitC-1)
 *
 * One file, one job: PIR motion -> HMAC-framed TCP trigger to the Raspberry Pi
 * -> act on the Pi's answer -> sound the buzzer when told to.
 *
 * The wire format is docs/PROTOCOL.md section 2 and is not redefined here:
 *
 *   +--------+--------+------------------+--------+
 *   | len(4) | flags  |   body (len B)   | tag(16) |
 *   +--------+--------+------------------+--------+
 *      uint32   uint8     JSON, UTF-8      16 B
 *     big-end.
 *
 *   tag = HMAC_SHA256(SHIELD_HMAC_KEY, len || flags || body)[0:16]   (2.5)
 *
 * Messages this node sends:  trigger, heartbeat, alarm_ack.
 * Messages it handles:       reply, heartbeat_ack, alarm, silence.
 * (`health` does not exist in PROTOCOL.md 2.4 and nothing sends it; an unknown
 *  type is logged and ignored, which is what shield_rpi/service.py does too.)
 *
 * Hardware (README.md):
 *   IO4   HC-SR501 PIR, rising-edge interrupt. The ISR only sets a flag.
 *   IO17  passive buzzer, LEDC PWM, nominal 2 kHz.
 *   IO16  LD2410 TX (optional, compiled out unless USE_LD2410=1).
 *
 * Build, host-verified only -- no board has run this yet:
 *   arduino-cli compile --fqbn esp32:esp32:esp32s3 \
 *       --board-options CDCOnBoot=cdc firmware/esp32/shield_esp32
 * Arduino IDE: open this file, board = "ESP32S3 Dev Module", USB CDC On Boot
 * enabled (otherwise the 115200 log never reaches the console).
 *
 * Secrets live in secrets.h (gitignored). Copy secrets.h.example first.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <stdarg.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_random.h"
#include "mbedtls/md.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "secrets.h not found. Copy secrets.h.example to secrets.h and fill it in. It is gitignored on purpose."
#endif

// The LD2410 is optional and off until everything else is proven on the bench
// (shield-esp32-firmware-pass.md). Override with -DUSE_LD2410=1.
#ifndef USE_LD2410
#define USE_LD2410 0
#endif

// UART demo mode (shield-serial-bridge-prompt.md). **Default 0: the deployment
// target is wireless.** Set -DSERIAL_DEMO=1 to talk to a laptop or a Pi over
// plain serial lines instead of the HMAC link.
//
// The demo path sends `MOTION` out over USB serial and accepts `ALERT` / `SAFE`
// back. It has NO HMAC, NO framing and NO authentication, so anything that can
// write to the port can synthesise a trigger or suppress the buzzer. It is a
// bench link between two boards. See PROTOCOL.md 9 - do not point it at
// anything reachable from a network.
//
// Build note: a SERIAL_DEMO=1 build compiles clean apart from "defined but not
// used" warnings for the Wi-Fi and link functions it deliberately skips. They
// are left compiled rather than wrapped in #if because guarding them cascades -
// it orphans the link state variables and produces strictly more warnings, for a
// configuration nobody ships. The default build is 0 errors, 0 warnings.
#ifndef SERIAL_DEMO
#define SERIAL_DEMO 0
#endif

// ---------------------------------------------------------------------------
// Pins
// ---------------------------------------------------------------------------
static const uint8_t PIR_PIN = 4;     // HC-SR501 OUT
static const uint8_t BUZZER_PIN = 17; // passive buzzer

#if USE_LD2410
static const uint8_t LD2410_RX_PIN = 16;              // LD2410 TX -> IO16
static const uint8_t LD2410_TX_PIN = 18;              // node never configures the radar
static const uint32_t LD2410_BAUD = 256000;           // PROTOCOL.md 2.9
static const uint32_t RADAR_WINDOW_MS = 2000;         // PROTOCOL.md 2.6
#endif

// ---------------------------------------------------------------------------
// Protocol constants (PROTOCOL.md 2.3 / 2.5 / 2.7 / 2.8 / 2.9)
// ---------------------------------------------------------------------------
static const uint8_t PROTO_VERSION = 2;
static const uint8_t FLAG_JSON = 0x01;
static const size_t MAX_BODY = 8192;
static const size_t TAG_BYTES = 16;
static const size_t HEADER_BYTES = 5;
static const uint8_t FAULT_LIMIT = 3;

// Our own frames are small; the receive buffer still honours the 8192 B cap so
// an oversized len is caught rather than allocated (PROTOCOL.md 2.3).
static const size_t MAX_TX_BODY = 768;

// -- timing -----------------------------------------------------------------
static const uint32_t WARMUP_MS = 10000;      // no trigger for 10 s after boot
static const uint32_t COOLDOWN_MS = 5000;     // see D-047: PROTOCOL.md 2.9 says 10 s
static const uint32_t PIR_DEBOUNCE_MS = 200;  // PROTOCOL.md 2.9
static const uint32_t HEARTBEAT_MS = 2000;    // PROTOCOL.md 2.8
static const uint32_t REPLY_TIMEOUT_MS = 5000;
static const uint32_t TRIGGER_RETRY_MS = 5000;
static const uint8_t TRIGGER_RETRIES = 3;     // PROTOCOL.md 2.8 step 3
static const uint32_t ALARM_MS = 10000;       // status 2 / fail-safe (2.4.5)
static const uint32_t ESCALATION_MS = 30000;  // `alarm` message (2.4.5)
static const uint32_t WIFI_RETRY_MS = 5000;
static const uint32_t LINK_CONNECT_TIMEOUT_MS = 2000;
static const uint32_t LINK_BACKOFF_MS[4] = {500, 1000, 2000, 5000}; // 2.7 step 3

// -- UART demo mode (PROTOCOL.md 9) ------------------------------------------
#if SERIAL_DEMO
// ~3 s for ALERT, per the prompt: long enough to notice, short enough that a
// false answer does not drown the room. SAFE is one short chirp.
static const uint32_t DEMO_ALERT_MS = 3000;
static const uint32_t DEMO_SAFE_MS = 120;
static const char DEMO_CMD_MOTION[] = "MOTION";
#endif

// -- buzzer -----------------------------------------------------------------
static const uint32_t BUZZ_CARRIER_HZ = 2000; // PROTOCOL.md 2.9
static const uint8_t BUZZ_RESOLUTION_BITS = 10;
static const uint32_t BUZZ_DUTY = 512; // 50 %
static const uint8_t BUZZ_CHANNEL = 0; // only used by the 2.x API below

// ---------------------------------------------------------------------------
// Serial log. Every line the brief asks for is emitted from exactly one place:
// boot, warmup done, PIR fired, trigger sent, reply status, alarm start/stop,
// reconnects.
// ---------------------------------------------------------------------------
static void slogf(const char* fmt, ...) {
  char line[256];
  va_list args;
  va_start(args, fmt);
  vsnprintf(line, sizeof(line), fmt, args);
  va_end(args);
  Serial.printf("[%8lu] %s\n", (unsigned long)millis(), line);
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
static WiFiClient client;

static bool linkUp = false;
static uint8_t backoffStep = 0;
static uint32_t nextAttemptMs = 0;
static uint32_t lastHeartbeatMs = 0;
static uint32_t lastWifiAttemptMs = 0;

static volatile bool pirEdge = false;
static uint32_t warmupUntilMs = 0;
static bool warmupActive = true;
static uint32_t lastPirMs = 0;
static uint32_t cooldownUntilMs = 0;

// Fail-safe latch (PROTOCOL.md 2.8): one 10 s alarm per link-down / reply
// timeout, re-armed when the link comes back.
static bool failSafeLatched = false;

// Periodic 100 ms warning beep every 5 s while a trigger is outstanding and
// the Pi is unreachable ("local warning beep" in shield-esp32-firmware-pass.md).
static bool warnRepeat = false;
static uint32_t warnNextMs = 0;
static uint32_t warnOffAtMs = 0;

struct TriggerState {
  char id[12];        // 8 hex chars + NUL; stable across retries
  char tier[8];       // "high" | "low" (PROTOCOL.md 2.6)
  char sensors[24];   // ["pir"] or ["pir","mmwave"]
  uint8_t attempts;   // copies sent, first one included
  bool awaiting;      // a copy is in flight, deadline running
  bool exhausted;     // 3 retries used; keep the id for a late reply
  uint32_t deadlineMs;
  uint32_t retryAtMs;
};
static TriggerState trig; // zero-initialised

struct BuzzerState {
  uint8_t pattern;
  uint32_t startedMs;
  uint32_t untilMs;
};
enum BuzPattern : uint8_t { BUZZ_OFF = 0, BUZZ_CHIRP, BUZZ_SIREN };
static BuzzerState buz = {BUZZ_OFF, 0, 0};

#if USE_LD2410
static uint32_t lastRadarMs = 0;
#endif

// Receive reassembly buffer. 8213 B static is deliberate: PROTOCOL.md 2.3 says
// a len above 8192 closes the connection, so the buffer can never be overrun.
static uint8_t rxFrame[HEADER_BYTES + MAX_BODY + TAG_BYTES];
static size_t rxLen = 0;
static uint8_t rxFaults = 0;
static uint8_t txFrame[HEADER_BYTES + MAX_TX_BODY + TAG_BYTES];

// ---------------------------------------------------------------------------
// HMAC (PROTOCOL.md 2.5)
// ---------------------------------------------------------------------------
static bool hmacTag(const uint8_t* msg, size_t len, uint8_t out[TAG_BYTES]) {
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (info == NULL) return false;
  uint8_t full[32];
  int rc = mbedtls_md_hmac(info,
                           (const unsigned char*)SHIELD_HMAC_KEY,
                           strlen(SHIELD_HMAC_KEY),
                           msg, len, full);
  if (rc != 0) return false;
  memcpy(out, full, TAG_BYTES);
  return true;
}

// Constant-time on purpose: a byte-by-byte compare leaks a prefix match, which
// is exactly the forgery 2.5 exists to prevent.
static bool ctEqual(const uint8_t* a, const uint8_t* b, size_t n) {
  uint8_t diff = 0;
  for (size_t i = 0; i < n; i++) diff |= (uint8_t)(a[i] ^ b[i]);
  return diff == 0;
}

// ---------------------------------------------------------------------------
// Framing (PROTOCOL.md 2.3)
// ---------------------------------------------------------------------------
static bool sendFrame(const char* body) {
  if (!linkUp) return false;
  size_t bodyLen = strlen(body);
  if (bodyLen == 0 || bodyLen > MAX_TX_BODY) {
    slogf("frame not sent: body is %u bytes (max %u)", (unsigned)bodyLen, (unsigned)MAX_TX_BODY);
    return false;
  }

  uint8_t* f = txFrame;
  f[0] = (uint8_t)((bodyLen >> 24) & 0xFF);
  f[1] = (uint8_t)((bodyLen >> 16) & 0xFF);
  f[2] = (uint8_t)((bodyLen >> 8) & 0xFF);
  f[3] = (uint8_t)(bodyLen & 0xFF);
  f[4] = FLAG_JSON;
  memcpy(f + HEADER_BYTES, body, bodyLen);

  uint8_t tag[TAG_BYTES];
  if (!hmacTag(f, HEADER_BYTES + bodyLen, tag)) {
    slogf("frame not sent: HMAC failed");
    return false;
  }
  memcpy(f + HEADER_BYTES + bodyLen, tag, TAG_BYTES);

  size_t total = HEADER_BYTES + bodyLen + TAG_BYTES;
  size_t written = client.write(f, total);
  if (written != total) {
    slogf("frame not sent: wrote %u of %u bytes", (unsigned)written, (unsigned)total);
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Minimal JSON readers
//
// The bodies we parse are produced by shield_rpi/link.py's json.dumps with
// compact separators, and every field we read is a plain string or integer, so
// a hand-rolled scanner is enough. Escaped quotes are skipped; \u escapes are
// not needed for ids, reasons or event ids.
// ---------------------------------------------------------------------------
static const char* jsonFindKey(const char* json, const char* key) {
  char pattern[40];
  snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  const char* p = strstr(json, pattern);
  if (p == NULL) return NULL;
  p += strlen(pattern);
  while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
  if (*p != ':') return NULL;
  return p + 1;
}

static bool jsonGetString(const char* json, const char* key, char* out, size_t outLen) {
  const char* p = jsonFindKey(json, key);
  if (p == NULL || *p != '"') return false;
  p++;
  size_t i = 0;
  while (*p != '\0' && *p != '"') {
    if (*p == '\\' && p[1] != '\0') p++;
    if (i + 1 < outLen) out[i++] = *p;
    p++;
  }
  out[i] = '\0';
  return *p == '"';
}

static bool jsonGetLong(const char* json, const char* key, long* out) {
  const char* p = jsonFindKey(json, key);
  if (p == NULL) return false;
  char* end = NULL;
  long value = strtol(p, &end, 10);
  if (end == p) return false;
  *out = value;
  return true;
}

// ---------------------------------------------------------------------------
// Message envelopes (PROTOCOL.md 2.4)
// ---------------------------------------------------------------------------
static void makeId(char* out, size_t outLen) {
  snprintf(out, outLen, "%08lx", (unsigned long)esp_random());
}

// shield-net has no internet path, so there is no NTP: without a seed from
// secrets.h the wall clock reads 1970. sent_at is advisory only (2.4), and
// ts_ms is monotonic, so this never affects the link.
static void formatSentAt(char* out, size_t outLen) {
  time_t now = time(NULL);
  if (now < (time_t)1600000000) {
    strlcpy(out, "1970-01-01T00:00:00.000Z", outLen);
    return;
  }
  struct tm utc;
  gmtime_r(&now, &utc);
  // strftime rather than snprintf: it returns 0 instead of truncating if the
  // year ever does not fit, which a hand-rolled "%04d" cannot promise.
  if (strftime(out, outLen, "%Y-%m-%dT%H:%M:%S.000Z", &utc) == 0) {
    strlcpy(out, "1970-01-01T00:00:00.000Z", outLen);
  }
}

// Writes the common envelope with no closing brace. If id[0] is already set it
// is reused, which is how a retried trigger keeps the id the Pi is answering.
static void beginEnvelope(char* out, size_t outLen, const char* type, char* id, size_t idLen) {
  if (id[0] == '\0') makeId(id, idLen);
  char sentAt[32];
  formatSentAt(sentAt, sizeof(sentAt));
  snprintf(out, outLen,
           "{\"v\":%u,\"type\":\"%s\",\"id\":\"%s\",\"ts_ms\":%lu,\"sent_at\":\"%s\"",
           (unsigned)PROTO_VERSION, type, id, (unsigned long)millis(), sentAt);
}

static bool sendHeartbeat() {
  char body[224];
  char id[12] = "";
  beginEnvelope(body, sizeof(body), "heartbeat", id, sizeof(id));
  strlcat(body, "}", sizeof(body));
  return sendFrame(body);
}

static bool sendAlarmAck() {
  char body[256];
  char id[12] = "";
  beginEnvelope(body, sizeof(body), "alarm_ack", id, sizeof(id));
  strlcat(body, ",\"reason\":\"buzzer_running\"}", sizeof(body));
  return sendFrame(body);
}

// ---------------------------------------------------------------------------
// Receive path (PROTOCOL.md 2.3 / 2.5 / 2.7)
//
// Recoverable faults (bad tag, bad flags) are counted and skipped, one count
// per *frame* so a peer cannot pack many forgeries into one packet and be
// judged a single attempt. A len above MAX_BODY means the stream position can
// no longer be trusted, so the connection is closed instead: there is no magic
// byte to resynchronise on.
// ---------------------------------------------------------------------------
static void handleMessage(char* json);
static void dropLink(const char* why);

static bool extractFrames() {
  while (true) {
    if (rxLen < HEADER_BYTES) return true;

    uint32_t bodyLen = ((uint32_t)rxFrame[0] << 24) | ((uint32_t)rxFrame[1] << 16) |
                       ((uint32_t)rxFrame[2] << 8) | (uint32_t)rxFrame[3];
    if (bodyLen > MAX_BODY) {
      slogf("link: declared body length %lu exceeds %u; closing", (unsigned long)bodyLen,
            (unsigned)MAX_BODY);
      return false;
    }

    size_t total = HEADER_BYTES + (size_t)bodyLen + TAG_BYTES;
    if (rxLen < total) return true;

    uint8_t expected[TAG_BYTES];
    if (!hmacTag(rxFrame, HEADER_BYTES + bodyLen, expected) ||
        !ctEqual(expected, rxFrame + HEADER_BYTES + bodyLen, TAG_BYTES)) {
      rxFaults++;
      slogf("link: HMAC tag did not verify; frame dropped, faults %u/%u", rxFaults, FAULT_LIMIT);
      memmove(rxFrame, rxFrame + total, rxLen - total);
      rxLen -= total;
      if (rxFaults >= FAULT_LIMIT) {
        slogf("link: fault limit reached; closing");
        return false;
      }
      continue;
    }

    if (!(rxFrame[4] & FLAG_JSON)) {
      rxFaults++;
      slogf("link: body not flagged JSON (flags=0x%02x); frame dropped, faults %u/%u",
            rxFrame[4], rxFaults, FAULT_LIMIT);
      memmove(rxFrame, rxFrame + total, rxLen - total);
      rxLen -= total;
      if (rxFaults >= FAULT_LIMIT) return false;
      continue;
    }

    // Tag verified before the body is read (2.5), so an unauthenticated body
    // never reaches the parser. Terminate in place: the byte being overwritten
    // is the first tag byte of a frame we are about to discard.
    rxFrame[HEADER_BYTES + bodyLen] = '\0';
    handleMessage((char*)(rxFrame + HEADER_BYTES));

    memmove(rxFrame, rxFrame + total, rxLen - total);
    rxLen -= total;
  }
}

static void pumpLink() {
  while (client.available() > 0) {
    if (rxLen >= sizeof(rxFrame)) {
      slogf("link: receive buffer full; closing");
      dropLink("rx buffer full");
      return;
    }
    int n = client.read(rxFrame + rxLen, sizeof(rxFrame) - rxLen);
    if (n <= 0) break;
    rxLen += (size_t)n;
    if (!extractFrames()) {
      dropLink("protocol violation");
      return;
    }
  }
}

// ---------------------------------------------------------------------------
// Buzzer (LEDC, nominal 2 kHz)
//
// Non-blocking on purpose: the loop task must keep running for the link, the
// PIR and the watchdog, so nothing here waits.
// ---------------------------------------------------------------------------
static const char* patternName(uint8_t pattern) {
  switch (pattern) {
    case BUZZ_CHIRP: return "chirp";
    case BUZZ_SIREN: return "siren";
    default: return "off";
  }
}

static void buzDuty(uint32_t duty) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(BUZZER_PIN, duty);
#else
  ledcWrite(BUZZ_CHANNEL, duty);
#endif
}

static void buzFrequency(uint32_t hz) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWriteTone(BUZZER_PIN, hz);
#else
  ledcWriteTone(BUZZ_CHANNEL, hz);
#endif
}

static void buzOn(uint32_t hz) {
  buzFrequency(hz);
  buzDuty(BUZZ_DUTY);
}

static void buzOff() { buzDuty(0); }

static void buzzerInit() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(BUZZER_PIN, BUZZ_CARRIER_HZ, BUZZ_RESOLUTION_BITS);
#else
  ledcSetup(BUZZ_CHANNEL, BUZZ_CARRIER_HZ, BUZZ_RESOLUTION_BITS);
  ledcAttachPin(BUZZER_PIN, BUZZ_CHANNEL);
#endif
  buzOff();
}

static void buzzerStart(uint8_t pattern, uint32_t durationMs, const char* reason) {
  uint32_t now = millis();
  buz.pattern = pattern;
  buz.startedMs = now;
  buz.untilMs = now + durationMs;
  warnOffAtMs = 0;
  slogf("alarm start pattern=%s duration_ms=%lu reason=%s", patternName(pattern),
        (unsigned long)durationMs, reason);
}

static void buzzerStop(const char* reason) {
  if (buz.pattern == BUZZ_OFF) return;
  slogf("alarm stop pattern=%s elapsed_ms=%lu reason=%s", patternName(buz.pattern),
        (unsigned long)(millis() - buz.startedMs), reason);
  buz.pattern = BUZZ_OFF;
  buz.untilMs = 0;
  warnOffAtMs = 0;
  buzOff();
}

static void buzzerUpdate() {
  uint32_t now = millis();

  if (buz.pattern != BUZZ_OFF) {
    if ((int32_t)(now - buz.untilMs) >= 0) {
      buzzerStop("duration elapsed");
    } else {
      switch (buz.pattern) {
        case BUZZ_CHIRP:
          // Short chirp: 150 ms on / 850 ms off at 2 kHz (2.4.5, status 2 and
          // fail-safe). Shorter and duller than the escalation siren on purpose.
          if (((now - buz.startedMs) % 1000) < 150) buzOn(BUZZ_CARRIER_HZ);
          else buzOff();
          break;
        case BUZZ_SIREN: {
          // Escalation siren: triangle sweep 1600..2400 Hz over 1.6 s, centred
          // on the nominal 2 kHz of PROTOCOL.md 2.9.
          uint32_t t = (now - buz.startedMs) % 1600;
          uint32_t hz = (t < 800) ? (1600 + t) : (1600 + (1600 - t));
          buzOn(hz);
          break;
        }
        default:
          break;
      }
      return;
    }
  }

  // Idle: the unreachable-link warning beep, 100 ms every 5 s, only while a
  // trigger is outstanding. It never pre-empts a running alarm.
  if (warnRepeat && warnOffAtMs == 0 && (int32_t)(now - warnNextMs) >= 0) {
    buzOn(BUZZ_CARRIER_HZ);
    warnOffAtMs = now + 100;
    warnNextMs = now + 5000;
  }
  if (warnOffAtMs != 0 && (int32_t)(now - warnOffAtMs) >= 0) {
    buzOff();
    warnOffAtMs = 0;
  }
}

// ---------------------------------------------------------------------------
// Fail-safe (PROTOCOL.md 2.8): absence of information is a threat.
// ---------------------------------------------------------------------------
static void failSafe(const char* cause) {
  if (failSafeLatched) {
    slogf("local_fail_safe_alarm already sounding; cause=%s not re-armed", cause);
    return;
  }
  failSafeLatched = true;
  slogf("local_fail_safe_alarm cause=%s; buzzer %lu ms", cause, (unsigned long)ALARM_MS);
  buzzerStart(BUZZ_CHIRP, ALARM_MS, cause);
}

static void clearFailSafe() { failSafeLatched = false; }

// ---------------------------------------------------------------------------
// Wi-Fi + link lifecycle (PROTOCOL.md 2.7)
// ---------------------------------------------------------------------------
static void scheduleReconnect(uint32_t delayMs) {
  nextAttemptMs = millis() + delayMs;
}

static void dropLink(const char* why) {
  bool wasUp = linkUp;
  client.stop();
  linkUp = false;
  rxLen = 0;
  rxFaults = 0;
  backoffStep = 0;
  scheduleReconnect(LINK_BACKOFF_MS[0]);

  if (!wasUp) return;
  slogf("link lost: %s", why);

  // 2.8: the link dropping is itself a reason to alarm -- an RPi reboot takes
  // shield-net down, and that must be audible rather than silent.
  failSafe("link_down");

  if (trig.id[0] != '\0') {
    trig.awaiting = false;
    trig.retryAtMs = millis() + TRIGGER_RETRY_MS;
    warnRepeat = true;
    warnNextMs = millis();
    slogf("trigger id=%s kept; will retry when the link returns", trig.id);
  }
}

static void onLinkUp() {
  linkUp = true;
  backoffStep = 0;
  rxLen = 0;
  rxFaults = 0;
  clearFailSafe();
  warnRepeat = false;
  lastHeartbeatMs = millis() - HEARTBEAT_MS; // heartbeat immediately
  slogf("link connected to %s:%u", RPI_HOST, (unsigned)RPI_PORT);
}

static void serviceLink() {
  if (linkUp) {
    if (!client.connected()) {
      dropLink("peer closed");
      return;
    }
    pumpLink();
    if (!linkUp) return;
    uint32_t now = millis();
    if ((int32_t)(now - lastHeartbeatMs) >= (int32_t)HEARTBEAT_MS) {
      lastHeartbeatMs = now;
      if (!sendHeartbeat()) {
        dropLink("heartbeat send failed");
        return;
      }
    }
    return;
  }

  if (WiFi.status() != WL_CONNECTED) return;
  uint32_t now = millis();
  if ((int32_t)(now - nextAttemptMs) < 0) return;

  slogf("link: connecting to %s:%u", RPI_HOST, (unsigned)RPI_PORT);
  client.setConnectionTimeout(LINK_CONNECT_TIMEOUT_MS);
  client.connect(RPI_HOST, RPI_PORT);
  if (!client.connected()) {
    client.stop();
    slogf("link: connect failed; retry in %lu ms", (unsigned long)LINK_BACKOFF_MS[backoffStep]);
    scheduleReconnect(LINK_BACKOFF_MS[backoffStep]);
    if (backoffStep < 3) backoffStep++;
    return;
  }
  onLinkUp();
}

static void serviceWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  uint32_t now = millis();
  if ((int32_t)(now - lastWifiAttemptMs) >= (int32_t)WIFI_RETRY_MS) {
    lastWifiAttemptMs = now;
    slogf("wifi: not connected (status=%d); rejoining %s", (int)WiFi.status(), WIFI_SSID);
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  // A socket over a dead interface reports closed on the next serviceLink().
}

// ---------------------------------------------------------------------------
// Trigger state machine (PROTOCOL.md 2.8)
// ---------------------------------------------------------------------------
static void sendTriggerAttempt() {
  char body[320];
  beginEnvelope(body, sizeof(body), "trigger", trig.id, sizeof(trig.id));
  size_t used = strlen(body);
  snprintf(body + used, sizeof(body) - used, ",\"tier\":\"%s\",\"sensors\":[%s]}",
           trig.tier, trig.sensors);

  if (sendFrame(body)) {
    trig.attempts++;
    trig.awaiting = true;
    trig.deadlineMs = millis() + REPLY_TIMEOUT_MS;
    warnRepeat = false;
    slogf("trigger sent id=%s attempt=%u/%u tier=%s sensors=[%s]", trig.id, trig.attempts,
          (unsigned)(1 + TRIGGER_RETRIES), trig.tier, trig.sensors);
    return;
  }

  slogf("trigger id=%s not sent (link down)", trig.id);
  failSafe("link_down");
  warnRepeat = true;
  warnNextMs = millis();
  trig.retryAtMs = millis() + TRIGGER_RETRY_MS;
}

static void onReplyTimeout(uint32_t now) {
  slogf("no reply within %lu ms for trigger id=%s", (unsigned long)REPLY_TIMEOUT_MS, trig.id);
  failSafe("reply_timeout");
  trig.awaiting = false;
  trig.retryAtMs = now + TRIGGER_RETRY_MS;
}

static void serviceTrigger() {
  if (trig.id[0] == '\0') return;
  uint32_t now = millis();

  if (trig.awaiting && (int32_t)(now - trig.deadlineMs) >= 0) onReplyTimeout(now);
  if (trig.awaiting || trig.exhausted || !linkUp) return;
  if ((int32_t)(now - trig.retryAtMs) < 0) return;

  if (trig.attempts >= (uint8_t)(1 + TRIGGER_RETRIES)) {
    trig.exhausted = true;
    slogf("trigger id=%s: %u attempts used; keeping the id for a late reply", trig.id,
          trig.attempts);
    return;
  }
  sendTriggerAttempt();
}

static void startTrigger() {
#if SERIAL_DEMO
  // Demo path: no trigger state machine, no link, no HMAC. One MOTION line out
  // and return. The cooldown in servicePir() is what stops one person from
  // spamming the line - the bridge on the other end discards a burst anyway,
  // but not sending it is cheaper than making it discard.
  cooldownUntilMs = millis() + COOLDOWN_MS;
  Serial.print(DEMO_CMD_MOTION);
  Serial.print("\n");
  slogf("motion: MOTION sent over serial (demo mode)");
  return;
#else
  trig.id[0] = '\0';
  trig.attempts = 0;
  trig.awaiting = false;
  trig.exhausted = false;
  trig.deadlineMs = 0;

  bool mmwave = false;
#if USE_LD2410
  mmwave = (lastRadarMs != 0) && ((int32_t)(millis() - lastRadarMs) <= (int32_t)RADAR_WINDOW_MS);
#endif
  strlcpy(trig.tier, mmwave ? "high" : "low", sizeof(trig.tier));
  strlcpy(trig.sensors, mmwave ? "\"pir\",\"mmwave\"" : "\"pir\"", sizeof(trig.sensors));
  makeId(trig.id, sizeof(trig.id));

  trig.retryAtMs = millis();
  cooldownUntilMs = millis() + COOLDOWN_MS;
  slogf("motion: trigger id=%s tier=%s", trig.id, trig.tier);

  if (!linkUp) {
    failSafe("link_down");
    warnRepeat = true;
    warnNextMs = millis();
  }
  serviceTrigger(); // send immediately when the link is up
#endif
}

#if SERIAL_DEMO
// Partial-line buffer for the demo link. A USB CDC read can split "ALERT" at any
// point, so acting on a half-line would chirp on the letters A, L or R.
static char demoRx[32];
static size_t demoRxLen = 0;

static bool demoEquals(const char* line, const char* want) {
  size_t i = 0;
  while (line[i] != '\0' && want[i] != '\0') {
    char a = line[i];
    if (a >= 'a' && a <= 'z') a = (char)(a - 'a' + 'A');
    char b = want[i];
    if (b >= 'a' && b <= 'z') b = (char)(b - 'a' + 'A');
    if (a != b) return false;
    i++;
  }
  return line[i] == '\0' && want[i] == '\0';
}

static void serviceDemoSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;      // tolerate CRLF from a laptop-side writer
    if (c != '\n') {
      if (demoRxLen < sizeof(demoRx) - 1) demoRx[demoRxLen++] = c;
      continue;
    }

    demoRx[demoRxLen] = '\0';
    if (demoRxLen == 0) continue;  // blank line
    demoRxLen = 0;

    slogf("serial in: %s", demoRx);
    if (demoEquals(demoRx, "ALERT")) {
      buzzerStart(BUZZ_SIREN, DEMO_ALERT_MS, "serial_alert");
    } else if (demoEquals(demoRx, "SAFE")) {
      buzzerStart(BUZZ_CHIRP, DEMO_SAFE_MS, "serial_safe");
    } else {
      // Logged and ignored, never a fault: one unexpected line must not wedge
      // the buzzer or stop the PIR.
      slogf("serial: unrecognised command '%s'; ignored", demoRx);
    }
  }
}
#endif

static void onReply(char* json) {
  char triggerId[24] = "";
  if (!jsonGetString(json, "trigger_id", triggerId, sizeof(triggerId))) {
    slogf("reply without trigger_id; ignored");
    return;
  }
  long status = -1;
  long confidence = -1;
  jsonGetLong(json, "status", &status);
  jsonGetLong(json, "confidence", &confidence);

  if (trig.id[0] == '\0' || strcmp(triggerId, trig.id) != 0) {
    slogf("reply for unknown trigger id=%s (ignored)", triggerId);
    return;
  }

  slogf("reply status=%ld confidence=%ld trigger_id=%s", status, confidence, triggerId);
  trig.id[0] = '\0';
  trig.awaiting = false;
  trig.exhausted = false;
  warnRepeat = false;
  clearFailSafe();

  // PROTOCOL.md 2.4.3: 0 clear and 1 known are silent, 2 alarms for 10 s.
  if (status == 2) buzzerStart(BUZZ_CHIRP, ALARM_MS, "reply_status_2");
}

static void onAlarm(char* json) {
  char reason[64] = "unknown";
  char eventId[64] = "";
  jsonGetString(json, "reason", reason, sizeof(reason));
  jsonGetString(json, "event_id", eventId, sizeof(eventId));
  slogf("alarm received reason=%s event_id=%s", reason, eventId);
  buzzerStart(BUZZ_SIREN, ESCALATION_MS, reason);
  if (!sendAlarmAck()) slogf("alarm_ack could not be sent");
}

// `silence` is what verdict.py sends when the user answers "known"
// (shield_rpi/verdict.py:127). It is missing from the PROTOCOL.md 2.4 table --
// recorded as D-047 -- but it is on the wire, so it is handled.
static void onSilence(char* json) {
  char reason[64] = "unknown";
  char eventId[64] = "";
  jsonGetString(json, "reason", reason, sizeof(reason));
  jsonGetString(json, "event_id", eventId, sizeof(eventId));
  slogf("silence received reason=%s event_id=%s", reason, eventId);
  buzzerStop("received silence");
  warnRepeat = false;
}

static void handleMessage(char* json) {
  char type[32] = "";
  if (!jsonGetString(json, "type", type, sizeof(type))) {
    slogf("link: frame without a type; ignored");
    return;
  }
  if (strcmp(type, "reply") == 0) {
    onReply(json);
  } else if (strcmp(type, "alarm") == 0) {
    onAlarm(json);
  } else if (strcmp(type, "silence") == 0) {
    onSilence(json);
  } else if (strcmp(type, "heartbeat_ack") == 0) {
    // Expected every 2 s; logging it would bury the lines that matter.
  } else if (strcmp(type, "health") == 0) {
    // The brief asks for this one, but it does not exist on the socket:
    // PROTOCOL.md 2.4 has no `health` and link.py never sends it. The
    // `GET /health` that exists is the HTTP shim on 127.0.0.1 (D-046), a
    // different protocol. Handled here so the intent is on the record, and
    // ignored so that a future RPi that did send it could not wedge us (D-047).
    slogf("link: 'health' is not a frame type; ignored");
  } else {
    // Unknown types are logged, never counted as a fault: one RPi bug should
    // not take down the link the RPi is trying to reach.
    slogf("link: unhandled message type '%s'; ignored", type);
  }
}

// ---------------------------------------------------------------------------
// PIR (IO4). The ISR only sets a flag -- no network, no allocation, no logging.
// ---------------------------------------------------------------------------
void IRAM_ATTR pirIsr() { pirEdge = true; }

static void servicePir() {
  uint32_t now = millis();

  if (warmupActive && (int32_t)(now - warmupUntilMs) >= 0) {
    warmupActive = false;
    slogf("warmup done; PIR armed");
  }

  if (!pirEdge) return;
  pirEdge = false;

  slogf("PIR fired");
  if (warmupActive) {
    slogf("  ignored: boot warmup, %lu ms left", (unsigned long)(warmupUntilMs - now));
    return;
  }
  if ((int32_t)(now - lastPirMs) < (int32_t)PIR_DEBOUNCE_MS) return; // 2.9 debounce
  lastPirMs = now;
  if ((int32_t)(now - cooldownUntilMs) < 0) {
    slogf("  ignored: cooldown, %lu ms left", (unsigned long)(cooldownUntilMs - now));
    return;
  }
  if (trig.id[0] != '\0') {
    slogf("  ignored: trigger id=%s still in flight", trig.id);
    return;
  }
  startTrigger();
}

// ---------------------------------------------------------------------------
// LD2410 mmWave (optional). PROTOCOL.md 6: the vendor frame is header AA AA,
// a little-endian length, then payload and an XOR checksum over everything
// before it. Framing on the length field rather than a fixed 23/24-byte size
// sidesteps the off-by-one in the 6 table (offset 10 size 13 plus a checksum
// at 23 does not add up to the stated 23 total) and follows the real device.
// ---------------------------------------------------------------------------
#if USE_LD2410
static uint8_t ldBuf[64];
static size_t ldLen = 0;

static void radarInit() {
  Serial1.begin(LD2410_BAUD, SERIAL_8N1, LD2410_RX_PIN, LD2410_TX_PIN);
  slogf("ld2410: UART1 at %lu baud, rx=IO%u", (unsigned long)LD2410_BAUD, LD2410_RX_PIN);
}

static void radarFrame(size_t total) {
  uint8_t sum = 0;
  for (size_t i = 0; i + 1 < total; i++) sum ^= ldBuf[i];
  if (sum != ldBuf[total - 1]) return;
  if (ldBuf[4] == 0x02 && ldBuf[5] == 0x01) lastRadarMs = millis();
}

static void radarPoll() {
  while (Serial1.available() > 0) {
    uint8_t b = (uint8_t)Serial1.read();
    if (ldLen == 0) {
      if (b == 0xAA) ldBuf[ldLen++] = b;
      continue;
    }
    if (ldLen == 1) {
      if (b == 0xAA) ldBuf[ldLen++] = b;
      else ldLen = 0;
      continue;
    }
    if (ldLen >= sizeof(ldBuf)) {
      ldLen = 0;
      continue;
    }
    ldBuf[ldLen++] = b;
    if (ldLen < 4) continue;

    size_t total = 4 + ldBuf[2] + ((size_t)ldBuf[3] << 8);
    if (total < 6 || total > sizeof(ldBuf)) {
      ldLen = 0;
      continue;
    }
    if (ldLen >= total) {
      radarFrame(total);
      ldLen = 0;
    }
  }
}
#endif

// ---------------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------------
static void seedClock() {
#if TIME_SEED_EPOCH_S > 0
  if (TIME_SEED_EPOCH_S > 1600000000UL) {
    struct timeval tv;
    tv.tv_sec = (time_t)TIME_SEED_EPOCH_S;
    tv.tv_usec = 0;
    settimeofday(&tv, NULL);
    slogf("clock: seeded from TIME_SEED_EPOCH_S");
  }
#else
  slogf("clock: no wall-clock seed; sent_at will read 1970 (advisory only, PROTOCOL.md 2.4)");
#endif
}

static void wifiConnectBlocking() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  slogf("wifi: joining %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - started) < 20000) delay(100);

  if (WiFi.status() == WL_CONNECTED) {
    slogf("wifi: connected ip=%s rssi=%d dBm", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    slogf("wifi: not connected after 20000 ms; rejoining in the background");
    lastWifiAttemptMs = millis();
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  slogf("boot: S.H.I.E.L.D sensor node (shield_esp32.ino, PROTOCOL v%u)", PROTO_VERSION);
  slogf("boot: chip=%s rev=%d cores=%d flash=%u MB", ESP.getChipModel(), ESP.getChipRevision(),
        ESP.getChipCores(), (unsigned)(ESP.getFlashChipSize() / (1024 * 1024)));
  slogf("boot: watchdog left at its default (never disabled)");

  seedClock();
  buzzerInit();

  pinMode(PIR_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(PIR_PIN), pirIsr, RISING);

#if USE_LD2410
  radarInit();
#else
  slogf("ld2410: disabled (USE_LD2410=0)");
#endif

#if SERIAL_DEMO
  // No Wi-Fi in demo mode: the link is a USB cable. Joining an AP here would
  // only add a 20 s stall at boot and a reconnect loop to look at when the
  // thing being debugged is the buzzer.
  slogf("demo: SERIAL_DEMO=1, Wi-Fi and the HMAC link are disabled (PROTOCOL.md 9)");
  slogf("demo: send MOTION over USB serial, expect ALERT or SAFE back");
#else
  wifiConnectBlocking();
#endif

  warmupUntilMs = millis() + WARMUP_MS;
  warmupActive = true;
  cooldownUntilMs = millis();
  slogf("warmup: PIR ignored for %lu ms", (unsigned long)WARMUP_MS);
}

void loop() {
#if SERIAL_DEMO
  serviceDemoSerial();
#else
  serviceWifi();
  serviceLink();
#endif
#if USE_LD2410
  radarPoll();
#endif
  servicePir();
  serviceTrigger();
  buzzerUpdate();

  delay(2); // yield; the loop task never busy-waits
}
