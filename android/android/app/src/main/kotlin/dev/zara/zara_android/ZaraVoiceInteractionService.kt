package dev.zara.zara_android

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.service.voice.VoiceInteractionService
import android.util.Log

/**
 * Zara VoiceInteractionService (Stage 10 compatibility spike).
 *
 * Answers ONE question: can the OS route the native assistant gesture to
 * Zara? This service creates diagnostic sessions only. It never executes
 * tools, never touches policy, never reads secrets. Authority stays in
 * Zara Core; the session UI is a stateless diagnostic surface.
 */
class ZaraVoiceInteractionService : VoiceInteractionService() {

    companion object {
        const val TAG = "zara-assist"
        @Volatile var ready = false

        fun component(ctx: Context): ComponentName =
            ComponentName(ctx, ZaraVoiceInteractionService::class.java)
    }

    override fun onReady() {
        super.onReady()
        ready = true
        val active = VoiceInteractionService.isActiveService(
            this, component(this))
        Log.i(TAG, "service ready; isActiveService=$active")
    }

    override fun onShutdown() {
        ready = false
        Log.i(TAG, "service shutdown")
        super.onShutdown()
    }

    override fun onUnbind(intent: Intent?): Boolean {
        Log.i(TAG, "service unbind")
        return super.onUnbind(intent)
    }
}
