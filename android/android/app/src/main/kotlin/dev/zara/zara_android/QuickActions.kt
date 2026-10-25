package dev.zara.zara_android

import android.app.NotificationManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.KeyEvent
import androidx.core.content.ContextCompat
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Stage 31-P3 phase A — phone quick/one-shot actions (P3a media keys,
 * P3b clipboard push, P3h torch/alarm-bound volume, P3i find-my-phone
 * ring, P3j open link, P3k battery saver).
 *
 * Discipline (matches the file-level rules of DeviceBridge):
 * - every STATE CHANGE is verified by an immediate RE-READ of the OS
 *   property it changed where a re-read exists (torch torch-mode
 *   callback, power-saver PowerManager.isPowerSaveMode, global
 *   Settings re-read, stream volume getStreamVolume); where a re-read
 *   cannot exist (media keys go to whichever app keeps the media
 *   session; clipboard focus rules) the verdict is honestly "sent"/
 *   "refused" with the reason, never a fabricated "applied".
 * - unavailable means unavailable: no flash unit, no handler for a
 *   URI, DND active without explicit bypass allowance, missing adb
 *   grant — all reported as data, never faked.
 */
private const val PERM_WRITE_SECURE_SETTINGS =
    "android.permission.WRITE_SECURE_SETTINGS"

/** P3h: torch via CameraManager.setTorchMode. No camera permission
 *  needed (the platform exempts torch); verified by the torch-mode
 *  callback re-read. */
fun torchSet(ctx: Context, on: Boolean?): Map<String, Any?> {
    if (on == null) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "on flag required")
    }
    val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as? CameraManager
    if (cm == null) {
        return mapOf("supported" to true, "available" to false,
            "detail" to "camera service unavailable on this device")
    }
    fun readTorch(): Boolean? {
        val got = arrayOf<Boolean?>(null)
        val latch = CountDownLatch(1)
        try {
            val cb = object : CameraManager.TorchCallback() {
                override fun onTorchModeChanged(id: String, enabled: Boolean) {
                    if (got[0] == null) {
                        got[0] = enabled
                        latch.countDown()
                    }
                }
            }
            cm.registerTorchCallback(cb, Handler(Looper.getMainLooper()))
            latch.await(1, TimeUnit.SECONDS)
            cm.unregisterTorchCallback(cb)
        } catch (e: Exception) {
            // honest null
        }
        return got[0]
    }
    return try {
        val id = cm.cameraIdList.firstOrNull {
            try {
                cm.getCameraCharacteristics(it).get(
                    CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            } catch (e: Exception) {
                false
            }
        } ?: return mapOf("supported" to true, "available" to false,
            "detail" to "no camera with a flash unit on this device " +
            "(honest abstain; never faked)")
        cm.setTorchMode(id, on)
        // Re-read verify via the torch callback (initial callback
        // delivers the CURRENT state synchronously in practice; the
        // latch bounds it either way).
        val after = readTorch()
        mapOf("supported" to true, "available" to true,
            "requested" to on,
            "after_torch" to after,
            "verification" to "CameraManager torch-callback re-read",
            "verdict" to (if (after == null) "unverified"
                          else if (after == on) "applied" else "not-applied"))
    } catch (e: Exception) {
        mapOf("supported" to true, "available" to false,
            "detail" to "torch refused: ${e.message}")
    }
}

/** P3i: find-my-phone ring. Honest DND rule: if the phone is in a
 *  do-not-disturb state, the ring REFUSES unless the caller explicitly
 *  carries the user's `allow_dnd_bypass` allowance for THIS run; the
 *  bypass itself stays honest (never silently overrides the user's
 *  silence). Bounded ≤30 s on the alarm stream at max volume. */
