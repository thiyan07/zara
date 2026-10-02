package dev.zara.zara_android

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
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
class DeviceBridge(private val context: Context) {
    companion object { const val CHANNEL = "zara/device" }

    fun attach(engine: FlutterEngine) {
        MethodChannel(engine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "getBattery" -> result.success(battery())
                    "getNetwork" -> result.success(network())
                    "getPermissions" -> result.success(permissions())
                    "getVoiceSupport" -> result.success(voiceSupport())
                    else -> result.notImplemented()
                }
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
            val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
                status == BatteryManager.BATTERY_STATUS_FULL
            val powerSave = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                val pm = context.getSystemService(Context.POWER_SERVICE)
                    as android.os.PowerManager
                pm.isPowerSaveMode
            } else false
            mapOf("supported" to true, "battery_pct" to pct,
                "charging" to charging, "power_save" to powerSave)
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
        // Location/mic/camera intentionally unrequested in Stage 2 — report state only.
        return mapOf(
            "supported" to true,
            "notifications" to notif,
            "microphone" to granted(Manifest.permission.RECORD_AUDIO),
            "camera" to granted(Manifest.permission.CAMERA),
            "location" to granted(Manifest.permission.ACCESS_FINE_LOCATION),
        )
    }

    /**
     * Voice support report (Stage 3 foundation). Only the microphone
     * *permission state* is real here; capture/STT/TTS/wake engines are
     * reported as unsupported until provider-backed implementations land
     * with hardware validation. Nothing is faked.
     */
    private fun voiceSupport(): Map<String, Any?> {
        val mic = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        return mapOf(
            "supported" to true,
            "wake_phrase" to "Hey Zara",
            "microphone_permission" to mic,
            "audio_capture" to false,
            "stt" to false,
            "tts" to false,
            "wake_word_engine" to false,
            "note" to "provider-backed voice pending hardware validation",
        )
    }
}
