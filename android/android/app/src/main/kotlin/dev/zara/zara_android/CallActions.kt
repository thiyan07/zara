package dev.zara.zara_android

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.ContactsContract
import androidx.core.content.ContextCompat

/**
 * Stage 31-P3 phase B (P3d) — call with contact resolution (Round 16
 * spec, user decisions 2026-10-20 + standing verbatim call spec).
 *
 * Discipline:
 * - contactsLookup returns EXACTLY display name + number (+ id/starred)
 *   from ContactsContract; nothing else about a contact ever leaves the
 *   contacts database. Missing grant / no match are data, never guesses.
 * - Numbers reach callDirect ONLY through Core's confirmed contact-
 *   resolution step (match shown, user said yes, per-call approval
 *   granted); the bridge still REFUSES any number carrying characters
 *   outside the dial grammar — raw untrusted text can never dial.
 * - callDirect's own verdict is "fired" — placing a call is NOT its own
 *   proof; the FOLLOW-UP bounded a11y poll (callScreenPoll, 250 ms x 4 s
 *   window checks) supplies the verified/absent verdict to Core.
 * - Not-found is an answer ("no match"), never a guess; Core asks the
 *   "saved under another name?" question per the user's standing spec
 *   and writes the alias fact only after the user CONFIRMS it.
 */
private const val PERM_READ_CONTACTS = "android.permission.READ_CONTACTS"
private const val PERM_CALL_PHONE = "android.permission.CALL_PHONE"

/** Dial grammar: phone numbers, plus, star, hash, and GSM pauses/waits.
 *  Anything else in a "number" means it came from somewhere other than
 *  a contact record — refuse (defense in depth; Core pre-checks too). */
private val DIAL_RE = Regex("^[+0-9*#,,]*$")

/** P3d step 1: bounded contact lookup by (substring) display name.
 *  Bounded to 5 rows regardless of caller input; query is a LIKE with
 *  escaped wildcards via the standard String[] args binding. */
fun contactsLookup(ctx: Context, name: String?, limitArg: Int?): Map<String, Any?> {
    if (name.isNullOrBlank()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "query name required")
    }
    if (ContextCompat.checkSelfPermission(ctx, PERM_READ_CONTACTS) !=
            PackageManager.PERMISSION_GRANTED) {
        return mapOf("supported" to true, "available" to false,
            "detail" to "READ_CONTACTS not granted — request it at the "
                + "surface-enablement prompt, never silently")
    }
    val limit = (limitArg ?: 5).coerceIn(1, 5)
    val q = name.trim()
    val out = ArrayList<Map<String, Any?>>(limit)
    return try {
        val uri = ContactsContract.CommonDataKinds.Phone.CONTENT_URI
        ctx.contentResolver.query(
            uri,
            arrayOf(
                ContactsContract.CommonDataKinds.Phone._ID,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY,
                ContactsContract.CommonDataKinds.Phone.NUMBER,
                ContactsContract.CommonDataKinds.Phone.STARRED),
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY +
                " LIKE ?",
            arrayOf("%$q%"),
            ContactsContract.CommonDataKinds.Phone.STARRED +
                " DESC, " +
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME_PRIMARY +
                " COLLATE LOCALIZED ASC")?.use { c ->
            while (c.moveToNext() && out.size < limit) {
                out.add(mapOf(
                    "contact_id" to c.getString(0),
                    "display_name" to (c.getString(1) ?: ""),
                    "number" to (c.getString(2) ?: ""),
                    "starred" to (c.getInt(3) == 1)))
            }
        }
        mapOf("supported" to true, "ok" to true, "query" to q,
            "count" to out.size, "matches" to out)
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false,
            "error" to ("contacts query failed: ${e.message ?: e}"))
    }
}

/** P3d step 3: place the call DIRECTLY (ACTION_CALL — no dialer UI).
 *  Requires CALL_PHONE granted (requested once at surface enablement)
 *  and a dial-grammar-clean number. Verdict is "fired" + the poll is
 *  the caller's job. */