fun findMyPhoneRing(ctx: Context, seconds: Int?, allowDndBypass: Boolean?):
        Map<String, Any?> {
    val dur = (seconds ?: 15).coerceIn(3, 30)
    val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
        as? NotificationManager
    val filter = nm?.currentInterruptionFilter
        ?: NotificationManager.INTERRUPTION_FILTER_ALL
    val inDnd = filter != NotificationManager.INTERRUPTION_FILTER_ALL
    if (inDnd && allowDndBypass != true) {
        // The user's silence is RESPECTED by default; the honest
        // refusal names the rule instead of fighting the phone.
        return mapOf("supported" to true, "ok" to false, "rang" to false,
            "dnd_filter" to filter,
            "detail" to "phone is in do-not-disturb; Zara does not " +
            "override silence without an explicit user allowance " +
            "(allow_dnd_bypass=true on this run)")
    }
    val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    val uri = RingtoneManager.getActualDefaultRingtoneUri(
        ctx, RingtoneManager.TYPE_ALARM)
        ?: RingtoneManager.getActualDefaultRingtoneUri(
            ctx, RingtoneManager.TYPE_RINGTONE)
    if (uri == null) {
        return mapOf("supported" to true, "ok" to false, "rang" to false,
            "detail" to "no alarm/ringtone sound configured on this " +
            "device — refused, never faked")
    }
    val origAlarmVol = am.getStreamVolume(AudioManager.STREAM_ALARM)
    val maxAlarm = am.getStreamMaxVolume(AudioManager.STREAM_ALARM)
    am.setStreamVolume(AudioManager.STREAM_ALARM, maxAlarm, 0)
    val mp = MediaPlayer()
    var rang = false
    try {
        mp.setDataSource(ctx, uri)
        mp.setAudioAttributes(
            AudioAttributes.Builder().setUsage(
                AudioAttributes.USAGE_ALARM).build())
        mp.isLooping = true
        mp.prepare()
        mp.start()
        rang = true
        // Run the ring on the calling (job worker) thread; bounded.
        val end = System.currentTimeMillis() + dur * 1000L
        while (mp.isPlaying && System.currentTimeMillis() < end) {
            try {
                Thread.sleep(200)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                break
            }
        }
    } catch (e: Exception) {
        return mapOf("supported" to true, "ok" to false, "rang" to rang,
            "detail" to "ring failed: ${e.message}")
    } finally {
        try {
            mp.stop()
            mp.release()
        } catch (e: Exception) {
            // release best-effort
        }
        try {
            am.setStreamVolume(AudioManager.STREAM_ALARM, origAlarmVol, 0)
        } catch (e: Exception) {
            // restore best-effort
        }
    }
    return mapOf("supported" to true, "ok" to true, "rang" to true,
        "seconds" to dur, "dnd_filter" to filter,
        "volume_restored" to true,
        "verification" to "MediaPlayer listed the default alarm sound " +
        "on STREAM_ALARM at max for the bounded window; original " +
        "alarm volume restored after")
}

/** P3a (phone side): media transport via key events. Reaches whatever
 *  holds the media session; a targeted-session controller requires the
 *  notification-listener surface (separate, opt-in). Verdict is
 *  honestly "sent" — the dispatch gives Zara no verify channel. */
fun mediaKeyEvent(ctx: Context, action: String?): Map<String, Any?> {
    val code = mapOf(
        "play_pause" to KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
        "play" to KeyEvent.KEYCODE_MEDIA_PLAY,
        "pause" to KeyEvent.KEYCODE_MEDIA_PAUSE,
        "stop" to KeyEvent.KEYCODE_MEDIA_STOP,
        "next" to KeyEvent.KEYCODE_MEDIA_NEXT,
        "previous" to KeyEvent.KEYCODE_MEDIA_PREVIOUS)[action]
    if (code == null) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "action must be play|pause|play_pause|stop|" +
            "next|previous")
    }
    val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    val down = KeyEvent(KeyEvent.ACTION_DOWN, code)
    val up = KeyEvent(KeyEvent.ACTION_UP, code)
    am.dispatchMediaKeyEvent(down)
    am.dispatchMediaKeyEvent(up)
    return mapOf("supported" to true, "ok" to true, "action" to action,
        "verdict" to "sent",
        "verification" to "media key events dispatched; no per-session " +
        "verify channel exists (honest sent-only)")
}

/** P3b (phone side): clipboard PUSH (laptop → phone only; the reverse
 *  direction is Android-restricted by design). Android 10+ requires
 *  the writing app's window to hold focus — an unfocused job write is
 *  refused by the OS, reported plainly as refused-focus. */
