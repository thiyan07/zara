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

        /** Foreground package right now, or null when unbound/rootless.
         * Never throws. Read-only. */
        fun focusedPackageNow(): String? {
            return try {
                instance?.rootInActiveWindow?.packageName?.toString()
            } catch (_: Exception) {
                null
            }
        }

        /**
         * Global navigation (back/home) with an explicit foreground-package
         * guard, mirroring the tap/set-text guard discipline:
         * - Refuses with NO_WINDOW when the service is unbound or the
         *   snapshot root is gone, and with UNEXPECTED_PACKAGE when
         *   `expectedPackage` (from a fresh inspect) does not match the
         *   current foreground — so a navigation is never fired into an
         *   unexpected app.
         * - Accepts the framework's verdict via performGlobalAction; a
         *   framework refusal is NAV_FAILED, not silent success.
         * - After acceptance, re-reads the foreground package and takes a
         *   FRESH bounded snapshot so callers verify the new screen with
         *   the same primitives as everywhere else (never a stale id).
         * Never throws; refusals are structured error codes like the
         * other contract actions.
         */
        fun navigateResult(
            direction: String,
            expectedPackage: String?,
        ): Map<String, Any?> {
            val base = mapOf("contract_version" to CONTRACT_VERSION,
                "supported" to true, "direction" to direction)
            return try {
                val svc = instance
                    ?: return base + mapOf("supported" to false,
                        "applied" to false, "error" to "NO_WINDOW",
                        "reason" to "accessibility service unbound/disabled")
                val root = svc.rootInActiveWindow
                    ?: return base + mapOf("supported" to false,
                        "applied" to false, "error" to "NO_WINDOW",
                        "reason" to "no active window (service bound, no root)")
                val actualPkg = root.packageName?.toString() ?: ""
                if (!expectedPackage.isNullOrEmpty() &&
                    actualPkg != expectedPackage) {
                    return base + mapOf("supported" to false,
                        "applied" to false, "error" to "UNEXPECTED_PACKAGE",
                        "reason" to "foreground package does not match the "
                            + "approved context",
                        "actual_package" to actualPkg,
                        "expected_package" to expectedPackage)
                }
                val action = if (direction == "home")
                    android.accessibilityservice.AccessibilityService
                        .GLOBAL_ACTION_HOME
                else
                    android.accessibilityservice.AccessibilityService
                        .GLOBAL_ACTION_BACK
                val dispatched = svc.performGlobalAction(action)
                if (!dispatched) {
                    return base + mapOf("supported" to false,
                        "applied" to false, "error" to "NAV_FAILED",
                        "reason" to "framework refused the global action")
                }
                // Bounded settle, then the FRESH post-navigation state.
                var pkgAfter: String? = null
                var freshId: String? = null
                try {
                    Thread.sleep(400)
                    pkgAfter = focusedPackageNow()
                    val snap = snapshot()
                    freshId = snap["snapshot_id"] as? String
                } catch (_: Exception) {
                }
                base + mapOf("applied" to true, "package_before" to actualPkg,
                    "package_after" to pkgAfter,
                    "fresh_snapshot_id" to freshId,
                    "verify_state" to
                        "applied (framework accepted; confirm the new screen "
                            + "via the fresh snapshot)")
            } catch (e: Exception) {
                base + mapOf("supported" to false, "applied" to false,
                    "error" to "NAV_FAILED",
                    "reason" to ((e.message ?: "navigation failed")
                        .take(200)))
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
        /** UI snapshot/action contract version (docs/UI_SNAPSHOT_CONTRACT.md).
         * v2 adds snapshot-bound set-text and scroll; the v1 snapshot_id
         * algorithm is unchanged, so v1 ids remain valid. */
        const val CONTRACT_VERSION = 3

        /** One bounded grace walk after ACTION_SET_TEXT: the accessibility
         * tree may still serve the pre-edit text immediately after the
         * action completes (event propagation), so a short wait plus one
         * re-walk verifies what the field actually holds. Honest verify
         * states are unchanged — text_matched only when truly observed. */
        const val POST_EDIT_REREAD_GRACE_MS = 800L

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
                out["contract_version"] = CONTRACT_VERSION
                out["max_elements"] = maxElements
                val snapId = structureId(
                    capResId(root.packageName), capClass(root.className),
                    nodes)
                out["snapshot_id"] = snapId
                // Contract v1 staleness check: a caller holding an older
                // snapshot_id learns the screen changed instead of acting
                // on moved targets.
                val expectedSnap =
                    params?.get("expected_snapshot_id") as String?
                if (!expectedSnap.isNullOrEmpty() && expectedSnap != snapId) {
                    return mapOf("supported" to false,
                        "reason" to "STALE_SNAPSHOT",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_snapshot_id" to expectedSnap,
                        "snapshot_id" to snapId,
                        "package" to (capResId(root.packageName) ?: ""))
                }
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
         * Contract-v1 structural snapshot id: stable hash over the tree
         * STRUCTURE (node order, classes, resource ids, bounds, flags),
         * never over text/content-desc (chat lines and labels change
         * without moving tap targets). Format "<8-hex>:<node_count>".
         * Java/Kotlin String.hashCode is specified, so the id is stable
         * across processes for the same structure.
         */
        private fun structuralKey(
            id: Int, cls: String?, resId: String?, bounds: Any?,
            clickable: Boolean, editable: Boolean,
            enabled: Boolean, visible: Boolean,
        ): String = "$id|$cls|$resId|$bounds|$clickable|$editable" +
            "|$enabled|$visible"

        private fun structureId(
            pkg: String?, activity: String?,
            nodes: List<Map<String, Any?>>,
        ): String {
            val sb = StringBuilder("v1|${pkg ?: ""}|${activity ?: ""}|")
            for (n in nodes) {
                sb.append(structuralKey(
                    (n["node_id"] as? Number)?.toInt() ?: -1,
                    n["class"] as? String, n["resource_id"] as? String,
                    n["bounds"],
                    n["clickable"] as? Boolean ?: false,
                    n["editable"] as? Boolean ?: false,
                    n["enabled"] as? Boolean ?: false,
                    n["visible"] as? Boolean ?: false)).append(';')
            }
            return sb.toString().hashCode().toUInt().toString(16) +
                ":${nodes.size}"
        }

        /**
         * Contract-v1 tap: fresh-root lookup of [nodeId] in DFS order with
         * foreground-package and snapshot-staleness guards, then
         * ACTION_CLICK only on a clickable node. Never throws; the result
         * map carries explicit error codes (STALE_SNAPSHOT,
         * UNEXPECTED_PACKAGE, NODE_MISSING, NOT_CLICKABLE, NO_WINDOW)
         * instead of a bare boolean, so callers cannot mistake a refusal
         * for success.
         */
        fun tapResult(
            nodeId: Int,
            expectedPackage: String?,
            expectedSnapshotId: String?,
            maxElements: Int = MAX_NODES,
            target: Any? = null,
        ): Map<String, Any?> {
            return try {
                val svc = instance ?: return mapOf("supported" to false,
                    "applied" to false, "error" to "NO_WINDOW",
                    "reason" to "accessibility service unbound/disabled",
                    "contract_version" to CONTRACT_VERSION)
                val root = svc.rootInActiveWindow ?: return mapOf(
                    "supported" to false, "applied" to false,
                    "error" to "NO_WINDOW",
                    "reason" to "no active window (service bound, no root)",
                    "contract_version" to CONTRACT_VERSION)
                val actualPkg = root.packageName?.toString() ?: ""
                if (!expectedPackage.isNullOrEmpty() &&
                    actualPkg != expectedPackage
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "UNEXPECTED_PACKAGE",
                        "reason" to
                            "foreground package changed since snapshot",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_package" to expectedPackage,
                        "actual_package" to actualPkg)
                }
                // One fresh walk: structural digest + target lookup
                // together, so the staleness verdict describes the exact
                // tree we would act on.
                val digest = StringBuilder()
                digest.append("v1|").append(
                    capResId(root.packageName) ?: "").append('|')
                    .append(capClass(root.className) ?: "").append('|')
                var count = 0
                // Renamed local (was `target`): it must not shadow the
                // v3 `target: Any?` parameter — a shadowed param is how
                // the semantic selector silently degrades to positional.
                var posTarget: AccessibilityNodeInfo? = null
                var targetClickable = false
                // Contract-v3 semantic targeting: parallel collector for
                // selector-matched nodes (refs + DFS ids + selector facts),
                // empty and unused on the positional node_id path.
                val semSel = if (target != null) {
                    when (val parsed = SemTarget.parse(target)) {
                        is SemTarget.Parse.Refuse -> return mapOf(
                            "supported" to false, "applied" to false,
                            "error" to parsed.code,
                            "reason" to parsed.reason,
                            "contract_version" to CONTRACT_VERSION)
                        is SemTarget.Parse.Ok -> parsed.sel
                    }
                } else null
                val matchRefs = mutableListOf<AccessibilityNodeInfo>()
                val matchIds = mutableListOf<Int>()
                val matchFacts = mutableListOf<SemTarget.NodeFacts>()
                val seen = mutableSetOf<Int>()
                val cap = maxElements.coerceIn(1, MAX_NODES)
                fun keyOf(n: AccessibilityNodeInfo, id: Int): String {
                    val b = Rect()
                    try {
                        n.getBoundsInScreen(b)
                    } catch (_: Exception) {
                    }
                    fun flag(f: () -> Boolean): Boolean = try {
                        f()
                    } catch (_: Exception) {
                        false
                    }
                    return structuralKey(id, capClass(n.className),
                        capResId(n.viewIdResourceName),
                        listOf(b.left, b.top, b.right, b.bottom),
                        flag { n.isClickable }, flag { n.isEditable },
                        flag { n.isEnabled }, flag { n.isVisibleToUser })
                }
                fun dfs(n: AccessibilityNodeInfo?, depth: Int) {
                    if (n == null || count >= cap || depth > MAX_DEPTH) return
                    // Cycle protection mirrors snapshot(); digest covers
                    // the WHOLE tree (no early exit on a match): the
                    // tap-time id must equal a full inspect digest for the
                    // same screen.
                    val idHash = System.identityHashCode(n)
                    if (idHash in seen) return
                    seen.add(idHash)
                    digest.append(keyOf(n, count)).append(';')
                    if (count == nodeId) {
                        posTarget = n
                        targetClickable = try {
                            n.isClickable
                        } catch (_: Exception) {
                            false
                        }
                    }
                    if (semSel != null) {
                        val f = SemTarget.NodeFacts(
                            className = capClass(n.className),
                            resourceId = capResId(n.viewIdResourceName),
                            contentDesc = try { n.contentDescription
                                ?.toString() } catch (_: Exception) { null },
                            text = try { n.text?.toString()
                                ?.takeIf { it.isNotEmpty() }
                            } catch (_: Exception) { null },
                        )
                        if (SemTarget.matches(semSel, f)) {
                            matchRefs.add(n)
                            matchIds.add(count)
                            matchFacts.add(f)
                        }
                    }
                    count++
                    val kids = try {
                        n.childCount
                    } catch (_: Exception) {
                        0
                    }
                    for (i in 0 until kids) {
                        if (count >= cap) break
                        var c: AccessibilityNodeInfo? = null
                        try {
                            c = n.getChild(i)
                        } catch (_: Exception) {
                            c = null
                        }
                        if (c != null) dfs(c, depth + 1)
                    }
                }
                try {
                    dfs(root, 0)
                } catch (_: Exception) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "TAP_FAILED",
                        "reason" to "snapshot walk failed",
                        "contract_version" to CONTRACT_VERSION)
                }
                val snapId = digest.toString().hashCode().toUInt()
                    .toString(16) + ":$count"
                if (!expectedSnapshotId.isNullOrEmpty() &&
                    expectedSnapshotId != snapId
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "STALE_SNAPSHOT",
                        "reason" to "screen structure changed since " +
                            "snapshot (refused, never executed)",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_snapshot_id" to expectedSnapshotId,
                        "snapshot_id" to snapId,
                        "package" to actualPkg)
                }
                // Contract-v3: semantic target resolution happens against
                // the EXACT tree the digest describes — same walk, same
                // verdict basis as the staleness check.
                var t: AccessibilityNodeInfo? = null
                var pickedId = -1
                var pickedFacts: SemTarget.NodeFacts? = null
                var specName = "node"
                if (semSel != null) {
                    specName = "target"
                    when (val p = SemTarget.pick(matchIds, semSel.indexHint)) {
                        is SemTarget.Pick.NotFound -> return mapOf(
                            "supported" to false, "applied" to false,
                            "error" to "TARGET_NOT_FOUND",
                            "reason" to
                                "no element matches the target in the " +
                                "fresh tree (refused, never guessed)",
                            "contract_version" to CONTRACT_VERSION,
                            "snapshot_id" to snapId,
                            "package" to actualPkg)
                        is SemTarget.Pick.Ambiguous -> return mapOf(
                            "supported" to false, "applied" to false,
                            "error" to "TARGET_AMBIGUOUS",
                            "reason" to ("${p.matches} elements match the "
                                + "target; give index_hint or a more "
                                + "specific selector (refused)"),
                            "contract_version" to CONTRACT_VERSION,
                            "snapshot_id" to snapId,
                            "package" to actualPkg,
                            "candidates" to matchIds.take(8).mapIndexed {
                                i, id -> mapOf(
                                    "node_id" to id,
                                    "resource_id" to (matchFacts[i]
                                        .resourceId ?: ""),
                                    "role" to (matchFacts[i]
                                        .className ?: ""),
                                )
                            })
                        is SemTarget.Pick.BadHint -> return mapOf(
                            "supported" to false, "applied" to false,
                            "error" to "TARGET_INDEX_OUT_OF_RANGE",
                            "reason" to ("index_hint beyond the ${p.available} "
                                + "matches (refused, never guessed)"),
                            "contract_version" to CONTRACT_VERSION,
                            "snapshot_id" to snapId,
                            "package" to actualPkg)
                        is SemTarget.Pick.Found -> {
                            pickedId = p.index
                            val refIx = matchIds.indexOf(pickedId)
                            t = if (refIx >= 0) matchRefs[refIx] else null
                            pickedFacts = if (refIx >= 0) matchFacts[refIx]
                                else null
                        }
                    }
                } else {
                    if (nodeId >= 0) {
                        pickedId = nodeId
                        t = posTarget
                    }
                }
                if (t == null) return mapOf("supported" to false,
                    "applied" to false, "error" to "NODE_MISSING",
                    "reason" to
                        (if (semSel != null)
                            "matched element not present in fresh tree"
                        else "node $nodeId not present in fresh tree"),
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                // Positional path captured clickability during the walk; the
                // semantic path re-reads it from the picked live node.
                val clickableNow = if (semSel != null) {
                    try {
                        t.isClickable
                    } catch (_: Exception) {
                        false
                    }
                } else targetClickable
                if (!clickableNow) return mapOf("supported" to false,
                    "applied" to false, "error" to "NOT_CLICKABLE",
                    "reason" to "$specName is not clickable",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                val applied = try {
                    t.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                } catch (_: Exception) {
                    false
                }
                val baseId = mapOf("supported" to true, "applied" to applied,
                    "package" to actualPkg, "snapshot_id" to snapId,
                    "contract_version" to CONTRACT_VERSION,
                    "verify_state" to if (applied) "clicked"
                    else "not_applied")
                if (semSel != null) {
                    baseId + mapOf("target_matched" to mapOf(
                        "node_id" to pickedId,
                        "resource_id" to (pickedFacts?.resourceId ?: ""),
                        "content_desc" to (pickedFacts?.contentDesc ?: ""),
                        "text" to (pickedFacts?.text ?: ""),
                        "role" to (pickedFacts?.className ?: "")))
                } else baseId
            } catch (e: Exception) {
                mapOf("supported" to false, "applied" to false,
                    "error" to "TAP_FAILED",
                    "reason" to ((e.message ?: "tap failed").take(MAX_TEXT)),
                    "contract_version" to CONTRACT_VERSION)
            }
        }

        /** Facts about one walked node (structure + capability flags). */
        private data class WalkFacts(
            val key: String,
            val clickable: Boolean,
            val editable: Boolean,
            val scrollable: Boolean,
            val sensitive: Boolean,
            val text: String?,
            // Contract-v3 selector facts (digest unchanged — these are
            // additive and never feed structuralKey).
            val cls: String? = null,
            val resId: String? = null,
            val cd: String? = null,
            val bounds: List<Int> = emptyList(),
        )

        /**
         * Contract-v3 read-only semantic lookup: resolve a `target` selector
         * against the FRESH tree WITHOUT acting. Returns MATCH with the
         * element's bounded facts, or TARGET_NOT_FOUND / TARGET_AMBIGUOUS
         * (with bounded candidates). Never taps, types, or focuses; the
         * acting capabilities (gui.tap / gui.set_text) keep their own
         * policy path. Facts are validated/scrubbed like inspect facts.
         */
        fun findResult(
            expectedPackage: String?,
            maxElements: Int = MAX_NODES,
            target: Any?,
        ): Map<String, Any?> {
            return try {
                val parsed = SemTarget.parse(target)
                if (parsed is SemTarget.Parse.Refuse) {
                    return mapOf("supported" to false, "applied" to false,
                        "status" to parsed.code,
                        "error" to parsed.code,
                        "reason" to parsed.reason,
                        "contract_version" to CONTRACT_VERSION)
                }
                val sel = (parsed as SemTarget.Parse.Ok).sel
                val (snapId, facts, root) = freshWalk(maxElements)
                if (root == null || snapId.isEmpty()) {
                    return mapOf("supported" to false, "applied" to false,
                        "status" to "NO_WINDOW", "error" to "NO_WINDOW",
                        "reason" to "no fresh tree (service unbound or no " +
                            "active window)",
                        "contract_version" to CONTRACT_VERSION)
                }
                val actualPkg = root.packageName?.toString() ?: ""
                if (!expectedPackage.isNullOrEmpty() &&
                    actualPkg != expectedPackage
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "status" to "UNEXPECTED_PACKAGE",
                        "error" to "UNEXPECTED_PACKAGE",
                        "reason" to
                            "foreground package is not the requested one",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_package" to expectedPackage,
                        "actual_package" to actualPkg)
                }
                val nodeFacts = facts.map {
                    SemTarget.NodeFacts(
                        className = it.cls, resourceId = it.resId,
                        contentDesc = it.cd, text = it.text)
                }
                val ids = SemTarget.resolve(nodeFacts, sel)
                when (val p = SemTarget.pick(ids, sel.indexHint)) {
                    is SemTarget.Pick.NotFound -> mapOf(
                        "supported" to true, "applied" to false,
                        "status" to "TARGET_NOT_FOUND",
                        "error" to "TARGET_NOT_FOUND",
                        "reason" to "no element matches the target in the " +
                            "fresh tree (refused, never guessed)",
                        "package" to actualPkg,
                        "snapshot_id" to snapId,
                        "contract_version" to CONTRACT_VERSION)
                    is SemTarget.Pick.Ambiguous -> mapOf(
                        "supported" to true, "applied" to false,
                        "status" to "TARGET_AMBIGUOUS",
                        "error" to "TARGET_AMBIGUOUS",
                        "reason" to ("${p.matches} elements match the " +
                            "target; give index_hint or a more specific " +
                            "selector"),
                        "package" to actualPkg,
                        "snapshot_id" to snapId,
                        "contract_version" to CONTRACT_VERSION,
                        "candidates" to ids.take(8).mapIndexed { i, id ->
                            mapOf(
                                "node_id" to id,
                                "resource_id" to (nodeFacts[id].resourceId
                                    ?: ""),
                                "role" to (nodeFacts[id].className ?: ""),
                                "bounds" to facts[id].bounds)
                        })
                    is SemTarget.Pick.BadHint -> mapOf(
                        "supported" to true, "applied" to false,
                        "status" to "TARGET_INDEX_OUT_OF_RANGE",
                        "error" to "TARGET_INDEX_OUT_OF_RANGE",
                        "reason" to ("index_hint beyond the ${p.available} " +
                            "matches"),
                        "package" to actualPkg,
                        "snapshot_id" to snapId,
                        "contract_version" to CONTRACT_VERSION)
                    is SemTarget.Pick.Found -> mapOf(
                        "supported" to true, "applied" to false,
                        "status" to "MATCH",
                        "package" to actualPkg,
                        "snapshot_id" to snapId,
                        "contract_version" to CONTRACT_VERSION,
                        "element" to mapOf(
                            "node_id" to p.index,
                            "resource_id" to (nodeFacts[p.index].resourceId
                                ?: ""),
                            "content_desc" to (nodeFacts[p.index].contentDesc
                                ?: ""),
                            "text" to (facts[p.index].text ?: ""),
                            "role" to (nodeFacts[p.index].className ?: ""),
                            "bounds" to facts[p.index].bounds,
                            "clickable" to facts[p.index].clickable,
                            "editable" to facts[p.index].editable),
                        "note" to "read-only lookup; no element was acted on")
                }
            } catch (e: Exception) {
                mapOf("supported" to false, "applied" to false,
                    "status" to "FIND_FAILED", "error" to "FIND_FAILED",
                    "reason" to ((e.message ?: "find failed").take(MAX_TEXT)),
                    "contract_version" to CONTRACT_VERSION)
            }
        }

        /**
         * One bounded fresh walk: structural digest + per-node facts.
         * Shared by the v2 edit/scroll actions (tap keeps its proven
         * inline walk). Never throws.
         */
        private fun freshWalk(
            maxElements: Int,
        ): Triple<String, List<WalkFacts>, AccessibilityNodeInfo?> {
            val svc = instance
            val root = try {
                svc?.rootInActiveWindow
            } catch (_: Exception) {
                null
            } ?: return Triple("", emptyList(), null)
            val digest = StringBuilder()
            digest.append("v1|").append(
                capResId(root.packageName) ?: "").append('|')
                .append(capClass(root.className) ?: "").append('|')
            val facts = mutableListOf<WalkFacts>()
            val seen = mutableSetOf<Int>()
            val cap = maxElements.coerceIn(1, MAX_NODES)
            var count = 0
            fun dfs(n: AccessibilityNodeInfo?, depth: Int) {
                if (n == null || count >= cap || depth > MAX_DEPTH) return
                val idHash = System.identityHashCode(n)
                if (idHash in seen) return
                seen.add(idHash)
                val b = Rect()
                try {
                    n.getBoundsInScreen(b)
                } catch (_: Exception) {
                }
                fun flag(f: () -> Boolean): Boolean = try {
                    f()
                } catch (_: Exception) {
                    false
                }
                val cls = capClass(n.className)
                val resId = capResId(n.viewIdResourceName)
                val bounds = listOf(b.left, b.top, b.right, b.bottom)
                val clickable = flag { n.isClickable }
                val editable = flag { n.isEditable }
                val scrollable = flag { n.isScrollable }
                val inputType = try {
                    n.inputType
                } catch (_: Exception) {
                    0
                }
                val sensitive =
                    (inputType and android.text.InputType
                        .TYPE_TEXT_VARIATION_PASSWORD) != 0 ||
                        (inputType and android.text.InputType
                            .TYPE_NUMBER_VARIATION_PASSWORD) != 0 ||
                        (inputType and android.text.InputType
                            .TYPE_TEXT_VARIATION_VISIBLE_PASSWORD) != 0
                val text = if (sensitive) null else capText(n.text)
                facts.add(WalkFacts(
                    structuralKey(count, cls, resId, bounds, clickable,
                        editable, flag { n.isEnabled },
                        flag { n.isVisibleToUser }),
                    clickable, editable, scrollable, sensitive, text,
                    cls, resId,
                    try {
                        n.contentDescription?.toString()
                    } catch (_: Exception) {
                        null
                    },
                    bounds))
                digest.append(facts.last().key).append(';')
                count++
                val kids = try {
                    n.childCount
                } catch (_: Exception) {
                    0
                }
                for (i in 0 until kids) {
                    if (count >= cap) break
                    var c: AccessibilityNodeInfo? = null
                    try {
                        c = n.getChild(i)
                    } catch (_: Exception) {
                        c = null
                    }
                    if (c != null) dfs(c, depth + 1)
                }
            }
            try {
                dfs(root, 0)
            } catch (_: Exception) {
                return Triple("", emptyList(), null)
            }
            val snapId = digest.toString().hashCode().toUInt()
                .toString(16) + ":$count"
            return Triple(snapId, facts, root)
        }

        /**
         * Contract-v2 text entry: snapshot-bound ACTION_SET_TEXT on an
         * editable node, content-verified by re-read when readable. The
         * typed text is NEVER echoed in the result (password fields are
         * never re-read). Same guards as tap plus NOT_EDITABLE.
         */
        fun editResult(
            nodeId: Int,
            expectedPackage: String?,
            expectedSnapshotId: String?,
            maxElements: Int = MAX_NODES,
            text: String?,
            target: Any? = null,
        ): Map<String, Any?> {
            return try {
                val svc = instance ?: return mapOf("supported" to false,
                    "applied" to false, "error" to "NO_WINDOW",
                    "reason" to "accessibility service unbound/disabled",
                    "contract_version" to CONTRACT_VERSION)
                val root = svc.rootInActiveWindow ?: return mapOf(
                    "supported" to false, "applied" to false,
                    "error" to "NO_WINDOW",
                    "reason" to "no active window (service bound, no root)",
                    "contract_version" to CONTRACT_VERSION)
                val actualPkg = root.packageName?.toString() ?: ""
                if (!expectedPackage.isNullOrEmpty() &&
                    actualPkg != expectedPackage
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "UNEXPECTED_PACKAGE",
                        "reason" to
                            "foreground package changed since snapshot",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_package" to expectedPackage,
                        "actual_package" to actualPkg)
                }
                val want = (text ?: "").take(500)
                if (want.isEmpty()) return mapOf("supported" to false,
                    "applied" to false, "error" to "EMPTY_TEXT",
                    "reason" to "nothing to type (empty text)",
                    "contract_version" to CONTRACT_VERSION)
                val (snapId, facts, _) = freshWalk(maxElements)
                if (facts.isEmpty() && snapId.isEmpty()) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "TAP_FAILED",
                        "reason" to "snapshot walk failed",
                        "contract_version" to CONTRACT_VERSION)
                }
                if (!expectedSnapshotId.isNullOrEmpty() &&
                    expectedSnapshotId != snapId
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "STALE_SNAPSHOT",
                        "reason" to "screen structure changed since " +
                            "snapshot (refused, never executed)",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_snapshot_id" to expectedSnapshotId,
                        "snapshot_id" to snapId,
                        "package" to actualPkg)
                }
                // Contract-v3: semantic targets resolve against the EXACT
                // fact list the digest describes; positional node_id keeps
                // its proven behavior. Never both.
                if (target != null && nodeId >= 0) return mapOf(
                    "supported" to false, "applied" to false,
                    "error" to "TARGET_CONFLICT",
                    "reason" to "give target or node_id, not both",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                var editIdx = nodeId
                var specName = "node $nodeId"
                var targetMatched: SemTarget.NodeFacts? = null
                if (target != null) {
                    specName = "target"
                    when (val parsed = SemTarget.parse(target)) {
                        is SemTarget.Parse.Refuse -> return mapOf(
                            "supported" to false, "applied" to false,
                            "error" to parsed.code,
                            "reason" to parsed.reason,
                            "contract_version" to CONTRACT_VERSION,
                            "snapshot_id" to snapId,
                            "package" to actualPkg)
                        is SemTarget.Parse.Ok -> {
                            val nodeFacts = facts.map {
                                SemTarget.NodeFacts(
                                    className = it.cls, resourceId = it.resId,
                                    contentDesc = it.cd, text = it.text)
                            }
                            val ids = SemTarget.resolve(nodeFacts, parsed.sel)
                            when (val p = SemTarget.pick(ids, parsed.sel.indexHint)) {
                                is SemTarget.Pick.NotFound -> return mapOf(
                                    "supported" to false, "applied" to false,
                                    "error" to "TARGET_NOT_FOUND",
                                    "reason" to ("no element matches the " + "target in the fresh tree (refused, never guessed)"),
                                    "contract_version" to CONTRACT_VERSION,
                                    "snapshot_id" to snapId,
                                    "package" to actualPkg)
                                is SemTarget.Pick.Ambiguous -> return mapOf(
                                    "supported" to false, "applied" to false,
                                    "error" to "TARGET_AMBIGUOUS",
                                    "reason" to ("${p.matches} elements match the target; give index_hint or a more specific selector (refused)"),
                                    "contract_version" to CONTRACT_VERSION,
                                    "snapshot_id" to snapId,
                                    "package" to actualPkg,
                                    "candidates" to ids.take(8).mapIndexed {
                                        i, id -> mapOf(
                                            "node_id" to id,
                                            "resource_id" to (nodeFacts[id]
                                                .resourceId ?: ""),
                                            "role" to (nodeFacts[id]
                                                .className ?: ""))
                                    })
                                is SemTarget.Pick.BadHint -> return mapOf(
                                    "supported" to false, "applied" to false,
                                    "error" to "TARGET_INDEX_OUT_OF_RANGE",
                                    "reason" to ("index_hint beyond the ${p.available} matches (refused, never guessed)"),
                                    "contract_version" to CONTRACT_VERSION,
                                    "snapshot_id" to snapId,
                                    "package" to actualPkg)
                                is SemTarget.Pick.Found -> {
                                    editIdx = p.index
                                    targetMatched = nodeFacts[p.index]
                                }
                            }
                        }
                    }
                }
                val factsNow = facts.getOrNull(editIdx) ?: return mapOf(
                    "supported" to false, "applied" to false,
                    "error" to "NODE_MISSING",
                    "reason" to "$specName not present in fresh tree",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                if (!factsNow.editable) return mapOf("supported" to false,
                    "applied" to false, "error" to "NOT_EDITABLE",
                    "reason" to "$specName is not editable",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                // Re-locate the live node by the resolved DFS index.
                var live: AccessibilityNodeInfo? = null
                var count = 0
                fun find(n: AccessibilityNodeInfo?): Boolean {
                    if (n == null || count > editIdx) return false
                    if (count == editIdx) {
                        live = n
                        return true
                    }
                    count++
                    val kids = try {
                        n.childCount
                    } catch (_: Exception) {
                        0
                    }
                    for (i in 0 until kids) {
                        var c: AccessibilityNodeInfo? = null
                        try {
                            c = n.getChild(i)
                        } catch (_: Exception) {
                            c = null
                        }
                        if (c != null && find(c)) return true
                    }
                    return false
                }
                try {
                    find(root)
                } catch (_: Exception) {
                    live = null
                }
                val t = live ?: return mapOf("supported" to false,
                    "applied" to false, "error" to "NODE_MISSING",
                    "reason" to "node $nodeId vanished before edit",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                val args = android.os.Bundle()
                args.putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                    want)
                val performed = try {
                    t.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,
                        args)
                } catch (_: Exception) {
                    false
                }
                if (!performed) return mapOf("supported" to false,
                    "applied" to false, "error" to "EDIT_FAILED",
                    "reason" to "framework refused set-text",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                if (factsNow.sensitive) return mapOf("supported" to true,
                    "applied" to true, "package" to actualPkg,
                    "snapshot_id" to snapId,
                    "contract_version" to CONTRACT_VERSION,
                    "verify_state" to
                        "applied (sensitive field, content not re-read)")
                // Content verification: one fresh walk, compare readable
                // text (structure is expected to be stable across typing).
                val (_, factsAfter, _) = freshWalk(maxElements)
                val after = factsAfter.getOrNull(editIdx)?.text
                val okEdit = mapOf("supported" to true,
                    "applied" to true, "package" to actualPkg,
                    "snapshot_id" to snapId,
                    "contract_version" to CONTRACT_VERSION)
                val matchedEvidence = if (targetMatched != null) mapOf(
                    "target_matched" to mapOf(
                        "node_id" to editIdx,
                        "resource_id" to (targetMatched.resourceId ?: ""),
                        "content_desc" to (targetMatched.contentDesc ?: ""),
                        "text" to (targetMatched.text ?: ""),
                        "role" to (targetMatched.className ?: "")))
                else emptyMap()
                if (after == want) {
                    return okEdit + mapOf(
                        "verify_state" to "text_matched") + matchedEvidence
                }
                return okEdit + mapOf(
                    "verify_state" to "applied (content changed, re-read " +
                        "differs — field may transform input)") + matchedEvidence
            } catch (e: Exception) {
                mapOf("supported" to false, "applied" to false,
                    "error" to "EDIT_FAILED",
                    "reason" to ((e.message ?: "edit failed").take(MAX_TEXT)),
                    "contract_version" to CONTRACT_VERSION)
            }
        }

        /**
         * One bounded post-edit re-read (accessibility-event grace).
         * ACTION_SET_TEXT can complete while the a11y tree still serves
         * the pre-edit text, so the synchronous walk in [editResult]
         * reports the honest "content changed" state even for a write
         * that landed. The bridge schedules THIS walk once, a short
         * grace later, off the immediate compare; it either confirms
         * text_matched or returns the SAME honest base map unchanged.
         * Same-process windows cache node state, so the target node is
         * refreshed before the text read — the same data the independent
         * uiautomator view of the field reports. Never fabricates
         * success: text_matched only when the refreshed node truly shows
         * the requested text; structural drift keeps the honest
         * uncertain state — no positional re-guessing.
         */
        fun revalidateEditResult(
            editIdx: Int,
            want: String,
            maxElements: Int = MAX_NODES,
            base: Map<String, Any?>,
        ): Map<String, Any?> {
            return try {
                val svc = instance ?: return base
                val root = try {
                    svc.rootInActiveWindow
                } catch (_: Exception) {
                    null
                } ?: return base
                var live: AccessibilityNodeInfo? = null
                var count = 0
                fun dfs(n: AccessibilityNodeInfo?, depth: Int) {
                    // Same pre-order discipline as freshWalk so the index
                    // space is identical.
                    if (n == null || count >= maxElements.coerceIn(
                            1, MAX_NODES) || depth > MAX_DEPTH
                    ) return
                    if (count == editIdx) live = n
                    count++
                    val kids = try {
                        n.childCount
                    } catch (_: Exception) {
                        0
                    }
                    for (i in 0 until kids) {
                        if (count >= maxElements.coerceIn(1, MAX_NODES)) break
                        var c: AccessibilityNodeInfo? = null
                        try {
                            c = n.getChild(i)
                        } catch (_: Exception) {
                            c = null
                        }
                        if (c != null) dfs(c, depth + 1)
                    }
                }
                dfs(root, 0)
                val node = live ?: return base
                val ok = try {
                    node.refresh()
                } catch (_: Exception) {
                    false
                }
                if (ok && node.text?.toString() == want) {
                    base + mapOf("verify_state" to "text_matched")
                } else {
                    base
                }
            } catch (_: Exception) {
                base
            }
        }

        /**
         * Contract-v2 scroll: snapshot-bound ACTION_SCROLL on a scrollable
         * node. Scrolling changes content, not structure, so verification
         * is the fresh post-scroll snapshot_id (callers diff content
         * themselves). Same guards as tap plus NOT_SCROLLABLE.
         */
        fun scrollResult(
            nodeId: Int,
            expectedPackage: String?,
            expectedSnapshotId: String?,
            maxElements: Int = MAX_NODES,
            forward: Boolean = true,
        ): Map<String, Any?> {
            return try {
                val svc = instance ?: return mapOf("supported" to false,
                    "applied" to false, "error" to "NO_WINDOW",
                    "reason" to "accessibility service unbound/disabled",
                    "contract_version" to CONTRACT_VERSION)
                val root = svc.rootInActiveWindow ?: return mapOf(
                    "supported" to false, "applied" to false,
                    "error" to "NO_WINDOW",
                    "reason" to "no active window (service bound, no root)",
                    "contract_version" to CONTRACT_VERSION)
                val actualPkg = root.packageName?.toString() ?: ""
                if (!expectedPackage.isNullOrEmpty() &&
                    actualPkg != expectedPackage
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "UNEXPECTED_PACKAGE",
                        "reason" to
                            "foreground package changed since snapshot",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_package" to expectedPackage,
                        "actual_package" to actualPkg)
                }
                val (snapId, facts, _) = freshWalk(maxElements)
                if (facts.isEmpty() && snapId.isEmpty()) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "TAP_FAILED",
                        "reason" to "snapshot walk failed",
                        "contract_version" to CONTRACT_VERSION)
                }
                if (!expectedSnapshotId.isNullOrEmpty() &&
                    expectedSnapshotId != snapId
                ) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "STALE_SNAPSHOT",
                        "reason" to "screen structure changed since " +
                            "snapshot (refused, never executed)",
                        "contract_version" to CONTRACT_VERSION,
                        "expected_snapshot_id" to expectedSnapshotId,
                        "snapshot_id" to snapId,
                        "package" to actualPkg)
                }
                if (nodeId !in facts.indices) return mapOf(
                    "supported" to false, "applied" to false,
                    "error" to "NODE_MISSING",
                    "reason" to "node $nodeId not present in fresh tree",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                if (!facts[nodeId].scrollable) {
                    return mapOf("supported" to false, "applied" to false,
                        "error" to "NOT_SCROLLABLE",
                        "reason" to "node $nodeId is not scrollable",
                        "contract_version" to CONTRACT_VERSION,
                        "snapshot_id" to snapId, "package" to actualPkg)
                }
                var live: AccessibilityNodeInfo? = null
                var count = 0
                fun find(n: AccessibilityNodeInfo?): Boolean {
                    if (n == null || count > nodeId) return false
                    if (count == nodeId) {
                        live = n
                        return true
                    }
                    count++
                    val kids = try {
                        n.childCount
                    } catch (_: Exception) {
                        0
                    }
                    for (i in 0 until kids) {
                        var c: AccessibilityNodeInfo? = null
                        try {
                            c = n.getChild(i)
                        } catch (_: Exception) {
                            c = null
                        }
                        if (c != null && find(c)) return true
                    }
                    return false
                }
                try {
                    find(root)
                } catch (_: Exception) {
                    live = null
                }
                val t = live ?: return mapOf("supported" to false,
                    "applied" to false, "error" to "NODE_MISSING",
                    "reason" to "node $nodeId vanished before scroll",
                    "contract_version" to CONTRACT_VERSION,
                    "snapshot_id" to snapId, "package" to actualPkg)
                val action = if (forward) {
                    AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                } else {
                    AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                }
                val performed = try {
                    t.performAction(action)
                } catch (_: Exception) {
                    false
                }
                val (afterId, _, _) = freshWalk(maxElements)
                mapOf("supported" to performed, "applied" to performed,
                    "package" to actualPkg,
                    "snapshot_id" to if (afterId.isNotEmpty()) afterId
                    else snapId,
                    "contract_version" to CONTRACT_VERSION,
                    "verify_state" to if (performed) {
                        "requested (compare snapshots for movement)"
                    } else {
                        "not_applied"
                    },
                    "error" to if (performed) "" else "SCROLL_FAILED")
            } catch (e: Exception) {
                mapOf("supported" to false, "applied" to false,
                    "error" to "SCROLL_FAILED",
                    "reason" to ((e.message ?: "scroll failed")
                        .take(MAX_TEXT)),
                    "contract_version" to CONTRACT_VERSION)
            }
        }

    }
}
