package com.immanuelc0d5s.shield

import com.immanuelc0d5s.shield.data.AlertPayload
import com.immanuelc0d5s.shield.data.Verdict
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Payload parsing tests.
 *
 * These are JVM tests, not instrumented, on purpose: the payload contract is a
 * plain string map and it is the one part of the app that a rename anywhere in
 * the pipeline silently breaks. The six field names are fixed by
 * shield-app-prompt.md and produced by `supabase/functions/send-alert/index.ts`;
 * `rpi/tests/test_credentials.py` pins the TypeScript side.
 */
class AlertPayloadTest {

    /** The exact example from shield-app-prompt.md. */
    private val canonical = mapOf(
        "event_id" to "3f2c8e14-0a1b-4c2d-8e3f-1a2b3c4d5e6f",
        "ts" to "1760000000",
        "status" to "2",
        "confidence" to "87",
        "image_url" to "https://project.supabase.co/storage/v1/object/sign/snapshots/a.jpg?token=eyJ",
        "needs_verdict" to "true",
    )

    @Test
    fun `parses the canonical payload`() {
        val payload = AlertPayload.fromMap(canonical)
        assertNotNull(payload)
        requireNotNull(payload)

        assertEquals("3f2c8e14-0a1b-4c2d-8e3f-1a2b3c4d5e6f", payload.eventId)
        assertEquals(1760000000L, payload.ts)
        assertEquals(2, payload.status)
        assertEquals(87, payload.confidence)
        assertTrue(payload.needsVerdict)
        assertTrue(payload.hasImage)
    }

    @Test
    fun `an event with status 2 requires a verdict`() {
        requireNotNull(AlertPayload.fromMap(canonical)).let {
            assertTrue(it.requiresVerdict)
        }
    }

    @Test
    fun `status 1 never requires a verdict`() {
        // The Edge Function drops 0 and 1, so reaching the app with one means a
        // contract mismatch - and alarming the user for a known face is exactly
        // the failure that makes people disable security apps.
        val known = AlertPayload.fromMap(canonical + ("status" to "1"))
        requireNotNull(known)
        assertFalse(known.requiresVerdict)
    }

    @Test
    fun `needs_verdict false suppresses the alert even at status 2`() {
        val payload = AlertPayload.fromMap(canonical + ("needs_verdict" to "false"))
        requireNotNull(payload)
        assertFalse(payload.requiresVerdict)
    }

    @Test
    fun `a payload with no event id is rejected`() {
        // Returning null is what stops the app inventing an event whose verdict
        // would then be written to a row that does not exist.
        assertNull(AlertPayload.fromMap(canonical - "event_id"))
    }

    @Test
    fun `a blank event id is rejected`() {
        assertNull(AlertPayload.fromMap(canonical + ("event_id" to "")))
        assertNull(AlertPayload.fromMap(canonical + ("event_id" to "   ")))
    }

    @Test
    fun `a malformed ts does not throw`() {
        // A crash inside onMessageReceived means the alarm never sounds, so every
        // field has to degrade rather than throw.
        val payload = AlertPayload.fromMap(canonical + ("ts" to "not-a-number"))
        requireNotNull(payload)
        assertEquals(0L, payload.ts)
    }

    @Test
    fun `a malformed status does not throw and defaults to alerting`() {
        val payload = AlertPayload.fromMap(canonical + ("status" to "???"))
        requireNotNull(payload)
        assertEquals(AlertPayload.STATUS_ALERT, payload.status)
    }

    @Test
    fun `confidence is clamped into range`() {
        val high = AlertPayload.fromMap(canonical + ("confidence" to "140"))
        val low = AlertPayload.fromMap(canonical + ("confidence" to "-20"))
        assertEquals(100, requireNotNull(high).confidence)
        assertEquals(0, requireNotNull(low).confidence)
    }

    @Test
    fun `a missing image url is not an error`() {
        // The Edge Function sends an empty image_url rather than failing when the
        // snapshot cannot be signed, so this is an expected shape.
        val payload = AlertPayload.fromMap(canonical + ("image_url" to ""))
        requireNotNull(payload)
        assertFalse(payload.hasImage)
    }

    @Test
    fun `a missing needs_verdict defaults to true`() {
        val payload = AlertPayload.fromMap(canonical - "needs_verdict")
        requireNotNull(payload)
        assertTrue(payload.needsVerdict)
    }

    @Test
    fun `the field names match the edge function contract`() {
        // Renaming any of these on either side means no alert ever shows a
        // snapshot. Asserted explicitly so the test fails on the rename rather
        // than on the symptom.
        assertEquals(
            setOf("event_id", "ts", "status", "confidence", "image_url", "needs_verdict"),
            setOf(
                AlertPayload.KEY_EVENT_ID,
                AlertPayload.KEY_TS,
                AlertPayload.KEY_STATUS,
                AlertPayload.KEY_CONFIDENCE,
                AlertPayload.KEY_IMAGE_URL,
                AlertPayload.KEY_NEEDS_VERDICT,
            ),
        )
    }

    @Test
    fun `verdict wire values match the database check constraint`() {
        // events.verdict has CHECK (verdict IN ('known','intruder')).
        assertEquals("known", Verdict.KNOWN.wire)
        assertEquals("intruder", Verdict.INTRUDER.wire)
        assertEquals(Verdict.KNOWN, Verdict.fromWire("known"))
        assertEquals(Verdict.INTRUDER, Verdict.fromWire("intruder"))
        assertNull(Verdict.fromWire("dismissed"))
        assertNull(Verdict.fromWire(null))
    }
}