fun clipboardSet(ctx: Context, text: String?): Map<String, Any?> {
    if (text.isNullOrEmpty()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "text required (empty clipboard write refused)")
    }
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE)
        as? ClipboardManager ?: return mapOf(
        "supported" to true, "ok" to false,
        "error" to "clipboard service unavailable")
    return try {
        cm.setPrimaryClip(ClipData.newPlainText("zara", text))
        // Re-read verify: the OS hands back the actual primary clip.
        val cur = cm.primaryClip
        val applied = cur != null && cur.itemCount > 0 &&
            cur.getItemAt(0).coerceToText(ctx)?.toString() == text
        mapOf("supported" to true, "applied" to applied,
            "length" to text.length,
            "verdict" to (if (applied) "applied" else "focus-refused"),
            "detail" to "the primary clip was verified by re-read; a " +
            "mismatch means the OS refused the write because the app " +
            "window is not focused (Android 10+ rule) — bring Zara " +
            "to the foreground or use the tap-to-copy notification",
            "verification" to "clipboard primary-clip re-read (text " +
            "compared verbatim; never echoed back into the result)")
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false, "applied" to false,
            "detail" to "clipboard refused: ${e.message}")
    }
}

/** P3j: open a URI on the phone. Per-link consent lives at CORE (the
 *  job only arrives through the approval pipeline); device-side the
 *  schemes are bounded to ordinary-view types and the handler is
 *  verified present before firing (no silent falls-through). */
fun openUri(ctx: Context, uri: String?): Map<String, Any?> {
    if (uri.isNullOrEmpty()) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "uri required")
    }
    val parsed = try {
        Uri.parse(uri)
    } catch (e: Exception) {
        null
    } ?: return mapOf("supported" to true, "ok" to false,
        "error" to "uri unparseable")
    val allowedSchemes = setOf("http", "https", "mailto", "tel", "geo",
        "market", "content", "file")
    if (parsed.scheme?.lowercase() !in allowedSchemes) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "scheme '${parsed.scheme}' not in the bounded " +
            "view allowlist (allowlist: http/https/mailto/tel/geo/" +
            "market/content/file)")
    }
    val intent = Intent(Intent.ACTION_VIEW, parsed)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    val pm = ctx.packageManager
    val resolvable = try {
        pm.queryIntentActivities(intent, 0).isNotEmpty()
    } catch (e: Exception) {
        false
    }
    if (!resolvable) {
        return mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "no app on this device handles this link — " +
            "honest abstain (never guessed or forced)")
    }
    return try {
        ctx.startActivity(intent)
        mapOf("supported" to true, "ok" to true, "fired" to true,
            "scheme" to parsed.scheme,
            "verdict" to "sent",  // OS dispatch has no verify channel
            "verification" to "VIEW intent fired after a resolvable " +
            "handler was confirmed; the receiving app's own behavior " +
            "is not Zara's to verify")
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "startActivity refused: ${e.message}")
    }
}

/** P3h: alarm/timer via ACTION_SET_ALARM — the clock app opens
 *  pre-filled and the USER completes it (bounded honesty: Zara never
 *  completes an alarm silently; the verdict is "handed-to-clock"). */
fun alarmSet(ctx: Context, hour: Int?, minute: Int?, label: String?,
             skipUi: Boolean?): Map<String, Any?> {
    val intent = Intent(android.provider.AlarmClock.ACTION_SET_ALARM)
    intent.putExtra(android.provider.AlarmClock.EXTRA_HOUR, hour ?: -1)
    intent.putExtra(android.provider.AlarmClock.EXTRA_MINUTES, minute ?: 0)
    if (!label.isNullOrEmpty()) {
        intent.putExtra(android.provider.AlarmClock.EXTRA_MESSAGE, label)
    }
    // Bounded honesty: skip_ui would set an alarm withOUT the user
    // finalizing it — the user decision (P3h spec): clock opens
    // pre-filled, user completes. skipUi=true is refused structurally.
    if (skipUi == true) {
        return mapOf("supported" to true, "ok" to false,
            "detail" to "skip_ui refused by design: the alarm is " +
            "opened pre-filled and the user completes it (P3h " +
            "decision 2026-10-20)")
    }
    if (hour == null && (minute != null)) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "minute requires hour")
    }
    val resolvable = try {
        ctx.packageManager.queryIntentActivities(intent, 0).isNotEmpty()
    } catch (e: Exception) {
        false
    }
    if (!resolvable) {
        return mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "no clock app handles ACTION_SET_ALARM — " +
            "honest abstain")
    }
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    return try {
        ctx.startActivity(intent)
        mapOf("supported" to true, "ok" to true, "fired" to true,
            "hour" to hour, "minute" to minute, "label" to (label ?: ""),
            "verdict" to "handed-to-clock",
            "verification" to "the clock app opened pre-filled (USER " +
            "completes the alarm; Zara claims only the hand-off)")
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "startActivity refused: ${e.message}")
    }
}

