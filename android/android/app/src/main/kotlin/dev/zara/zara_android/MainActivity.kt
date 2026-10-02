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
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var pendingPermResult: MethodChannel.Result? = null

    companion object {
        private const val REQ_MIC = 9001
        private const val REQ_NOTIF = 9002
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        DeviceBridge(applicationContext, ::requestZaraPermission)
            .attach(flutterEngine)
    }

    private fun requestZaraPermission(
        permission: String, result: MethodChannel.Result
    ) {
        if (ContextCompat.checkSelfPermission(this, permission) ==
            PackageManager.PERMISSION_GRANTED) {
            result.success(mapOf("granted" to true, "already" to true))
            return
        }
        pendingPermResult = result
        val code = if (permission == Manifest.permission.POST_NOTIFICATIONS)
            REQ_NOTIF else REQ_MIC
        ActivityCompat.requestPermissions(this, arrayOf(permission), code)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        // Denial is a normal outcome: report state, never throw.
        pendingPermResult?.success(
            mapOf("granted" to granted, "already" to false))
        pendingPermResult = null
    }
}
