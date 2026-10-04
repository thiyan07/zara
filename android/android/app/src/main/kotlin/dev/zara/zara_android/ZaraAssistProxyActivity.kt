package dev.zara.zara_android

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * ASSIST intent entry point (Stage 10 compatibility spike).
 *
 * The OS assistant chooser enumerates packages through the
 * android.intent.action.ASSIST activity filter; without it Zara is
 * invisible no matter how correct the VoiceInteractionService is.
 * Plain Activity (NOT FlutterActivity): it must not own a FlutterEngine —
 * only MainActivity attaches the DeviceBridge channel. This proxy does
 * NOTHING but open the normal app surface (exactly what tapping the
 * launcher icon does) and finish. No tools, no execution, no authority.
 */
class ZaraAssistProxyActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Route through the real app entry point; no separate UI, no fork.
        val launch = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (intent?.extras != null) putExtras(intent.extras!!)
        }
        startActivity(launch)
        finish()
    }
}
