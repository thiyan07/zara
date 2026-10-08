package dev.zara.zara_android

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Rung-2 lab-pilot AccessibilityService (emulator lab ONLY, never Vivo).
 *
 * Enabled only when the operator turns it on in system Settings; the
 * bridge reports {supported:false} until then. Snapshot + tap helpers are
 * bounded, null-dropping, and never throw out to the channel handler.
 */
class ZaraA11yService : AccessibilityService() {

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    override fun onServiceConnected() {
        instance = this
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    companion object {
        @Volatile private var instance: ZaraA11yService? = null

        /** True while the service is bound (OS enabled it and connected). */
        fun isBound(): Boolean = instance != null

        /** True if the bound service currently sees an active window root.
         * Never throws; false covers unbound and rootless alike. */
        fun boundRootPresent(): Boolean {
            return try {
                instance?.rootInActiveWindow != null
            } catch (_: Exception) {
                false
            }
        }

        /** DFS node cap: snapshots stay small and bounded. */
        const val MAX_NODES = 50
        /** Max depth for bounded DFS. */
        const val MAX_DEPTH = 20
        /** Per-string caps for Rung-3. */
        const val MAX_CLASS = 40
        const val MAX_RES_ID = 128
        const val MAX_CONTENT_DESC = 120
        const val MAX_TEXT = 120

        private fun capClass(s: CharSequence?): String? {
            if (s == null) return null
            val t = s.toString()
            if (t.isEmpty()) return null
            return if (t.length > MAX_CLASS) t.substring(0, MAX_CLASS) else t
        }

        private fun capResId(s: CharSequence?): String? {
            if (s == null) return null
            val t = s.toString()
            if (t.isEmpty()) return null
            return if (t.length > MAX_RES_ID) t.substring(0, MAX_RES_ID) else t
        }

        private fun capContentDesc(s: CharSequence?): String? {
            if (s == null) return null
            val t = s.toString()
            if (t.isEmpty()) return null
            return if (t.length > MAX_CONTENT_DESC) t.substring(0, MAX_CONTENT_DESC) else t
        }

        private fun capText(s: CharSequence?): String? {
            if (s == null) return null
            val t = s.toString()
            if (t.isEmpty()) return null
            return if (t.length > MAX_TEXT) t.substring(0, MAX_TEXT) else t
        }

        /** Bounded DFS snapshot of the active window with Rung-3 fields. Never throws. */
        fun snapshot(params: Map<String, Any?>? = null): Map<String, Any?> {
            return try {
                val svc = instance
                    ?: return mapOf("supported" to false,
                        "reason" to "accessibility service unbound/disabled")
                val root = svc.rootInActiveWindow
                    ?: return mapOf("supported" to false,
                        "reason" to "no active window (service bound, no root)")

                // Expected package validation (Rung-3)
                val expectedPkg = params?.get("expected_package") as String?
                if (expectedPkg != null && expectedPkg.isNotEmpty()) {
                    val rootPkg = root.packageName?.toString() ?: ""
                    if (rootPkg != expectedPkg) {
                        return mapOf(
                            "supported" to false,
                            "reason" to "UNEXPECTED_PACKAGE",
                            "expected_package" to expectedPkg,
                            "actual_package" to rootPkg
                        )
                    }
                }

                // Configurable limits from params
                val maxElements = (params?.get("max_elements") as Number?)?.toInt() ?: MAX_NODES
                val includeText = (params?.get("include_text") as Boolean?) ?: true
                val includeContentDesc = (params?.get("include_content_description") as Boolean?) ?: true

                val nodes = mutableListOf<Map<String, Any?>>()
                val visited = mutableSetOf<Int>()
                var count = 0
                var truncated = false

                val rootBounds = Rect()
                try {
                    root.getBoundsInScreen(rootBounds)
                } catch (_: Exception) {}

                // Scrollable regions collection
                val scrollableRegions = mutableListOf<Map<String, Int>>()

                fun dfs(n: AccessibilityNodeInfo?, depth: Int) {
                    if (n == null || count >= maxElements || depth > MAX_DEPTH) {
                        if (count >= maxElements) truncated = true
                        return
                    }
                    // Cycle protection via identity hashCode
                    val idHash = System.identityHashCode(n)
                    if (idHash in visited) return
                    visited.add(idHash)

                    try {
                        val nodeData = nodeMap(n, count, includeText, includeContentDesc)
                        nodes.add(nodeData)
                        count++

                        // Collect scrollable regions
                        try {
                            if (n.isScrollable) {
                                val b = Rect()
                                n.getBoundsInScreen(b)
                                scrollableRegions.add(mapOf(
                                    "left" to b.left,
                                    "top" to b.top,
                                    "right" to b.right,
                                    "bottom" to b.bottom
                                ))
                            }
                        } catch (_: Exception) {}

                        if (count >= maxElements) {
                            truncated = true
                            return
                        }
                        val kids = try {
                            n.childCount
                        } catch (_: Exception) {
                            0
                        }
                        for (i in 0 until kids) {
                            if (count >= maxElements) break
                            var c: AccessibilityNodeInfo? = null
                            try {
                                c = n.getChild(i)
                            } catch (_: Exception) {
                                c = null
                            }
                            if (c != null) dfs(c, depth + 1)
                        }
                    } catch (_: Exception) {
                        // Skip malformed node
                    } finally {
                        // Release node reference
                        try {
                            n.recycle()
                        } catch (_: Exception) {}
                    }
                }

                try {
                    dfs(root, 0)
                } catch (_: Exception) {
                    return mapOf("supported" to false,
                        "reason" to "snapshot walk failed")
                }

                // Focused node ID (first focusable/selected node in DFS order)
                var focusedId = -1
                for (i in nodes.indices) {
                    val n = nodes[i]
                    if (n["focused"] == true || n["selected"] == true) {
                        focusedId = i
                        break
                    }
                }

                // Keyboard visibility heuristic
                var keyboardVisible = false
                try {
                    val imm = svc.getSystemService(android.content.Context.INPUT_METHOD_SERVICE) as android.view.inputmethod.InputMethodManager?
                    keyboardVisible = imm?.isAcceptingText ?: false
                } catch (_: Exception) {}

                val out = mutableMapOf<String, Any?>(
                    "supported" to true,
                    "node_count" to nodes.size,
                    "truncated" to truncated,
                    "nodes" to nodes,
                    "activity" to capClass(root.className),
                    "screen_w" to rootBounds.width(),
                    "screen_h" to rootBounds.height(),
                    "focused_id" to focusedId,
                    "keyboard_visible" to keyboardVisible,
                    "screenshot_available" to false,
                    "scrollable_regions" to scrollableRegions,
                )
                capResId(root.packageName)?.let { out["package"] = it }
                out
            } catch (e: Exception) {
                mapOf("supported" to false,
                    "reason" to ((e.message ?: "snapshot failed")
                        .take(MAX_TEXT)))
            }
        }

        private fun nodeMap(
            n: AccessibilityNodeInfo,
            id: Int,
            includeText: Boolean,
            includeContentDesc: Boolean
        ): Map<String, Any?> {
            val m = mutableMapOf<String, Any?>("node_id" to id)
            try {
                val b = Rect()
                try {
                    n.getBoundsInScreen(b)
                } catch (_: Exception) {
                }
                m["bounds"] = listOf(b.left, b.top, b.right, b.bottom)
                capClass(n.className)?.let { m["class"] = it }
                capResId(n.viewIdResourceName)?.let { m["resource_id"] = it }
                if (includeText) capText(n.text)?.let { m["text"] = it }
                if (includeContentDesc) capContentDesc(n.contentDescription)?.let { m["content_desc"] = it }
                capResId(n.packageName)?.let { m["package"] = it }
                try { m["clickable"] = n.isClickable } catch (_: Exception) {}
                try { m["long_clickable"] = n.isLongClickable } catch (_: Exception) {}
                try { m["scrollable"] = n.isScrollable } catch (_: Exception) {}
                try { m["editable"] = n.isEditable } catch (_: Exception) {}
                try { m["enabled"] = n.isEnabled } catch (_: Exception) {}
                try { m["selected"] = n.isSelected } catch (_: Exception) {}
                try { m["checked"] = n.isChecked } catch (_: Exception) {}
                try { m["focused"] = n.isFocused } catch (_: Exception) {}
                try { m["visible"] = n.isVisibleToUser } catch (_: Exception) {}
                // Sensitive: password field detection
                try {
                    val inputType = n.inputType
                    val isPassword = (inputType and android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD) != 0 ||
                                     (inputType and android.text.InputType.TYPE_NUMBER_VARIATION_PASSWORD) != 0 ||
                                     (inputType and android.text.InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD) != 0
                    m["sensitive"] = isPassword
                } catch (_: Exception) {
                    m["sensitive"] = false
                }
            } catch (_: Exception) {
            }
            return m
        }

        /**
         * Fresh-root lookup of [nodeId] in DFS order, then ACTION_CLICK.
         * Returns true if the click was performed, false if the node was
         * missing/not clickable, null when the service has no window
         * (unbound or no root — caller reports unavailable).
         */
        fun tapNode(nodeId: Int): Boolean? {
            return try {
                val svc = instance ?: return null
                val root = svc.rootInActiveWindow ?: return null
                var count = 0
                var target: AccessibilityNodeInfo? = null
                fun dfs(n: AccessibilityNodeInfo?): Boolean {
                    if (n == null || count >= MAX_NODES) return false
                    if (count == nodeId) {
                        target = n
                        return true
                    }
                    count++
                    val kids = try {
                        n.childCount
                    } catch (_: Exception) {
                        0
                    }
                    for (i in 0 until kids) {
                        if (count >= MAX_NODES) break
                        var c: AccessibilityNodeInfo? = null
                        try {
                            c = n.getChild(i)
                        } catch (_: Exception) {
                            c = null
                        }
                        if (c != null && dfs(c)) return true
                    }
                    return false
                }
                try {
                    dfs(root)
                } catch (_: Exception) {
                    return false
                }
                val t = target ?: return false
                try {
                    t.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                } catch (_: Exception) {
                    false
                }
            } catch (_: Exception) {
                false
            }
        }
    }
}
