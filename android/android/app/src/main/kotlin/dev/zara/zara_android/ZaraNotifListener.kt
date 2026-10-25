package dev.zara.zara_android

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Passive notification collector for the authorized device contract.
 *
 * Design limits (mirroring the accessibility service's battery honesty):
 * - NO polling, NO timers: the system pushes onNotificationPosted and the
 *   ring buffer only appends. A job explicitly requesting a read is the
 *   only path that drains it.
 * - Bounded: a fixed ring (MAX_ENTRIES) with per-field truncation caps;
 *   the buffer lives only in process memory, never persisted.
 * - Secret scrubbing happens AT CAPTURE: API-token/Bearer-looking
 *   material never reaches Dart transport, mirroring the Dart-side
 *   scrub() set and the core redaction patterns.
 * - Read access is NOT available until the user grants Notification
 *   listener access in Settings (the OS binds this service only then).
 *   Zara never opens Settings or asks silently — the job result states
 *   the missing grant honestly.
 */
class ZaraNotifListener : NotificationListenerService() {

    data class Entry(
        val whenMs: Long,
        val pkg: String,
        val title: String,
        val text: String,
        val category: String,
    )

    companion object {
        private const val MAX_ENTRIES = 20
        private const val MAX_FIELD = 120
        private const val MAX_TEXT = 200

        private val SECRET_PATTERNS = listOf(
            Regex("nvapi-[A-Za-z0-9_\\-]{8,}"),
            Regex("sk-ant-[A-Za-z0-9_\\-]{8,}"),
            Regex("sk-proj-[A-Za-z0-9_\\-]{8,}"),
            Regex("gsk_[A-Za-z0-9]{10,}"),
            Regex("zara-dev-\\S+"),
            Regex("zara-pair-\\S+"),
            Regex("Bearer \\S+"),
            Regex("AIza[0-9A-Za-z_\\-]{8,}"),
        )

        private val lock = Any()
        private val ring = ArrayDeque<Entry>()

        // ---- Stage 31-P3 phase B (P3f): bounded OTP scan -----------
        // Per-use consent lives in CORE (the tool parks for approval
        // EVERY time); the device side adds only FRESHNESS + SHAPE:
        // 4-8 digit standalone codes, inside a text that looks like an
        // OTP message, posted no older than maxAgeMs. The FULL body
        // never leaves the device: candidates carry package/title/code
        // only (least disclosure — the code is the only needed token).
        private val OTP_CONTEXT = Regex(
            "(?i)(code|otp|verification|verify|passcode|pin)",
        )
        private val OTP_CODE = Regex("(?<![0-9])[0-9]{4,8}(?![0-9])")

        fun otpScan(maxAgeMs: Long, senderHint: String?): Map<String, Any?> {
            synchronized(lock) {
                val now = System.currentTimeMillis()
                val fresh = ring.filter { now - it.whenMs <= maxAgeMs }
                    .takeLast(50)
                val items = mutableListOf<Map<String, Any?>>()
                for (e in fresh) {
                    val hay = e.title + " " + e.text
                    if (!OTP_CONTEXT.containsMatchIn(hay)) continue
                    if (senderHint != null && senderHint.isNotBlank() &&
                            !e.pkg.contains(senderHint, ignoreCase = true) &&
                            !hay.contains(senderHint, ignoreCase = true)) {
                        continue
                    }
                    val code = OTP_CODE.find(e.text)?.value
                        ?: OTP_CODE.find(e.title)?.value
                        ?: continue
                    items.add(mapOf(
                        "posted_at_ms" to e.whenMs,
                        "age_ms" to (now - e.whenMs),
                        "package" to e.pkg,
                        "title" to e.title.take(60),
                        "code" to code,
                    ))
                }
                return mapOf(
                    "supported" to true,
                    "count" to items.size,
                    "fresh_count" to fresh.size,
                    "candidates" to items.take(5),
                    "verification" to "fresh-window ring scan " +
                        "(<= ${maxAgeMs}ms); full bodies never leave the device",
                )
            }
        }


        private fun scrub(s: String): String {
            var out = s
            for (p in SECRET_PATTERNS) {
                out = p.replace(out, "[REDACTED]")
            }
            return out
        }

        /** Bounded read from the ring (entries newer than [sinceMs]). */
        fun snapshotSince(sinceMs: Long, limit: Int): Map<String, Any?> {
            synchronized(lock) {
                val newer = ring
                    .filter { it.whenMs >= sinceMs }
                    .toList()
                val items = newer
                    .takeLast(limit.coerceIn(1, 50))
                    .map {
                        mapOf(
                            "posted_at_ms" to it.whenMs,
                            "package" to it.pkg,
                            "title" to it.title,
                            "text" to it.text,
                            "category" to it.category,
                        )
                    }
                return mapOf(
                    "count" to items.size,
                    "truncated" to (newer.size > items.size),
                    "items" to items,
                )
            }
        }

        /** Post-capture store, honed by per-field caps + scrubbing. */
        private fun store(sbn: StatusBarNotification) {
            try {
                val extras =
                    sbn.notification?.extras ?: return
                val title = (extras.getCharSequence(
                    Notification.EXTRA_TITLE) ?: "").toString()
                val text = (extras.getCharSequence(
                    Notification.EXTRA_TEXT) ?: "").toString()
                val entry = Entry(
                    whenMs = sbn.notification?.`when`
                        ?: System.currentTimeMillis(),
                    pkg = (sbn.packageName ?: "").take(MAX_FIELD),
                    title = scrub(title).take(MAX_FIELD),
                    text = scrub(text).take(MAX_TEXT),
                    category = (sbn.notification?.category
                        ?: "").take(40),
                )
                synchronized(lock) {
                    while (ring.size >= MAX_ENTRIES) {
                        ring.removeFirst()
                    }
                    ring.addLast(entry)
                }
            } catch (_: Exception) {
                // A malformed notification is never allowed to break
                // the listener (the OS owns this process).
            }
        }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn != null) {
            store(sbn)
        }
    }
}
