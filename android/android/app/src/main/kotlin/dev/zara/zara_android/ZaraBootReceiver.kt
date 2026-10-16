package dev.zara.zara_android

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

/**
 * Post-reboot resume prompt (opt-in only).
 *
 * Android 8+ background restrictions mean no receiver, service, or job
 * here can keep the agent polling loop alive: the OS will not run this
 * process until the user opens the app. So this receiver does exactly
 * one honest thing — when the user previously opted in (plain marker
 * file, no secrets), it posts a "tap to resume" notification that opens
 * the app, where the normal credential-restore + register + loop path
 * runs. No network calls, no microphone, no auto-execution, no key
 * handling. Without the opt-in marker (default) it does nothing.
 *
 * If POST_NOTIFICATIONS is denied (API 33+), the prompt is skipped
 * silently — the app still works when launched manually.
 */
class ZaraBootReceiver : BroadcastReceiver() {
    companion object {
        const val RESUME_NOTIFICATION_ID = 9101
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val optedIn = try {
            context.openFileInput(
                DeviceBridge.RESUME_OPT_IN_FILE).use {
                it.readBytes().toString(Charsets.UTF_8).trim() == "1"
            }
        } catch (e: Exception) {
            false
        }
        if (!optedIn) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context,
                android.Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED) {
            return
        }
        try {
            val nm = context.getSystemService(
                Context.NOTIFICATION_SERVICE) as NotificationManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                nm.createNotificationChannel(
                    android.app.NotificationChannel(
                        "zara_status", "Zara status",
                        NotificationManager.IMPORTANCE_LOW))
            }
            val open = PendingIntent.getActivity(
                context, 0,
                Intent(context, MainActivity::class.java).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP)
                },
                PendingIntent.FLAG_UPDATE_CURRENT or
                    PendingIntent.FLAG_IMMUTABLE)
            val note = NotificationCompat.Builder(context, "zara_status")
                .setContentTitle("Zara agent stopped after reboot")
                .setContentText("Tap to resume pairing, tasks and approvals.")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(open)
                .setAutoCancel(true)
                .build()
            nm.notify(RESUME_NOTIFICATION_ID, note)
        } catch (e: Exception) {
            // Best-effort prompt; manual launch always works.
        }
    }
}