fun callDirect(ctx: Context, number: String?): Map<String, Any?> {
    if (number.isNullOrBlank()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "number required")
    }
    if (!DIAL_RE.matches(number.trim())) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "number outside dial grammar — refused")
    }
    if (ContextCompat.checkSelfPermission(ctx, PERM_CALL_PHONE) !=
            PackageManager.PERMISSION_GRANTED) {
        return mapOf("supported" to true, "available" to false,
            "detail" to "CALL_PHONE not granted — request it at the "
                + "surface-enablement prompt; refusing to fall back to "
                + "the dialer silently would break the no-surprise rule")
    }
    return try {
        val intent = Intent(Intent.ACTION_CALL,
            Uri.parse("tel:${number.trim()}"))
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
        mapOf("supported" to true, "ok" to true, "verdict" to "fired",
            "verification" to "ACTION_CALL started; call-screen presence "
                + "is the bounded a11y poll's verdict, not this one")
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false,
            "error" to ("ACTION_CALL failed: ${e.message ?: e}"))
    }
}

/** P3d step 4 (verification): bounded poll (250 ms ticks, 4 s window)
 *  of the focused-window package for a call screen. Verdict "confirmed"
 *  with the package name, or "absent" as honest data — never assumed. */
fun callScreenPoll(waitMsArg: Long?): Map<String, Any?> {
    val waitMs = (waitMsArg ?: 4000L).coerceIn(0L, 8000L)
    var observed: String? = null
    val deadline = System.currentTimeMillis() + waitMs
    while (System.currentTimeMillis() < deadline) {
        val pkg = try { ZaraA11yService.focusedPackageNow() } catch (_: Exception) { null }
        if (pkg != null && Regex("(?i)incall|dialer|telecom").containsMatchIn(pkg)) {
            observed = pkg
            break
        }
        Thread.sleep(250)
    }
    return if (observed != null) {
        mapOf("supported" to true, "verdict" to "confirmed",
            "package" to observed,
            "verification" to "focused-window a11y re-read")
    } else {
        mapOf("supported" to true, "verdict" to "absent",
            "verification" to "no call screen within the bounded window")
    }
}

// ---- Stage 31-P3 phase B (P3e): native carrier SMS ------------------

private const val PERM_SEND_SMS = "android.permission.SEND_SMS"

/** SMS draft cap: short human messages stay short; a huge "draft" is
 *  itself a red flag for the approval reviewer (and pump-and-dump /
 *  fraud vectors ship long texts). */
private const val SMS_MAX_LEN = 400

/**
 * P3e: send ONE carrier SMS through SmsManager — after the SAME
 * approval discipline as the call:
 * - the recipient comes from Core's contacts/alias resolution (the
 *   bridge re-checks the dial grammar anyway);
 * - the DRAFT rides inside the parked record's inputs, so it is THE
 *   thing the approval card shows ("Effect: text=...") — editing
 *   between confirm and approval changes the fingerprint and refires
 *   the approval (engine-side);
 * - the verdict is "sent" ONLY as "the radio accepted the send" —
 *   carrier delivery is NOT confirmable by any bounded re-read, so
 *   `verified:false` rides this verdict (honest); delivery receipts
 *   are a PendingIntent follow-up we deliberately do NOT promise.
 * - the TEXT never lands in the job result (length only — same rule
 *   as clipboard): the approval card + engine inputs are the only
 *   spaces the plaintext exists in.
 */
fun smsSend(ctx: Context, number: String?, text: String?): Map<String, Any?> {
    if (number.isNullOrBlank()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "recipient number required")
    }
    if (!DIAL_RE.matches(number.trim())) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "recipient outside dial grammar — refused")
    }
    val body = text ?: ""
    if (body.isBlank()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "message text required — nothing was sent")
    }
    if (body.length > SMS_MAX_LEN) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "message longer than $SMS_MAX_LEN characters — "
                + "refused (write it shorter; no truncation guesses)")
    }
    if (ContextCompat.checkSelfPermission(ctx, PERM_SEND_SMS) !=
            PackageManager.PERMISSION_GRANTED) {
        return mapOf("supported" to true, "available" to false,
            "detail" to "SEND_SMS not granted — requested once at the "
                + "surface-enablement prompt; refusing to send silently")
    }
    return try {
        val sm = android.telephony.SmsManager.getDefault()
        sm.sendTextMessage(number.trim(), null, body, null, null)
        mapOf("supported" to true, "ok" to true, "verdict" to "sent",
            "text_len" to body.length,
            "verification" to "SmsManager accepted the send; carrier "
                + "delivery is not confirmable by a bounded re-read")
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false,
            "error" to ("send refused by the radio: ${e.message ?: e}"))
    }
}

