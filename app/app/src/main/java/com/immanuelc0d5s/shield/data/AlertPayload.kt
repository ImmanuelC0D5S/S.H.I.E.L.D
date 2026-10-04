package com.immanuelc0d5s.shield.data

/**
 * One SHIELD alert, parsed from the FCM data payload.
 *
 * Every field arrives as a [String]. FCM data messages carry string values only,
 * and the Edge Function stringifies them deliberately so the contract is
 * identical on both sides (shield-edge-function-prompt.md, step 6). Parsing
 * defensively is therefore not paranoia: a malformed value must produce a
 * degraded alert, never a crash inside [com.immanuelc0d5s.shield.fcm.ShieldMessagingService],
 * because a crash there means the alarm never sounds.
 *
 * The six field names are fixed by shield-app-prompt.md and pinned by
 * `rpi/tests/test_credentials.py::test_the_alert_payload_fields_are_the_same_on_both_sides`.
 */
data class AlertPayload(
    val eventId: String,
    /** Unix seconds, UTC. */
    val ts: Long,
    val status: Int,
    /** 0-100. See shield_rpi/pipeline.py for what this measures. */
    val confidence: Int,
    /** Signed Supabase Storage URL. Empty if the snapshot could not be signed. */
    val imageUrl: String,
    val needsVerdict: Boolean,
) {
    /**
     * True when this alert is one the user must answer.
     *
     * Only `status = 2` reaches the phone - the Edge Function drops 0 and 1 - so
     * this is a sanity check rather than a filter, and it exists to make a
     * malformed push visible instead of silently showing a verdict UI for an
     * event that will never be written.
     */
    val requiresVerdict: Boolean get() = status == STATUS_ALERT && needsVerdict

    val hasImage: Boolean get() = imageUrl.isNotBlank()

    companion object {
        const val STATUS_CLEAR = 0
        const val STATUS_KNOWN = 1
        const val STATUS_ALERT = 2

        const val KEY_EVENT_ID = "event_id"
        const val KEY_TS = "ts"
        const val KEY_STATUS = "status"
        const val KEY_CONFIDENCE = "confidence"
        const val KEY_IMAGE_URL = "image_url"
        const val KEY_NEEDS_VERDICT = "needs_verdict"

        /**
         * Parse a data payload, returning null when there is no usable event id.
         *
         * Returns null rather than throwing: a payload this function cannot read
         * is not a reason to drop the whole message on the floor, but it is not
         * something to invent an event for either, because the verdict buttons
         * would then write to an `events` row that does not exist.
         */
        fun fromMap(data: Map<String, String>): AlertPayload? {
            val eventId = data[KEY_EVENT_ID]?.takeIf { it.isNotBlank() } ?: return null

            return AlertPayload(
                eventId = eventId,
                ts = data[KEY_TS]?.toLongOrNull() ?: 0L,
                status = data[KEY_STATUS]?.toIntOrNull() ?: STATUS_ALERT,
                confidence = (data[KEY_CONFIDENCE]?.toIntOrNull() ?: 0).coerceIn(0, 100),
                imageUrl = data[KEY_IMAGE_URL].orEmpty(),
                needsVerdict = data[KEY_NEEDS_VERDICT]?.toBooleanStrictOrNull() ?: true,
            )
        }
    }
}

/** What the user tapped on the alert screen. Mirrors the `events.verdict` check constraint. */
enum class Verdict(val wire: String) {
    KNOWN("known"),
    INTRUDER("intruder");

    companion object {
        fun fromWire(value: String?): Verdict? = entries.firstOrNull { it.wire == value }
    }
}

/** One row of the local event log, or of the shared `events` table. */
data class ShieldEvent(
    val id: String,
    val ts: Long,
    val status: Int,
    val confidence: Int,
    val imagePath: String,
    val verdict: Verdict?,
    val identity: String?,
) {
    val isAlert: Boolean get() = status == AlertPayload.STATUS_ALERT

    /** Seconds since the event, for the "3 min ago" label in the history list. */
    fun ageSeconds(nowSeconds: Long): Long = (nowSeconds - ts).coerceAtLeast(0)
}