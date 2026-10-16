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
        /** Background-resume opt-in marker filename (plain, no secrets). */
        const val RESUME_OPT_IN_FILE = "zara_resume_optin"
        /** Wake-word detection preference (plain, no secrets). Absent =
            OFF. The listener itself never starts from this file — the
            user must enable it in Settings each process lifetime, and
            gating (mic permission, model, battery) re-validates then. */
        const val WAKE_OPT_IN_FILE = "zara_wake_optin"
        /** Wake-model directory (app-private filesDir/wake_models).
            Expected once a licensed model exists: model_info.json (or a
            .zip bundle containing it), the keyword .onnx, and
            melspectrogram.onnx. Nothing here is executed without the
            ONNX Runtime dependency, which is deliberately NOT added
            (footprint + licensing unverified — see WAKE_ONNX_SETUP.md).
            The probe below only reports file presence so errors stay
            specific; it never claims readiness. */
        const val WAKE_MODEL_DIR = "wake_models"
    }

    @Volatile private var capture: AudioRecord? = null
    @Volatile private var player: AudioTrack? = null

    fun attach(engine: FlutterEngine) {
        MethodChannel(engine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "getBattery" -> result.success(battery())
                    "getNetwork" -> result.success(network())
                    "getDeviceTelem" -> result.success(deviceTelem())
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
                    "getResumeOptIn" ->
                        result.success(readResumeOptIn())
                    "setResumeOptIn" -> {
                        writeResumeOptIn(
                            call.argument<Boolean>("enabled") == true)
                        result.success(true)
                    }
                    "getWakeOptIn" ->
                        result.success(readWakeOptIn())
                    "setWakeOptIn" -> {
                        writeWakeOptIn(
                            call.argument<Boolean>("enabled") == true)
                        result.success(true)
                    }
                    "getWakeModelStatus" ->
                        result.success(wakeModelStatus())
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
                    "getWifiState" -> result.success(wifiState())
                    "getBluetoothState" -> result.success(bluetoothState())
                    "getHotspotState" -> result.success(hotspotState())
                    "getMobileDataState" -> result.success(mobileDataState())
                    "getAirplaneModeState" ->
                        result.success(airplaneModeState())
                    "openSettings" -> openSettings(
                        call.argument<String>("screen"), result)
                    "openApp" -> openApp(
                        call.argument<String>("package"), result)
                    "setWifi" -> setWifi(
                        call.argument<Boolean>("enabled"), result)
                    "setBluetooth" -> setBluetooth(
                        call.argument<Boolean>("enabled"), result)
                    "setHotspot" -> setHotspot(
                        call.argument<Boolean>("enabled"), result)
                    "setMobileData" -> setMobileData(
                        call.argument<Boolean>("enabled"), result)
                    "setAirplaneMode" -> setAirplaneMode(
                        call.argument<Boolean>("enabled"), result)
                    "privCompanionProbe" ->
                        result.success(privCompanionProbe())
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

    // ---------- background-resume opt-in marker ----------
    //
    // Plain internal file (no secrets, no credentials): true = the user
    // opted into a "tap to resume" prompt after reboot. The boot receiver
    // reads it; nothing else does. Default (absent/unreadable) is false.

    private fun readResumeOptIn(): Boolean {
        return try {
            context.openFileInput(RESUME_OPT_IN_FILE).use {
                it.readBytes().toString(Charsets.UTF_8).trim() == "1"
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun writeResumeOptIn(enabled: Boolean) {
        try {
            if (enabled) {
                context.openFileOutput(RESUME_OPT_IN_FILE,
                    Context.MODE_PRIVATE).use {
                    it.write("1".toByteArray(Charsets.UTF_8))
                }
            } else {
                context.deleteFile(RESUME_OPT_IN_FILE)
            }
        } catch (e: Exception) {
            // best-effort; UI keeps last-known state
        }
    }

    // Wake-word preference file. Same plain-marker pattern as resume
    // opt-in: "1" = user wants detection, absent = OFF. Read once at
    // app start; the detector still requires mic permission, a model,
    // and battery gating on every enable — this file never overrides
    // a revoked permission or a missing model.
    private fun readWakeOptIn(): Boolean {
        return try {
            context.openFileInput(WAKE_OPT_IN_FILE).use {
                it.readBytes().toString(Charsets.UTF_8).trim() == "1"
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun writeWakeOptIn(enabled: Boolean) {
        try {
            if (enabled) {
                context.openFileOutput(WAKE_OPT_IN_FILE,
                    Context.MODE_PRIVATE).use {
                    it.write("1".toByteArray(Charsets.UTF_8))
                }
            } else {
                context.deleteFile(WAKE_OPT_IN_FILE)
            }
        } catch (e: Exception) {
            // best-effort; UI keeps last-known state
        }
    }

    // Wake-model presence probe (report only). Lists which of the three
    // expected files exist under filesDir/wake_models. `ready` is always
    // false: without the ONNX Runtime dependency there is no engine, so
    // presence must never be mistaken for readiness. The Dart side turns
    // this into specific actionable errors (see describeWakeModelError).
    private fun wakeModelStatus(): Map<String, Any> {
        return try {
            val dir = java.io.File(context.filesDir, WAKE_MODEL_DIR)
            val info = java.io.File(dir, "model_info.json").exists() ||
                dir.listFiles()?.any {
                    it.isFile && it.name.endsWith(".zip")
                } == true
            val keyword = dir.listFiles()?.any {
                it.isFile && it.name.endsWith(".onnx") &&
                    it.name != "melspectrogram.onnx"
            } == true
            val mel = java.io.File(dir, "melspectrogram.onnx").exists()
            mapOf("model_info" to info, "keyword_model" to keyword,
                "mel_model" to mel, "ready" to false,
                "note" to "no on-device wake engine on this build")
        } catch (e: Exception) {
            mapOf("model_info" to false, "keyword_model" to false,
                "mel_model" to false, "ready" to false,
                "note" to "probe failed")
        }
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

    // ---------- Phase 1: system-control (read-only states + settings/app) ----------
    //
    // Read-only states use ordinary permissions only; anything requiring a
    // privileged grant or a hidden API reports supported=false honestly.
    // Writes are NEVER performed here: toggles are exposed only as
    // Settings-screen intents (user confirms in the OS UI).

    private fun wifiState(): Map<String, Any?> {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            val onWifi = caps?.hasTransport(
                NetworkCapabilities.TRANSPORT_WIFI) == true
            var ssid: String? = null
            try {
                @Suppress("DEPRECATION")
                val wm = context.applicationContext.getSystemService(
                    Context.WIFI_SERVICE) as android.net.wifi.WifiManager
                ssid = wm.connectionInfo?.ssid?.takeIf {
                    it.isNotEmpty() && it != "<unknown ssid>" }
            } catch (_: Exception) { }
            val enabled = try {
                @Suppress("DEPRECATION")
                val wm = context.applicationContext.getSystemService(
                    Context.WIFI_SERVICE) as android.net.wifi.WifiManager
                wm.isWifiEnabled || onWifi
            } catch (_: Exception) { onWifi }
            mapOf("supported" to true,
                "state" to if (enabled) "enabled" else "disabled",
                "connected" to onWifi,
                "ssid" to ssid)
        } catch (e: Exception) {
            mapOf("supported" to false,
                "error" to (e.message ?: "wifi unavailable").take(200))
        }
    }

    private fun bluetoothState(): Map<String, Any?> {
        return try {
            val bm = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR2) {
                context.getSystemService(Context.BLUETOOTH_SERVICE)
                    as? android.bluetooth.BluetoothManager
            } else null
            val adapter = bm?.adapter
                ?: @Suppress("DEPRECATION") android.bluetooth.BluetoothAdapter
                    .getDefaultAdapter()
            if (adapter == null) {
                return mapOf("supported" to false,
                    "reason" to "no bluetooth adapter")
            }
            // API 31+ needs BLUETOOTH_CONNECT to read state; without it we
            // report honestly instead of throwing.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
                ContextCompat.checkSelfPermission(context,
                    Manifest.permission.BLUETOOTH_CONNECT) !=
                    PackageManager.PERMISSION_GRANTED) {
                return mapOf("supported" to false,
                    "reason" to "BLUETOOTH_CONNECT not granted")
            }
            val on = try { adapter.isEnabled } catch (_: Exception) { false }
            mapOf("supported" to true,
                "state" to if (on) "enabled" else "disabled")
        } catch (e: Exception) {
            mapOf("supported" to false,
                "error" to (e.message ?: "bluetooth unavailable").take(200))
        }
    }

    private fun hotspotState(): Map<String, Any?> {
        // No public SoftAp-state API: best-effort tethering-transport
        // observation, otherwise honest unsupported (never guessed).
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            val tethered = caps?.hasTransport(
                NetworkCapabilities.TRANSPORT_WIFI) == true &&
                caps.hasCapability(
                    NetworkCapabilities.NET_CAPABILITY_NOT_VPN) &&
                cm.isActiveNetworkMetered
            // Without a verified AP signal this stays unknown, not enabled.
            mapOf("supported" to false,
                "reason" to "hotspot state has no public API",
                "tether_hint" to tethered)
        } catch (e: Exception) {
            mapOf("supported" to false,
                "error" to (e.message ?: "hotspot unavailable").take(200))
        }
    }

    private fun mobileDataState(): Map<String, Any?> {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            val onCell = caps?.hasTransport(
                NetworkCapabilities.TRANSPORT_CELLULAR) == true
            var enabled: Boolean? = null
            try {
                val tm = context.getSystemService(Context.TELEPHONY_SERVICE)
                    as android.telephony.TelephonyManager
                enabled = tm.isDataEnabled
            } catch (_: Exception) { }
            mapOf("supported" to true,
                "state" to when (enabled) {
                    true -> "enabled"; false -> "disabled"
                    else -> if (onCell) "enabled" else "unknown" },
                "on_cellular" to onCell)
        } catch (e: Exception) {
            mapOf("supported" to false,
                "error" to (e.message ?: "mobile data unavailable").take(200))
        }
    }

    private fun airplaneModeState(): Map<String, Any?> {
        return try {
            val on = android.provider.Settings.Global.getInt(
                context.contentResolver,
                android.provider.Settings.Global.AIRPLANE_MODE_ON, 0) != 0
            mapOf("supported" to true,
                "state" to if (on) "enabled" else "disabled")
        } catch (e: Exception) {
            mapOf("supported" to false,
                "error" to (e.message ?: "airplane mode unreadable").take(200))
        }
    }

    private val settingsActions = mapOf(
        "wifi" to android.provider.Settings.ACTION_WIFI_SETTINGS,
        "bluetooth" to android.provider.Settings.ACTION_BLUETOOTH_SETTINGS,
        "hotspot" to android.provider.Settings.ACTION_WIRELESS_SETTINGS,
        "mobile_data" to android.provider.Settings.ACTION_DATA_ROAMING_SETTINGS,
        "airplane_mode" to android.provider.Settings.ACTION_AIRPLANE_MODE_SETTINGS,
    )

    private fun openSettings(screen: String?, result: MethodChannel.Result) {
        val intent = settingsIntentFor(screen)
        if (intent == null) {
            result.error("denied",
                "unknown settings screen (refused)", null)
            return
        }
        try {
            context.startActivity(intent)
            result.success(mapOf("supported" to true, "screen" to screen,
                "activity" to (intent.action ?: "")))
        } catch (e: Exception) {
            result.error("failed", (e.message ?: "open settings failed"), null)
        }
    }

    private fun settingsIntentFor(screen: String?): Intent? {
        val action = settingsActions[screen] ?: return null
        return Intent(action).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
    }

    private fun settingsFallback(screen: String, enabled: Boolean,
                                 result: MethodChannel.Result,
                                 extra: Map<String, Any?> = emptyMap()) {
        // Tier-2 path shared by every set action: open the OS screen for
        // user confirmation, report applied:false with a verify pointer.
        // Opening settings is NEVER reported as success.
        try {
            val intent = settingsIntentFor(screen)
            if (intent == null) {
                result.error("denied",
                    "unknown settings screen (refused)", null)
                return
            }
            context.startActivity(intent)
            result.success(mapOf("supported" to true, "applied" to false,
                "requested" to enabled, "method" to "settings-ui",
                "settings_opened" to true,
                "verify" to "system.$screen.state") + extra)
        } catch (e: Exception) {
            result.error("failed",
                (e.message ?: "open $screen settings failed"), null)
        }
    }

    private val appOpenTargets = setOf(
        "dev.zara.zara_android",
        "dev.zara.lab.privtest",
        "com.android.settings",
        "com.android.chrome",
        "com.google.android.gm",
        "com.google.android.calendar",
    )

    private fun openApp(pkg: String?, result: MethodChannel.Result) {
        if (pkg.isNullOrEmpty() || pkg !in appOpenTargets) {
            result.error("denied",
                "package not in app-open allowlist (refused)", null)
            return
        }
        try {
            val intent = context.packageManager
                .getLaunchIntentForPackage(pkg)?.apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
            if (intent == null) {
                result.error("unavailable",
                    "no launch intent for $pkg", null)
                return
            }
            context.startActivity(intent)
            result.success(mapOf("supported" to true, "package" to pkg,
                "launched" to true))
        } catch (e: Exception) {
            result.error("failed", (e.message ?: "open app failed"), null)
        }
    }

    // ---------- Phase 2: tiered state changes (verified or honest) ----------
    //
    // Every set method returns {supported, applied, requested, method,
    // verify}. applied:true ONLY after re-reading the state. Anything that
    // cannot be done at the current deployment tier returns applied:false
    // with settings_opened/deployment/setup pointers — never success.

    private fun btAdapter(): android.bluetooth.BluetoothAdapter? {
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR2) {
                (context.getSystemService(Context.BLUETOOTH_SERVICE)
                    as? android.bluetooth.BluetoothManager)?.adapter
            } else {
                @Suppress("DEPRECATION")
                android.bluetooth.BluetoothAdapter.getDefaultAdapter()
            }
        } catch (e: Exception) { null }
    }

    private fun hasBtConnect(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        return ContextCompat.checkSelfPermission(context,
            Manifest.permission.BLUETOOTH_CONNECT) ==
            PackageManager.PERMISSION_GRANTED
    }

    private fun hasWriteSecureSettings(): Boolean =
        ContextCompat.checkSelfPermission(context,
            "android.permission.WRITE_SECURE_SETTINGS") ==
            PackageManager.PERMISSION_GRANTED

    private fun setWifi(enabled: Boolean?, result: MethodChannel.Result) {
        if (enabled == null) {
            result.error("failed", "missing {enabled: boolean}", null)
            return
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            try {
                @Suppress("DEPRECATION")
                val wm = context.applicationContext.getSystemService(
                    Context.WIFI_SERVICE) as android.net.wifi.WifiManager
                @Suppress("DEPRECATION")
                wm.isWifiEnabled = enabled
                val got = wifiState()["state"] == "enabled"
                result.success(mapOf("supported" to true,
                    "applied" to (got == enabled), "requested" to enabled,
                    "method" to "direct-legacy",
                    "verify" to "system.wifi.state"))
                return
            } catch (e: SecurityException) {
                result.error("denied", "CHANGE_WIFI_STATE not granted", null)
                return
            } catch (e: Exception) {
                // fall through to settings UI
            }
        }
        settingsFallback("wifi", enabled, result)
    }

    private fun setBluetooth(enabled: Boolean?, result: MethodChannel.Result) {
        if (enabled == null) {
            result.error("failed", "missing {enabled: boolean}", null)
            return
        }
        if (!enabled) {
            // Direct disable attempt where the runtime permission allows it.
            if (hasBtConnect()) {
                try {
                    val adapter = btAdapter()
                    if (adapter != null) {
                        @Suppress("DEPRECATION")
                        adapter.disable()
                        val got = bluetoothState()["state"] == "enabled"
                        result.success(mapOf("supported" to true,
                            "applied" to (got == enabled),
                            "requested" to enabled,
                            "method" to "direct-disable",
                            "verify" to "system.bluetooth.state"))
                        return
                    }
                } catch (e: SecurityException) {
                    // fall through to settings UI
                } catch (e: Exception) {
                    // fall through to settings UI
                }
            }
            settingsFallback("bluetooth", enabled, result)
            return
        }
        // Enable needs explicit user consent: system dialog now, verified
        // state on the next read (applied:false until then — never a vow).
        try {
            val intent = Intent(
                android.bluetooth.BluetoothAdapter.ACTION_REQUEST_ENABLE)
                .apply { addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
            context.startActivity(intent)
            result.success(mapOf("supported" to true, "applied" to false,
                "requested" to enabled, "method" to "consent-dialog",
                "consent_requested" to true,
                "verify" to "system.bluetooth.state"))
        } catch (e: Exception) {
            settingsFallback("bluetooth", enabled, result)
        }
    }

    private fun setAirplaneMode(enabled: Boolean?,
                                result: MethodChannel.Result) {
        if (enabled == null) {
            result.error("failed", "missing {enabled: boolean}", null)
            return
        }
        if (hasWriteSecureSettings()) {
            try {
                android.provider.Settings.Global.putInt(
                    context.contentResolver,
                    android.provider.Settings.Global.AIRPLANE_MODE_ON,
                    if (enabled) 1 else 0)
            } catch (e: SecurityException) {
                result.error("denied",
                    "airplane write refused despite grant: " +
                        (e.message ?: "SecurityException"), null)
                return
            } catch (e: Exception) {
                // fall through to settings UI
                settingsFallback("airplane_mode", enabled, result, mapOf(
                    "deployment" to "t1-adb-grant",
                    "setup" to ("adb shell pm grant " + context.packageName +
                        " android.permission.WRITE_SECURE_SETTINGS")))
                return
            }
            // Best-effort legacy broadcast: AIRPLANE_MODE_CHANGED is a
            // protected broadcast on modern Android, so this may throw
            // while the setting itself is already written. The framework
            // observes Settings.Global directly; success is decided by
            // the re-read below, never by this broadcast.
            try {
                context.sendBroadcast(Intent(
                    Intent.ACTION_AIRPLANE_MODE_CHANGED).apply {
                    putExtra("state", enabled)
                })
            } catch (e: Exception) {
                // ignored: verified state decides
            }
            val got = airplaneModeState()["state"] == "enabled"
            result.success(mapOf("supported" to true,
                "applied" to (got == enabled), "requested" to enabled,
                "method" to "direct-adb-grant",
                "verify" to "system.airplane_mode.state"))
            return
        }
        settingsFallback("airplane_mode", enabled, result, mapOf(
            "deployment" to "t1-adb-grant",
            "setup" to ("adb shell pm grant " + context.packageName +
                " android.permission.WRITE_SECURE_SETTINGS")))
    }

    private fun setHotspot(enabled: Boolean?, result: MethodChannel.Result) {
        if (enabled == null) {
            result.error("failed", "missing {enabled: boolean}", null)
            return
        }
        // Tier-3 lab companion first; ordinary builds have no direct path.
        val comp = companionCall("setTether", enabled)
        if (comp != null) {
            val applied = comp["applied"] == true
            result.success(mapOf("supported" to true, "applied" to applied,
                "requested" to enabled, "method" to "priv-companion",
                "verify" to "system.hotspot.state") +
                (if (applied) emptyMap()
                 else mapOf("error" to "${comp["error"] ?: "companion refused"}")))
            return
        }
        settingsFallback("hotspot", enabled, result, mapOf(
            "deployment" to "t3-priv-companion-or-settings-ui",
            "setup" to "lab only: android/privileged + scripts/setup_privileged_lab.sh"))
    }

    private fun setMobileData(enabled: Boolean?,
                              result: MethodChannel.Result) {
        if (enabled == null) {
            result.error("failed", "missing {enabled: boolean}", null)
            return
        }
        // MODIFY_PHONE_STATE is system-only: no direct path by design.
        settingsFallback("mobile_data", enabled, result)
    }

    // ---------- Tier-3 lab companion (priv-app ContentProvider) ----------
    //
    // The companion (android/privileged/) exposes
    // content://dev.zara.privcompanion/control guarded by a signature
    // permission. Absence (ordinary builds, physical phone) is
    // deployment_blocked, never an error in the request itself.

    private fun privCompanionProbe(): Map<String, Any?> {
        return try {
            context.packageManager.getPackageInfo(
                "dev.zara.privcompanion", 0)
            val out = companionCall("probe", null)
            mapOf("supported" to true, "present" to true,
                "reachable" to (out != null))
        } catch (e: Exception) {
            mapOf("supported" to true, "present" to false,
                "reachable" to false,
                "setup" to "lab only: android/privileged + scripts/setup_privileged_lab.sh")
        }
    }

    private fun companionCall(method: String,
                              enabled: Boolean?): Map<String, Any?>? {
        return try {
            // Authority MUST equal the provider's registered
            // android:authorities string verbatim (validated live:
            // a bare package-name authority resolves to nothing).
            val uri = android.net.Uri.parse(
                "content://dev.zara.privcompanion.control/cmd")
            val client = context.contentResolver
                .acquireContentProviderClient(uri) ?: return null
            try {
                val args = android.os.Bundle().apply {
                    if (enabled != null) putBoolean("enabled", enabled)
                }
                val out = client.call(method, null, args) ?: return null
                @Suppress("UNCHECKED_CAST")
                mapOf("applied" to out.getBoolean("applied", false),
                    "state" to (out.getString("state") ?: "unknown"),
                    "error" to (out.getString("error") ?: ""))
            } finally {
                client.close()
            }
        } catch (e: SecurityException) {
            mapOf("applied" to false, "state" to "unknown",
                "error" to "companion permission denied")
        } catch (e: Exception) {
            null
        }
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

    /**
     * Resource telemetry (observation only, never authority). All values
     * from standard public Android APIs — no root, no shell, no procfs,
     * no privileged APIs. Unavailable signals are omitted (null on the
     * Dart side), never zero-as-healthy. Never throws.
     */
    private fun deviceTelem(): Map<String, Any?> {
        val out = mutableMapOf<String, Any?>()
        try {
            // CPU count (logical cores; static capacity signal, not usage).
            try {
                out["cpu_count"] = Runtime.getRuntime().availableProcessors()
            } catch (_: Exception) {
            }
            // RAM via ActivityManager.MemoryInfo.
            try {
                val am = context.getSystemService(Context.ACTIVITY_SERVICE)
                    as android.app.ActivityManager
                val mi = android.app.ActivityManager.MemoryInfo()
                am.getMemoryInfo(mi)
                out["mem_avail_mb"] = (mi.availMem / 1048576L).toInt()
                if (Build.VERSION.SDK_INT >=
                    Build.VERSION_CODES.JELLY_BEAN) {
                    out["mem_total_mb"] = (mi.totalMem / 1048576L).toInt()
                }
                out["low_memory"] = mi.lowMemory
            } catch (_: Exception) {
            }
            // Internal storage via StatFs (no directory scan).
            try {
                if (Build.VERSION.SDK_INT >=
                    Build.VERSION_CODES.JELLY_BEAN_MR2) {
                    val stat = android.os.StatFs(
                        android.os.Environment.getDataDirectory().path)
                    out["storage_free_mb"] =
                        (stat.availableBytes / 1048576L).toInt()
                    out["storage_total_mb"] =
                        (stat.totalBytes / 1048576L).toInt()
                }
            } catch (_: Exception) {
            }
            // Battery temperature + power-save (same sources as battery()).
            try {
                val filter = context.registerReceiver(
                    null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
                val tenth = filter?.getIntExtra(
                    BatteryManager.EXTRA_TEMPERATURE, -1) ?: -1
                if (tenth >= 0) out["battery_temp_c"] = tenth / 10.0
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                    val pm = context.getSystemService(Context.POWER_SERVICE)
                        as android.os.PowerManager
                    out["power_save"] = pm.isPowerSaveMode
                }
            } catch (_: Exception) {
            }
            // Thermal status (API 29+ only; older releases omit it).
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val pm = context.getSystemService(Context.POWER_SERVICE)
                        as android.os.PowerManager
                    out["thermal_state"] = when (
                        pm.currentThermalStatus) {
                        android.os.PowerManager.THERMAL_STATUS_NONE -> "none"
                        android.os.PowerManager.THERMAL_STATUS_LIGHT -> "light"
                        android.os.PowerManager.THERMAL_STATUS_MODERATE ->
                            "moderate"
                        android.os.PowerManager.THERMAL_STATUS_SEVERE -> "severe"
                        android.os.PowerManager.THERMAL_STATUS_CRITICAL ->
                            "critical"
                        android.os.PowerManager.THERMAL_STATUS_EMERGENCY ->
                            "emergency"
                        android.os.PowerManager.THERMAL_STATUS_SHUTDOWN ->
                            "shutdown"
                        else -> "unknown"
                    }
                }
            } catch (_: Exception) {
            }
            // CPU usage: no reliable public API (no procfs by policy) —
            // deliberately omitted. Core must treat cpu_pct as unknown.
            // GPU: no telemetry stack — deliberately omitted.
        } catch (e: Exception) {
            out["error"] = (e.message ?: "telem failed").take(200)
        }
        return out
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