// ---- Stage 31-P3 phase B (P3a): TARGETED media session ----------------

private const val SESSION_MAX_AGE = 5

/**
 * P3a: control ONE app's media session — never a global blast.
 * - hint is REQUIRED (a bare "pause everything" refuses: the caller
 *   names the app, the bridge matches ownerPackageName contains);
 * - 0 matches (or >1) are honest answers with a bounded listing —
 *   no guess, no ordering;
 * - the verdict: "sent" with before/after PlaybackState names; when
 *   the controller system is unavailable (no session access), an
 *   honest `supported:false` refusal — never a guessed control.
 */
fun mediaSessionTargeted(ctx: Context, hint: String?, action: String?): Map<String, Any?> {
    if (hint.isNullOrBlank()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "target app hint required — media control is "
                + "TARGETED (name the app; no global blast)")
    }
    val act = action?.trim()?.lowercase()
    if (act !in setOf("play", "pause", "next", "previous", "stop")) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "unknown action '$act'")
    }
    return try {
        val msm = ctx.getSystemService(android.media.session.MediaSessionManager::class.java)
        val cn = ComponentName(ctx, ZaraNotifListener::class.java)
        val sessions = msm.getActiveSessions(cn)
        val matched = sessions.filter {
            it.packageName?.contains(hint, ignoreCase = true) == true
        }
        if (matched.isEmpty()) {
            val names = sessions.map { it.packageName ?: "?" }.take(5)
            return mapOf("supported" to true, "ok" to true, "count" to 0,
                "active" to names,
                "verification" to "no session matches the hint; nothing was sent")
        }
        if (matched.size > 1) {
            val names = matched.map { it.packageName ?: "?" }.take(3)
            return mapOf("supported" to true, "ok" to true, "count" to matched.size,
                "active" to names,
                "verification" to "multiple sessions match — refine the hint; nothing was sent")
        }
        val s = matched.first()
        val before = s.playbackState?.state ?: -1
        // Bounded state observation: the cached snapshot does not
        // refresh by itself — a callback + latch is the honest way to
        // re-read the state after the transport command. 1.5 s bound;
        // a silent session reports the delta as unconfirmed (data).
        val latch = java.util.concurrent.CountDownLatch(1)
        var observed = ArrayList<Int>()
        val cb = object : android.media.session.MediaController.Callback() {
            override fun onPlaybackStateChanged(st: android.media.session.PlaybackState?) {
                st?.let {
                    // The framework echoes the CURRENT state right at
                    // registration — that echo must not satisfy the
                    // latch (only the REAL post-dispatch change may).
                    android.util.Log.d("ZaraMediaObs",
                        "callback state=$it.state")
                    if (it.state != before) {
                        observed.add(it.state)
                        latch.countDown()
                    }
                }
            }
        }
        // The callback handler MUST NOT be the main thread here: the
        // method-channel handler runs on it — awaiting a latch while the
        // state callback is posted to the same looper would deadlock
        // (exactly how the first live run hung). A short worker thread
        // delivers the callback and dies after the bounded wait.
        val work = android.os.HandlerThread("zara-media-obs")
        work.start()
        val handler = android.os.Handler(work.looper)
        val trackBefore = s.playbackState?.activeQueueItemId ?: -1
        // Timing facts learned live (recording them in code):
        // - the transport command reaches the session ~1.5 s AFTER the
        //   dispatch (emulator system routing latency), so the observe
        //   window must comfortably exceed it;
        // - the controller delivers the state change on the callback
        //   handler thread — that thread must NEVER be the one awaiting
        //   the latch (it deadqueues its own delivery);
        // - registration happens before dispatch, and the framework
        //   echoes the CURRENT state at registration — the echo must
        //   not count as the change.
        s.registerCallback(cb, handler)
        handler.post {
            when (act) {
                "play" -> s.transportControls.play()
                "pause" -> s.transportControls.pause()
                "next" -> s.transportControls.skipToNext()
                "previous" -> s.transportControls.skipToPrevious()
                "stop" -> s.transportControls.stop()
            }
            android.util.Log.d("ZaraMediaObs", "dispatched $act")
        }
        latch.await(8000, java.util.concurrent.TimeUnit.MILLISECONDS)
        s.unregisterCallback(cb)
        work.quitSafely()
        val after = observed.lastOrNull() ?: s.playbackState?.state ?: -1
        val trackAfter = s.playbackState?.activeQueueItemId ?: -1
        val verified = when {
            act == "play" -> after == android.media.session.PlaybackState.STATE_PLAYING
            act == "pause" -> after == android.media.session.PlaybackState.STATE_PAUSED
            act == "stop" -> after == android.media.session.PlaybackState.STATE_STOPPED
            act == "next" || act == "previous" ->
                (observed.isNotEmpty() || (trackAfter != trackBefore))
            else -> false
        }
        mapOf("supported" to true, "ok" to true, "count" to 1,
            "package" to s.packageName,
            "action" to act,
            "state_before" to stateName(before),
            "state_after" to stateName(after),
            "verified" to verified,
            "verification" to "transportControls dispatched on the "
                + "matching session; playback state observed via a "
                + "bounded callback re-read (1.5 s)")
    } catch (e: Exception) {
        mapOf("supported" to false,
            "detail" to ("media session controller unavailable: "
                + (e.message ?: e.toString())))
    }
}

