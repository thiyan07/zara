package dev.zara.zara_android

import android.app.assist.AssistContent
import android.app.assist.AssistStructure
import android.content.Context
import android.graphics.Color
import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.util.Log
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView

private const val TAG = "zara-assist"

/** Host: the OS binds here, then asks for a session per invocation. */
class ZaraVoiceInteractionSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession {
        Log.i(TAG, "new session; args=" +
            (args?.keySet()?.joinToString() ?: "none"))
        return ZaraVoiceInteractionSession(this)
    }
}

/**
 * Diagnostic assistant surface (NOT the final UI).
 *
 * Shows: Zara active, invocation source (decoded from showFlags where the
 * OS provides it), underlying activity where exposed, last-known battery.
 * Buttons: none that act. Dismissal is the system back gesture / hide.
 * No Core calls, no tools, no secrets, no audio capture in this spike.
 */
class ZaraVoiceInteractionSession(ctx: Context) : VoiceInteractionSession(ctx) {

    private lateinit var sourceView: TextView
    private lateinit var underView: TextView
    private lateinit var coreView: TextView
    private var shownAt = 0L
    private var underlying: String = "unknown"

    override fun onCreateContentView(): View {
        val root = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.argb(230, 24, 16, 40))
            setPadding(64, 64, 64, 64)
        }
        fun line(size: Float): TextView = TextView(context).apply {
            textSize = size
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            root.addView(this)
        }
        line(28f).text = "Zara"
        line(16f).text = "Assistant session active (diagnostic)"
        sourceView = line(14f)
        underView = line(14f)
        coreView = line(14f)
        refresh("created")
        return root
    }

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        shownAt = System.currentTimeMillis()
        val src = decodeSource(showFlags)
        Log.i(TAG, "session show flags=$showFlags source=$src " +
            "withAssist=${showFlags and SHOW_WITH_ASSIST != 0} " +
            "withShot=${showFlags and SHOW_WITH_SCREENSHOT != 0}")
        refresh(src)
    }

    override fun onHide() {
        Log.i(TAG, "session hide")
        super.onHide()
    }

    override fun onHandleAssist(
        data: Bundle?, structure: AssistStructure?, content: AssistContent?
    ) {
        // Underlying-app component where the OS exposes it (gesture
        // invocations often carry no structure — "unknown" is honest).
        underlying = structure?.activityComponent?.flattenToShortString()
            ?: "unknown"
        Log.i(TAG, "assist underlying=$underlying " +
            "dataKeys=" + (data?.keySet()?.joinToString() ?: "none"))
        super.onHandleAssist(data, structure, content)
        if (::underView.isInitialized) refresh("assist-data")
    }

    override fun onTaskStarted(ref: android.content.Intent?, taskId: Int) {
        Log.i(TAG, "task started id=$taskId")
        super.onTaskStarted(ref, taskId)
    }

    override fun onTaskFinished(ref: android.content.Intent?, taskId: Int) {
        Log.i(TAG, "task finished id=$taskId")
        super.onTaskFinished(ref, taskId)
    }

    override fun onDestroy() {
        Log.i(TAG, "session destroy")
        super.onDestroy()
    }

    private fun refresh(src: String) {
        sourceView.text = "Invocation source: $src"
        underView.text = "Underlying app: $underlying"
        coreView.text = "Device battery: ${DeviceBridge.lastBatteryPct}% " +
            "(open Zara app for full runtime)"
    }

    companion object {
        fun decodeSource(flags: Int): String = when {
            flags and SHOW_SOURCE_ASSIST_GESTURE != 0 -> "assist-gesture"
            flags and SHOW_SOURCE_PUSH_TO_TALK != 0 -> "push-to-talk"
            flags and SHOW_SOURCE_NOTIFICATION != 0 -> "notification"
            flags and SHOW_SOURCE_ACTIVITY != 0 -> "activity"
            flags and SHOW_SOURCE_APPLICATION != 0 -> "application"
            else -> "unspecified($flags)"
        }
    }
}