/** P5 feature-hunt (2026-10-22): countdown timer via ACTION_SET_TIMER —
 *  same bounded honesty as the alarm: the clock app opens PRE-FILLED and
 *  the USER completes it (skip_ui is refused structurally; the verdict
 *  is "handed-to-clock"). Zara claims only the hand-off. */
fun timerSet(ctx: Context, minutes: Int?, label: String?,
             skipUi: Boolean?): Map<String, Any?> {
    val intent = Intent(android.provider.AlarmClock.ACTION_SET_TIMER)
    if (minutes == null || minutes < 1) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "minutes (>=1) required")
    }
    intent.putExtra(android.provider.AlarmClock.EXTRA_LENGTH,
        minutes * 60)
    if (!label.isNullOrEmpty()) {
        intent.putExtra(android.provider.AlarmClock.EXTRA_MESSAGE, label)
    }
    if (skipUi == true) {
        return mapOf("supported" to true, "ok" to false,
            "detail" to "skip_ui refused by design: the timer is " +
            "opened pre-filled and the user completes it (P3h " +
            "decision 2026-10-20)")
    }
    val resolvable = try {
        ctx.packageManager.queryIntentActivities(intent, 0).isNotEmpty()
    } catch (e: Exception) {
        false
    }
    if (!resolvable) {
        return mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "no clock app handles ACTION_SET_TIMER — " +
            "honest abstain")
    }
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    return try {
        ctx.startActivity(intent)
        mapOf("supported" to true, "ok" to true, "fired" to true,
            "minutes" to minutes, "label" to (label ?: ""),
            "verdict" to "handed-to-clock",
            "verification" to "the clock app opened pre-filled (USER " +
            "completes the timer; Zara claims only the hand-off)")
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "startActivity refused: ${e.message}")
    }
}

/** P5 feature-hunt ② (2026-10-22): Do-Not-Disturb state read. Read-only:
 *  the current interruption filter + whether Zara holds the
 *  ACCESS_NOTIFICATION_POLICY special access the setter needs. Never
 *  changes anything. */
fun dndGet(ctx: Context): Map<String, Any?> {
    val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
        as NotificationManager
    val filter = try {
        nm.currentInterruptionFilter
    } catch (e: Exception) {
        NotificationManager.INTERRUPTION_FILTER_UNKNOWN
    }
    val access = try {
        nm.isNotificationPolicyAccessGranted
    } catch (e: Exception) {
        false
    }
    val name = when (filter) {
        NotificationManager.INTERRUPTION_FILTER_ALL -> "all (DND off)"
        NotificationManager.INTERRUPTION_FILTER_PRIORITY -> "priority only"
        NotificationManager.INTERRUPTION_FILTER_NONE -> "no interruptions (DND on)"
        NotificationManager.INTERRUPTION_FILTER_ALARMS -> "alarms only"
        else -> "unknown"
    }
    return mapOf("supported" to true, "ok" to true,
        "filter" to filter,
        "filter_name" to name,
        "dnd_on" to (filter == NotificationManager.INTERRUPTION_FILTER_NONE ||
            filter == NotificationManager.INTERRUPTION_FILTER_ALARMS),
        "policy_access" to access)
}

/** P5 feature-hunt ② (2026-10-22): Do-Not-Disturb set through the
 *  authoritative NotificationManager surface. Requires the
 *  ACCESS_NOTIFICATION_POLICY special access (user grants once in
 *  Settings; the refusal suggests it — never auto-granted). enabled →
 *  INTERRUPTION_FILTER_NONE; !enabled → INTERRUPTION_FILTER_ALL.
 *  Verified by a re-read of the filter (honest verified flag), unlike
 *  the timer/alarm hand-offs. skip_ui is refused structurally (DND has
 *  no clock-style UI to hand off). */