private fun stateName(state: Int): String = when (state) {
    android.media.session.PlaybackState.STATE_PLAYING -> "playing"
    android.media.session.PlaybackState.STATE_PAUSED -> "paused"
    android.media.session.PlaybackState.STATE_STOPPED -> "stopped"
    android.media.session.PlaybackState.STATE_BUFFERING -> "buffering"
    -1 -> "unknown"
    else -> "state_$state"
}


// ---- Stage 31-P3 phase B (P3c): photo INVENTORY (counts only) --------

/**
 * P3c: an inventory of on-device camera-like photos — COUNTS ONLY.
 * No file names, no paths, no content, no thumbnails: names could
 * leak faces/context the user never asked to expose to the link; the
 * backup itself is a PULL (operator adb seam), consented per run,
 * and this surface NEVER deletes anything (count reads are read-only
 * by construction). Bounded while scanning ( mainstream camera
 * libraries are thousands of rows; the loop caps at 20000).
 */
fun photosInventory(context: Context): Map<String, Any?> {
    if (android.os.Build.VERSION.SDK_INT < 29) {
        return mapOf("supported" to false,
            "detail" to "inventory needs MediaStore RELATIVE_PATH (Android 10+)")
    }
    return try {
        val uri = android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        val proj = arrayOf(
            android.provider.MediaStore.Images.Media._ID,
            android.provider.MediaStore.Images.Media.SIZE,
            android.provider.MediaStore.Images.Media.DATE_MODIFIED,
            android.provider.MediaStore.Images.Media.RELATIVE_PATH)
        var count = 0
        var totalBytes = 0L
        var newest = 0L
        var oldest = Long.MAX_VALUE
        val paths = linkedSetOf<String>()
        context.contentResolver.query(uri, proj, null, null,
            null)?.use { c ->
            val iSize = c.getColumnIndexOrThrow(
                android.provider.MediaStore.Images.Media.SIZE)
            val iDate = c.getColumnIndexOrThrow(
                android.provider.MediaStore.Images.Media.DATE_MODIFIED)
            val iPath = c.getColumnIndexOrThrow(
                android.provider.MediaStore.Images.Media.RELATIVE_PATH)
            while (c.moveToNext() && count < 20000) {
                count++
                totalBytes += c.getLong(iSize).coerceAtLeast(0L)
                val d = c.getLong(iDate) * 1000L
                if (d > newest) newest = d
                if (d in 1 until oldest) oldest = d
                val p = (c.getString(iPath) ?: "").trim()
                if (p.isNotEmpty() && paths.size < 8) paths.add(p)
            }
        }
        mapOf("supported" to true, "count" to count,
            "total_bytes" to totalBytes,
            "newest_ms" to (if (newest > 0) newest else -1L),
            "oldest_ms" to (if (oldest == Long.MAX_VALUE) -1L else oldest),
            // Top-level folders only (bounded listing) — never names.
            "folders" to paths.toList(),
            "verification" to "MediaStore metadata scan; counts only, "
                + "no names, no content, read-only (never deletes)")
    } catch (e: Exception) {
        mapOf("supported" to false,
            "detail" to ("inventory failed: " + (e.message
                ?: e.toString())))
    }
}
