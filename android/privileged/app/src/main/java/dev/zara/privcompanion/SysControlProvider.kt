package dev.zara.privcompanion

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Bundle

/**
 * Lab-only privileged companion: ONE narrow interface for operations an
 * ordinary app can never perform (TETHER_PRIVILEGED). No network, no UI,
 * no other code. Every method verifies before reporting applied:true.
 *
 * Access control (defense in depth):
 *  1. Manifest provider android:permission (signature SYSCONTROL).
 *  2. checkCallingPermission() below (same verdict, explicit).
 * Anything else returns applied:false with a reason — never an exception
 * shaped like success, never a silent toggle.
 */
class SysControlProvider : ContentProvider() {
    companion object {
        const val PERMISSION = "dev.zara.permission.SYSCONTROL"
        private const val POLL_MS = 500L
        private const val POLL_ROUNDS = 12 // ~6 s bounded verification
    }

    override fun onCreate(): Boolean = true
    override fun query(uri: Uri, projection: Array<String>?,
                       selection: String?, selectionArgs: Array<String>?,
                       sortOrder: String?): Cursor? = null
    override fun getType(uri: Uri): String? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?,
                        selectionArgs: Array<String>?): Int = 0
    override fun update(uri: Uri, values: ContentValues?, selection: String?,
                        selectionArgs: Array<String>?): Int = 0

    private fun allowed(): Boolean {
        val ctx = context ?: return false
        return ctx.checkCallingPermission(PERMISSION) ==
            PackageManager.PERMISSION_GRANTED
    }

    override fun call(method: String, arg: String?, extras: Bundle?): Bundle? {
        if (!allowed()) {
            return Bundle().apply {
                putBoolean("applied", false)
                putString("state", "unknown")
                putString("error",
                    "calling package lacks $PERMISSION (refused)")
            }
        }
        return when (method) {
            "probe" -> Bundle().apply {
                putBoolean("applied", false)
                putString("state", "ready")
                putString("error", "")
                putString("version", "1")
                putStringArrayList("operations",
                    arrayListOf("probe", "setTether"))
            }
            "setTether" -> setTether(extras?.getBoolean("enabled",
                false) == true)
            else -> Bundle().apply {
                putBoolean("applied", false)
                putString("state", "unknown")
                putString("error", "unknown method (refused)")
            }
        }
    }

    private fun tetherUp(): Boolean {
        val cm = context?.getSystemService(
            Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            ?: return false
        // Authoritative where available: the hidden tethered-iface list
        // (callable here because this APK holds TETHER_PRIVILEGED).
        try {
            val m = cm.javaClass.getMethod("getTetheredIfaces")
            @Suppress("UNCHECKED_CAST")
            val ifaces = m.invoke(cm) as? Array<String>
            if (ifaces != null) return ifaces.isNotEmpty()
        } catch (e: Exception) {
            // fall through to the transport heuristic below
        }
        // Heuristic fallback (documented, weak on client-mode wifi):
        // active wifi transport present. Poll agreement, not proof.
        return try {
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        } catch (e: Exception) {
            false
        }
    }

    private fun setTether(enabled: Boolean): Bundle {
        // API truth (enumerated live on API 36): ConnectivityManager has
        // NO start/stopTether methods anymore — only observers. The real
        // switch is TetheringManager (public since API 30, needs
        // TETHER_PRIVILEGED, which this priv-app holds).
        if (android.os.Build.VERSION.SDK_INT <
            android.os.Build.VERSION_CODES.R) {
            return Bundle().apply {
                putBoolean("applied", false)
                putString("state", "unknown")
                putString("error", "TetheringManager needs API 30+")
            }
        }
        val tm = try {
            context?.getSystemService(
                android.net.TetheringManager::class.java)
        } catch (e: Exception) {
            null
        }
        if (tm == null) {
            return Bundle().apply {
                putBoolean("applied", false)
                putString("state", "unknown")
                putString("error", "no tethering service")
            }
        }
        try {
            val req = android.net.TetheringManager.TetheringRequest.Builder(
                android.net.TetheringManager.TETHERING_WIFI).build()
            val exec = java.util.concurrent.Executors
                .newSingleThreadExecutor()
            try {
                if (enabled) {
                    tm.startTethering(req, exec,
                        object : android.net.TetheringManager
                            .StartTetheringCallback {})
                } else {
                    tm.stopTethering(req, exec,
                        object : android.net.TetheringManager
                            .StopTetheringCallback {})
                }
            } finally {
                exec.shutdown()
            }
        } catch (e: SecurityException) {
            return Bundle().apply {
                putBoolean("applied", false)
                putString("state", "unknown")
                putString("error",
                    "TETHER_PRIVILEGED not effective (not a priv-app install)")
            }
        } catch (e: Exception) {
            return Bundle().apply {
                putBoolean("applied", false)
                putString("state", "unknown")
                putString("error", (e.message ?: "tether call failed"))
            }
        }
        // Bounded post-action verification: applied:true only on observed
        // state, never on API-call return.
        var observed = tetherUp()
        var left = POLL_ROUNDS
        while (observed != enabled && left > 0) {
            try {
                Thread.sleep(POLL_MS)
            } catch (e: InterruptedException) {
                break
            }
            observed = tetherUp()
            left--
        }
        val applied = observed == enabled
        return Bundle().apply {
            putBoolean("applied", applied)
            putString("state", if (observed) "enabled" else "disabled")
            putString("error", if (applied) "" else "state unverified")
        }
    }
}