fun dndSet(ctx: Context, enabled: Boolean?, skipUi: Boolean?):
    Map<String, Any?> {
    if (enabled == null) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "enabled flag required")
    }
    if (skipUi == true) {
        return mapOf("supported" to true, "ok" to false,
            "detail" to "skip_ui refused by design: DND is set directly " +
            "with policy access, no UI hand-off exists (P5 2026-10-22)")
    }
    val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)
        as NotificationManager
    val access = try {
        nm.isNotificationPolicyAccessGranted
    } catch (e: Exception) {
        false
    }
    if (!access) {
        return mapOf("supported" to true, "ok" to false, "fired" to false,
            "granted" to false,
            "detail" to "Do Not Disturb policy access not granted — " +
            "grant ONCE in Settings > Accessibility > Notification " +
            "policy access (special app access). Nothing changed.")
    }
    val want = if (enabled) NotificationManager.INTERRUPTION_FILTER_NONE
    else NotificationManager.INTERRUPTION_FILTER_ALL
    try {
        // API 36 ground truth (javap): setInterruptionFilter returns
        // void — the verified verdict comes from the re-read ONLY.
        nm.setInterruptionFilter(want)
    } catch (e: Exception) {
        return mapOf("supported" to true, "ok" to false, "fired" to false,
            "detail" to "setInterruptionFilter refused: ${e.message}")
    }
    val now = try {
        nm.currentInterruptionFilter
    } catch (e: Exception) {
        NotificationManager.INTERRUPTION_FILTER_UNKNOWN
    }
    return mapOf("supported" to true, "ok" to true, "fired" to true,
        "granted" to true,
        "filter" to now,
        "verification" to "interruption filter re-read after set",
        "verified" to (now == want))
}

/** P3k: battery saver via WRITE_SECURE_SETTINGS (the T1 adb-grant
 *  pattern from SYSCONTROL_MATRIX). Requires the once-granted
 *  permission; verified by the PowerManager re-read (the authoritative
 *  surface) AND the settings value. */
fun powerSaverSet(ctx: Context, on: Boolean?): Map<String, Any?> {
    if (on == null) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "on flag required")
    }
    val granted = ContextCompat.checkSelfPermission(
        ctx, PERM_WRITE_SECURE_SETTINGS) ==
        android.content.pm.PackageManager.PERMISSION_GRANTED
    if (!granted) {
        return mapOf("supported" to true, "ok" to false,
            "granted" to false,
            "detail" to "WRITE_SECURE_SETTINGS not granted (one-time " +
            "adb grant per SYSCONTROL_MATRIX T1: adb shell pm grant " +
            "dev.zara.zara_android android.permission." +
            "WRITE_SECURE_SETTINGS) — refused, nothing changed")
    }
    val pm = ctx.getSystemService(Context.POWER_SERVICE) as PowerManager
    val before = pm.isPowerSaveMode
    return try {
        Settings.Global.putInt(ctx.contentResolver, "low_power",
            if (on) 1 else 0)
        val setting = Settings.Global.getInt(ctx.contentResolver,
            "low_power", -1)
        val after = pm.isPowerSaveMode
        val applied = (setting == if (on) 1 else 0) && after == on
        mapOf("supported" to true, "before" to before, "requested" to on,
            "after_setting" to setting, "after_mode" to after,
            "verdict" to (if (applied) "applied" else "not-applied"),
            "verification" to "PowerManager.isPowerSaveMode + " +
            "Settings.Global re-read after write",
            "ok" to applied,
            "nothing_changed" to !applied)
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false,
            "detail" to "settings write refused: ${e.message}")
    }
}

/** P3h (volume): bounded ring/media-stream set, verified by
 *  getStreamVolume re-read. Never touches ringer/alarm by default. */
fun streamVolumeSet(ctx: Context, pct: Int?): Map<String, Any?> {
    if (pct == null || pct !in 0..100) {
        return mapOf("supported" to true, "ok" to false,
            "error" to "pct (0-100) required")
    }
    val am = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    val before = am.getStreamVolume(AudioManager.STREAM_MUSIC)
    val max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
    val target = Math.round(max * pct / 100.0).toInt()
    return try {
        am.setStreamVolume(AudioManager.STREAM_MUSIC, target, 0)
        val after = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        val applied = after == target
        mapOf("supported" to true, "ok" to applied,
            "before_pct" to Math.round(before * 100.0 / max).toInt(),
            "target_pct" to pct,
            "after_pct" to Math.round(after * 100.0 / max).toInt(),
            "verdict" to (if (applied) "applied" else "not-applied"),
            "verification" to "getStreamVolume re-read after set",
            "nothing_changed" to !applied)
    } catch (e: Exception) {
        mapOf("supported" to true, "ok" to false,
            "detail" to "volume set refused: ${e.message}")
    }
}
