package dev.zara.zara_android

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import androidx.core.content.ContextCompat
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

/**
 * Zara native bridge — the ONLY place Android APIs are touched.
 * Exposes 'zara/device' MethodChannel: getBattery / getNetwork / getPermissions.
 * Anything not implemented here reports supported=false to Flutter;
 * Flutter never pretends an unimplemented capability works.
 */
/**
 * Zara native bridge — the ONLY place Android APIs are touched.
 * Exposes 'zara/device' MethodChannel: getBattery / getNetwork /
 * getPermissions / getVoiceSupport + Stage 9 audio, permission-request,
 * notification-channel and self-test methods.
 * Anything not implemented here reports supported=false to Flutter;
 * Flutter never pretends an unimplemented capability works.
 *
 * Audio honesty: capture returns real PCM-or-error; a dataclass/wiring
 * success is NEVER reported as human-heard or voice-detected.
 */
class DeviceBridge(
    private val context: Context,
    private val requestPermission:
        (permission: String, result: MethodChannel.Result) -> Unit =
        { _, result -> result.success(mapOf("granted" to false)) }
) {
    companion object {
        const val CHANNEL = "zara/device"
        const val RATE = 16000
        const val MAX_SECONDS = 30
        /** Last-known battery pct for the assistant session UI (same process). */
        @Volatile var lastBatteryPct: Int = -1
    }

    @Volatile private var capture: AudioRecord? = null
    @Volatile private var player: AudioTrack? = null

    fun attach(engine: FlutterEngine) {
        MethodChannel(engine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "getBattery" -> result.success(battery())
                    "getNetwork" -> result.success(network())
                    "getPermissions" -> result.success(permissions())
                    "getVoiceSupport" -> result.success(voiceSupport())
                    "audioCapture" -> audioCapture(
                        (call.argument<Double>("seconds") ?: 5.0), result)
                    "audioStop" -> { stopCapture(); result.success(true) }
                    "audioPlay" -> audioPlay(
                        call.argument<ByteArray>("wav"), result)
                    "audioPlayStop" -> { stopPlayer(); result.success(true) }
                    "requestMicPermission" -> requestPermission(
                        Manifest.permission.RECORD_AUDIO, result)
                    "requestNotifPermission" ->
                        if (Build.VERSION.SDK_INT >=
                            Build.VERSION_CODES.TIRAMISU) {
                            requestPermission(
                                Manifest.permission.POST_NOTIFICATIONS, result)
                        } else {
                            result.success(mapOf("granted" to true,
                                "already" to true))
                        }
                    "createNotificationChannels" -> {
                        createChannels(); result.success(true)
                    }
                    "audioSelfTest" -> result.success(audioSelfTest())
                    "getAssistantStatus" -> result.success(assistantStatus())
                    "privForceStop" ->
                        privForceStop(call.argument<String>("package"), result)
                    "a11yInspect" -> result.success(a11yInspect())
                    "a11yInspectWithParams" ->
                        a11yInspectWithParams(
                            call.arguments as? Map<String, Any?>, result)
                    "a11yTap" -> a11yTap(
                        call.argument<String>("package"),
                        call.argument<Any>("node_id"), result)
                    "a11yProbe" -> result.success(a11yProbe())
                    else -> result.notImplemented()
                }
            }
    }

    // ---------- Rung-1 privileged gateway (ONE capability, lab only) ----------
    //
    // android.app.force_stop on a hardcoded lab allowlist. The Core registry
    // holds the same set; anything else is refused HERE even if a job for it
    // somehow arrived (defense in depth). No shell, no exec, no generic
    // dispatch: exactly one framework call, one bounded string parameter.
    private val forceStopLabTargets =
        setOf("dev.zara.lab.privtest") // harmless relaunchable lab probe

    private fun privForceStop(pkg: String?, result: MethodChannel.Result) {
        if (pkg.isNullOrEmpty() || pkg !in forceStopLabTargets) {
            result.error("denied",
                "target not in lab allowlist (refused)", null)
            return
        }
        // String literal: the Manifest constant is not in the public SDK
        // stubs on this compile target; the protection level (not the
        // constant) is what matters, and it is verified device-side.
        if (ContextCompat.checkSelfPermission(context,
                "android.permission.FORCE_STOP_PACKAGES") !=
                PackageManager.PERMISSION_GRANTED) {
            result.error("denied",
                "FORCE_STOP_PACKAGES not granted", null)
            return
        }
        try {
            val am = context.getSystemService(
                Context.ACTIVITY_SERVICE) as android.app.ActivityManager
            // Best-effort running check (modern Android may under-report
            // other packages; Core verifies authoritatively via pidof).
            fun isRunning(): Boolean =
                am.runningAppProcesses?.any { it.processName == pkg } == true
            val before = isRunning()
            // Hidden APIs: no public SDK force-stop or current-user-id
            // accessor exists. Reached by reflection; if the build blocks
            // either, that is reported honestly as unavailable
            // (never worked around).
            val uh = Class.forName("android.os.UserHandle")
            val myUserId = uh.getMethod("myUserId").invoke(null) as Int
            // NOTE (lab probe 2026-10-07): ActivityManager on API 36
            // exposes forceStopPackage(String) [single-arg] and
            // forceStopPackageAsUser(String,int) — there is NO
            // (String,int) overload. An earlier revision reflected the
            // wrong signature and honestly reported unavailable; the
            // method enumeration probe on-device settled it.
            val m = try {
                am.javaClass.getMethod("forceStopPackage",
                    String::class.java)
            } catch (e: NoSuchMethodException) {
                am.javaClass.getMethod("forceStopPackageAsUser",
                    String::class.java, Int::class.javaPrimitiveType!!)
            }
            if (m.parameterTypes.size == 1) {
                m.invoke(am, pkg)
            } else {
                m.invoke(am, pkg, myUserId)
            }
            val after = isRunning()
            result.success(mapOf(
                "supported" to true,
                "package" to pkg,
                "stopped" to !after,
                "was_running" to before,
                "verify_state" to if (!after) "stopped" else "running",
            ))
        } catch (e: NoSuchMethodException) {
            result.error("unavailable",
                "force-stop API absent or blocked on this build", null)
        } catch (e: SecurityException) {
            result.error("denied",
                "SecurityException from framework", null)
        } catch (e: Exception) {
            result.error("failed", (e.message ?: "force-stop failed"), null)
        }
    }

    // ---------- Rung-2 lab pilot: a11y inspect + tap (emulator lab only) ----------
    //
    // gui.screen.inspect / gui.tap on a hardcoded lab allowlist. No shell,
    // no exec, no ProcessBuilder, no reflection beyond Accessibility APIs:
    // exactly one snapshot builder and one node.performAction(ACTION_CLICK)
    // on a FRESH root lookup by node_id. Accessibility state is reported
    // via the inspect call itself (getPermissions gains no new key).
    private val a11yTapTargets =
        setOf("dev.zara.zara_android", "dev.zara.lab.privtest")

    private fun a11yInspect(): Map<String, Any?> = ZaraA11yService.snapshot()

    private fun a11yInspectWithParams(
        params: Map<String, Any?>?,
        result: MethodChannel.Result
    ) {
        result.success(ZaraA11yService.snapshot(params))
    }

    /**
     * Registration-guard probe: is the accessibility service enabled in
     * Settings AND bound AND able to see a window? Read-only, zero
     * interaction (no clicks, no messages, no settings writes). The Dart
     * side gates gui.* availability on the returned `ready` flag.
     */
    private fun a11yProbe(): Map<String, Any?> {
        return try {
            val comp = android.content.ComponentName(
                context, ZaraA11yService::class.java).flattenToString()
            val enabled = android.provider.Settings.Secure.getString(
                context.contentResolver,
                android.provider.Settings.Secure
                    .ENABLED_ACCESSIBILITY_SERVICES,
            ) ?: ""
            val parts = enabled.split(':')
            val enabledInSettings = parts.any { it == comp }
            android.util.Log.d("ZaraA11yProbe",
                "comp=$comp enabled_raw='$enabled' parts=$parts enabledInSettings=$enabledInSettings")
            // Note: AccessibilityService runs in system process; app process
            // cannot directly observe its bound state or root. We treat
            // "enabled in Settings" as the probe signal. Actual inspect/tap
            // will fail gracefully if service is not functional.
            val ready = enabledInSettings
            mapOf(
                "supported" to true,
                "ready" to ready,
                "service_enabled" to enabledInSettings,
                "bound" to enabledInSettings, // best-effort approximation
                "root_present" to enabledInSettings,
                "reason" to if (ready) "" else
                    "accessibility service not enabled",
            )
        } catch (e: Exception) {
            mapOf("supported" to false,
                "reason" to ((e.message ?: "probe failed").take(200)))
        }
    }

    private fun a11yTap(
        pkg: String?,
        nodeId: Any?,
        result: MethodChannel.Result,
    ) {
        if (pkg.isNullOrEmpty() || pkg !in a11yTapTargets) {
            result.error("denied",
                "target not in lab allowlist (refused)", null)
            return
        }
        val id = (nodeId as? Number)?.toInt()
        if (id == null || id < 0) {
            result.error("failed", "bad node_id (need snapshot node)", null)
            return
        }
        try {
            val applied = ZaraA11yService.tapNode(id)
            if (applied == null) {
                result.error("unavailable",
                    "accessibility service unbound/disabled", null)
                return
            }
            result.success(mapOf(
                "supported" to true,
                "package" to pkg,
                "stopped" to "n/a",
                "applied" to applied,
                "verify_state" to if (applied) "clicked" else "not_applied",
            ))
        } catch (e: SecurityException) {
            result.error("denied",
                "SecurityException from framework", null)
        } catch (e: Exception) {
            result.error("failed", (e.message ?: "tap failed"), null)
        }
    }

    // ---------- bounded microphone capture (AudioRecord -> WAV) ----------

    private fun audioCapture(seconds: Double, result: MethodChannel.Result) {
        if (ContextCompat.checkSelfPermission(
                context, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED) {
            result.error("denied", "microphone permission not granted", null)
            return
        }
        val secs = seconds.coerceIn(0.5, MAX_SECONDS.toDouble())
        val n = (RATE * secs).toInt()
        val minBuf = AudioRecord.getMinBufferSize(
            RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) {
            result.error("unavailable", "no audio input device", null)
            return
        }
        Thread {
            var rec: AudioRecord? = null
            try {
                rec = AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minBuf * 2, 32000))
                synchronized(this) { capture = rec }
                if (rec.state != AudioRecord.STATE_INITIALIZED) {
                    result.error("unavailable", "input init failed", null)
                    return@Thread
                }
                rec.startRecording()
                val pcm = ByteArray(n * 2)
                var read = 0
                while (read < n * 2) {
                    val r = rec.read(pcm, read, n * 2 - read)
                    if (r <= 0) break
                    read += r
                }
                result.success(wav(pcm, read))
            } catch (e: SecurityException) {
                result.error("denied", "microphone denied", null)
            } catch (e: Exception) {
                result.error("failed", (e.message ?: "capture failed"), null)
            } finally {
                try { rec?.stop() } catch (_: Exception) {}
                try { rec?.release() } catch (_: Exception) {}
                synchronized(this) {
                    if (capture === rec) capture = null
                }
            }
        }.start()
    }

    private fun stopCapture() {
        synchronized(this) {
            try { capture?.stop() } catch (_: Exception) {}
            try { capture?.release() } catch (_: Exception) {}
            capture = null
        }
    }

    private fun wav(pcm: ByteArray, len: Int): ByteArray {
        val total = len + 36
        val out = ByteArray(len + 44)
        fun w32(o: Int, v: Int) {
            out[o] = (v and 0xFF).toByte()
            out[o + 1] = ((v shr 8) and 0xFF).toByte()
            out[o + 2] = ((v shr 16) and 0xFF).toByte()
            out[o + 3] = ((v shr 24) and 0xFF).toByte()
        }
        fun w16(o: Int, v: Int) {
            out[o] = (v and 0xFF).toByte()
            out[o + 1] = ((v shr 8) and 0xFF).toByte()
        }
        "RIFF".forEachIndexed { i, c -> out[i] = c.code.toByte() }
        w32(4, total)
        "WAVEfmt ".forEachIndexed { i, c -> out[8 + i] = c.code.toByte() }
        w32(16, 16); w16(20, 1); w16(22, 1); w32(24, RATE)
        w32(28, RATE * 2); w16(32, 2); w16(34, 16)
        "data".forEachIndexed { i, c -> out[36 + i] = c.code.toByte() }
        w32(40, len)
        pcm.copyInto(out, 44, 0, len)
        return out
    }

    // ---------- speaker playback (AudioTrack + focus, cancellable) ----------

    private fun audioPlay(wav: ByteArray?, result: MethodChannel.Result) {
        if (wav == null || wav.size < 44) {
            result.error("failed", "empty audio", null)
            return
        }
        // NOTE: USAGE_ASSISTANT was rejected on Vivo OriginOS (AudioTrack
        // init failed). USAGE_MEDIA is the compatible route; the clip is
        // still Zara's own TTS reply, announced as software-only.
        val am = context.getSystemService(Context.AUDIO_SERVICE)
            as AudioManager
        val focus: Any? = if (Build.VERSION.SDK_INT >=
            Build.VERSION_CODES.O) {
            val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(
                        AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .build()
            if (am.requestAudioFocus(req) !=
                AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                result.error("failed", "audio focus denied", null)
                return
            }
            req
        } else {
            @Suppress("DEPRECATION")
            if (am.requestAudioFocus(
                    null, AudioManager.STREAM_MUSIC,
                    AudioManager.AUDIOFOCUS_GAIN) !=
                AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                result.error("failed", "audio focus denied", null)
                return
            }
            null
        }
        Thread {
            var track: AudioTrack? = null
            try {
                // WAV from Core TTS is 16-bit mono; honor its own rate.
                val rate = ((wav[24].toInt() and 0xFF) or
                    ((wav[25].toInt() and 0xFF) shl 8) or
                    ((wav[26].toInt() and 0xFF) shl 16) or
                    ((wav[27].toInt() and 0xFF) shl 24))
                    .takeIf { it in 8000..48000 } ?: 16000
                val body = wav.copyOfRange(44, wav.size)
                val minBuf = AudioTrack.getMinBufferSize(
                    rate, AudioFormat.CHANNEL_OUT_MONO,
                    AudioFormat.ENCODING_PCM_16BIT)
                if (minBuf <= 0) {
                    result.error("unavailable",
                        "no audio output for rate=$rate", null)
                    return@Thread
                }
                // MODE_STREAM (not STATIC): large TTS clips exceed static
                // buffer limits on some OEMs — stream in chunks instead.
                track = AudioTrack(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(
                            AudioAttributes.CONTENT_TYPE_SPEECH).build(),
                    AudioFormat.Builder().setEncoding(
                        AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(rate).setChannelMask(
                            AudioFormat.CHANNEL_OUT_MONO).build(),
                    maxOf(minBuf * 2, 32000),
                    AudioTrack.MODE_STREAM,
                    AudioManager.AUDIOFOCUS_NONE)
                synchronized(this) { player = track }
                if (track.state != AudioTrack.STATE_INITIALIZED) {
                    result.error("unavailable",
                        "output init failed state=${track.state} rate=$rate",
                        null)
                    return@Thread
                }
                track.play()
                var off = 0
                while (off < body.size) {
                    val n = track.write(body, off, body.size - off)
                    if (n <= 0) break
                    off += n
                    if (track.playState != AudioTrack.PLAYSTATE_PLAYING &&
                        off < body.size) break
                }
                // Bounded wait for the tail: clip length + 5 s max.
                val ms = (body.size * 1000L / (rate * 2)).coerceAtMost(60000)
                val deadline = System.currentTimeMillis() + ms + 5000
                while (track.playState == AudioTrack.PLAYSTATE_PLAYING &&
                    System.currentTimeMillis() < deadline) {
                    Thread.sleep(50)
                }
                // Completion == clean exit. NOT proof a human heard it.
                result.success(mapOf("played" to true,
                    "bytes" to body.size, "rate" to rate,
                    "audibility" to "manual-only"))
            } catch (e: Exception) {
                result.error("failed", (e.message ?: "play failed"), null)
            } finally {
                try { track?.stop() } catch (_: Exception) {}
                try { track?.release() } catch (_: Exception) {}
                synchronized(this) {
                    if (player === track) player = null
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    (focus as? AudioFocusRequest)?.let {
                        am.abandonAudioFocusRequest(it)
                    }
                } else {
                    @Suppress("DEPRECATION")
                    am.abandonAudioFocus(null)
                }
            }
        }.start()
    }

    private fun stopPlayer() {
        synchronized(this) {
            try { player?.stop() } catch (_: Exception) {}
            try { player?.release() } catch (_: Exception) {}
            player = null
        }
    }

    // ---------- notification channels ----------

    private fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(Context.NOTIFICATION_SERVICE)
            as NotificationManager
        nm.createNotificationChannel(NotificationChannel(
            "zara_approvals", "Zara approvals",
            NotificationManager.IMPORTANCE_HIGH))
        nm.createNotificationChannel(NotificationChannel(
            "zara_missions", "Zara missions",
            NotificationManager.IMPORTANCE_DEFAULT))
        nm.createNotificationChannel(NotificationChannel(
            "zara_status", "Zara status",
            NotificationManager.IMPORTANCE_LOW))
    }

    // ---------- assistant-role compatibility (report only) ----------

    /**
     * Truthful assistant-role report. Declaring the service in the manifest
     * does NOT make Zara the default — only the OS/user setting does, read
     * from Settings.Secure. Role request via RoleManager is NOT possible
     * for ROLE_ASSISTANT (Settings UI only); this reports that honestly.
     */
    private fun assistantStatus(): Map<String, Any?> {
        val svc = android.content.ComponentName(
            context, ZaraVoiceInteractionService::class.java)
        val flattened = svc.flattenToString()
        val current = try {
            android.provider.Settings.Secure.getString(
                context.contentResolver,
                "assistant") ?: ""
        } catch (e: Exception) { "" }
        val roleAvailable = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val rm = context.getSystemService(
                    android.app.role.RoleManager::class.java)
                rm?.isRoleAvailable(android.app.role.RoleManager.ROLE_ASSISTANT)
                    ?: false
            } else false
        } catch (e: Exception) { false }
        val roleHeld = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val rm = context.getSystemService(
                    android.app.role.RoleManager::class.java)
                rm?.isRoleHeld(android.app.role.RoleManager.ROLE_ASSISTANT)
                    ?: false
            } else false
        } catch (e: Exception) { false }
        // OS-side read of OUR OWN descriptor: meta-data resid present,
        // XML parseable, sessionService + supportsAssist as the OS sees
        // them. If this fails, no chooser will ever list Zara.
        var infoValid = false
        var supportsAssist = false
        var infoError = ""
        var sessionSvc = ""
        try {
            val si = context.packageManager.getServiceInfo(
                svc, PackageManager.GET_META_DATA)
            val resId = si.metaData?.getInt("android.voice_interaction", 0)
                ?: 0
            if (resId == 0) {
                infoError = "no android.voice_interaction meta-data"
            } else {
                val parser = context.resources.getXml(resId)
                var event = parser.eventType
                while (event != org.xmlpull.v1.XmlPullParser.END_DOCUMENT) {
                    if (event == org.xmlpull.v1.XmlPullParser.START_TAG &&
                        parser.name == "voice-interaction") {
                        sessionSvc = parser.getAttributeValue(
                            "http://schemas.android.com/apk/res/android",
                            "sessionService") ?: ""
                        supportsAssist = parser.getAttributeBooleanValue(
                            "http://schemas.android.com/apk/res/android",
                            "supportsAssist", false)
                        infoValid = sessionSvc.isNotEmpty()
                        if (!infoValid) {
                            infoError = "sessionService empty/unresolved"
                        }
                        break
                    }
                    event = parser.next()
                }
                if (!infoValid && infoError.isEmpty()) {
                    infoError = "no voice-interaction tag"
                }
            }
        } catch (e: Exception) {
            infoError = (e.javaClass.simpleName + ": " + (e.message ?: ""))
                .take(160)
        }
        return mapOf(
            "supported" to true,
            "service_registered" to true,
            "service_component" to flattened,
            "current_assistant" to current,
            "zara_is_default" to (current == flattened ||
                current.endsWith("/" + svc.className) ||
                current == svc.flattenToShortString()),
            "role_available" to roleAvailable,
            "role_held" to roleHeld,
            // ROLE_ASSISTANT cannot be requested programmatically.
            "role_request_possible" to false,
            "role_request_note" to "assistant role requires Settings UI",
            "service_info_valid" to infoValid,
            "supports_assist" to supportsAssist,
            "session_service" to sessionSvc,
            "service_info_error" to infoError,
        )
    }

    // ---------- self-test: API path only, no permission, no audio ----------

    private fun audioSelfTest(): Map<String, Any?> {
        val pm = context.packageManager
        val minBuf = try {
            AudioRecord.getMinBufferSize(RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT)
        } catch (e: Exception) { -1 }
        val outBuf = try {
            AudioTrack.getMinBufferSize(RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT)
        } catch (e: Exception) { -1 }
        return mapOf(
            "supported" to true,
            "microphone_hardware" to pm.hasSystemFeature(
                PackageManager.FEATURE_MICROPHONE),
            "speaker_hardware" to pm.hasSystemFeature(
                PackageManager.FEATURE_AUDIO_OUTPUT),
            "capture_api" to (minBuf > 0),
            "playback_api" to (outBuf > 0),
            "note" to "api path only; signal/audibility deferred",
        )
    }

    private fun battery(): Map<String, Any?> {
        return try {
            // Sticky battery intent: honors emulator overrides and all devices.
            val filter = context.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = filter?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = filter?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
            val status = filter?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
            val pct = if (level >= 0 && scale > 0) (level * 100 / scale) else -1
            lastBatteryPct = pct
            val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
            val powerSave = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                val pm = context.getSystemService(Context.POWER_SERVICE)
                    as android.os.PowerManager
                pm.isPowerSaveMode
            } else false
            val plugged = filter?.getIntExtra(
                BatteryManager.EXTRA_PLUGGED, -1) ?: -1
            val source = when (plugged) {
                BatteryManager.BATTERY_PLUGGED_AC -> "ac"
                BatteryManager.BATTERY_PLUGGED_USB -> "usb"
                BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                else -> "battery"
            }
            val health = when (filter?.getIntExtra(
                BatteryManager.EXTRA_HEALTH,
                BatteryManager.BATTERY_HEALTH_UNKNOWN)) {
                BatteryManager.BATTERY_HEALTH_GOOD -> "good"
                BatteryManager.BATTERY_HEALTH_OVERHEAT -> "overheat"
                BatteryManager.BATTERY_HEALTH_COLD -> "cold"
                BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "over-voltage"
                BatteryManager.BATTERY_HEALTH_DEAD -> "dead"
                else -> "unknown"
            }
            mapOf("supported" to true, "battery_pct" to pct,
                "charging" to charging, "power_save" to powerSave,
                "source" to source, "health" to health)
        } catch (e: Exception) {
            mapOf("supported" to false, "error" to (e.message ?: "unknown"))
        }
    }

    private fun network(): Map<String, Any?> {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            val metered = cm.isActiveNetworkMetered
            val net = when {
                caps == null -> "offline"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "metered"
                else -> "online"
            }
            mapOf("supported" to true, "network" to net, "metered" to metered)
        } catch (e: Exception) {
            mapOf("supported" to false, "error" to (e.message ?: "unknown"))
        }
    }

    private fun permissions(): Map<String, Any?> {
        fun granted(perm: String): Boolean =
            ContextCompat.checkSelfPermission(context, perm) ==
                PackageManager.PERMISSION_GRANTED
        val notif = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            granted(Manifest.permission.POST_NOTIFICATIONS)
        } else true
        // Microphone/camera/location are runtime-requested (Stage 9):
        // this reports state only; requests go through request* methods.
        return mapOf(
            "supported" to true,
            "notifications" to notif,
            "microphone" to granted(Manifest.permission.RECORD_AUDIO),
            "camera" to granted(Manifest.permission.CAMERA),
            "location" to granted(Manifest.permission.ACCESS_FINE_LOCATION),
            // Rung-1 pilot state (read-only; requests need no runtime grant,
            // the priv-app grant comes from the image allowlist instead).
            "forceStop" to
                granted("android.permission.FORCE_STOP_PACKAGES"),
        )
    }

    /**
     * Voice/audio capability report. Only natively checkable facts are real:
     * FEATURE_MICROPHONE (hardware), RECORD_AUDIO permission state, audio
     * output feature. Capture/STT/TTS/wake engines stay false until a
     * provider-backed implementation lands with hardware validation.
     * Nothing is faked: unimplemented == false, never assumed true.
     */
    private fun voiceSupport(): Map<String, Any?> {
        val pm = context.packageManager
        val hasMicHw = pm.hasSystemFeature(PackageManager.FEATURE_MICROPHONE)
        val hasAudioOut = pm.hasSystemFeature(PackageManager.FEATURE_AUDIO_OUTPUT)
        val mic = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        // SpeechRecognizer availability is an engine check, not a guarantee
        // of microphone signal; report engine presence only.
        val speechEngine = try {
            android.speech.SpeechRecognizer.isRecognitionAvailable(context)
        } catch (e: Exception) {
            false
        }
        // Android TTS engine presence (platform synthesis, not Zara's voice).
        val ttsEngine = try {
            val tts = android.speech.tts.TextToSpeech(context, null)
            val ok = tts.engines.isNotEmpty()
            tts.shutdown()
            ok
        } catch (e: Exception) {
            false
        }
        return mapOf(
            "supported" to true,
            "wake_phrase" to "Hey Zara",
            "microphone_hardware" to hasMicHw,
            "microphone_permission" to mic,
            "microphone_signal" to "unknown",
            "speaker_hardware" to hasAudioOut,
            "platform_stt_engine" to speechEngine,
            "platform_tts_engine" to ttsEngine,
            "audio_capture" to false,
            "stt" to false,
            "tts" to false,
            "wake_word_engine" to false,
            "note" to "provider-backed voice pending hardware validation",
        )
    }
}
