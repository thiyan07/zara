package dev.zara.zara_android

import java.text.Normalizer

/**
 * Contract-v3 semantic element targeting — PURE logic, no Android imports.
 *
 * A `target` selects an element among the FRESH accessibility-tree facts at
 * action time. Rationale (docs/UI_SNAPSHOT_CONTRACT.md §8): positional
 * node_ids are fragile across relayouts; resource ids / content descs /
 * text survive relaunches. Matching MIRRORS core/uicontrol.py's ElementTarget
 * discipline so core and native agree:
 * - a node matches when EVERY provided selector matches (resource_id,
 *   content_desc, text, role = class name); index_hint (0-based among
 *   matches) disambiguates; without it more than one match is AMBIGUOUS;
 * - text/content_desc compare in the matcher canonical form (NFC, bidi
 *   control chars stripped, whitespace collapsed, lowercased) mirroring
 *   core's normalize_text;
 * - selector values (or the selector KEYS) shaped like secrets are refused
 *   fail-closed ("INSECURE_TARGET"), mirroring core's _SECRET_RE;
 * - unknown keys are refused (no silent dimensions).
 * Deterministic: same facts + target → same verdict, always.
 */
object SemTarget {

    const val MAX_RESOURCE_ID_LEN = 128
    const val MAX_CONTENT_DESC_LEN = 120
    const val MAX_TEXT_LEN = 120
    const val MAX_ROLE_LEN = 40

    private val SECRET_RE = Regex(
        "password|passwd|token|api[_-]?key|apikey|secret|credential|private[_-]?key",
        RegexOption.IGNORE_CASE)

    private val SELECTOR_KEYS = setOf(
        "resource_id", "content_desc", "text", "role", "index_hint")

    data class Selectors(
        val resourceId: String? = null,
        val contentDesc: String? = null,
        val text: String? = null,
        val role: String? = null,
        val indexHint: Int? = null,
    )

    /** Per-node selector facts, as captured during one fresh tree walk. */
    data class NodeFacts(
        val className: String? = null,
        val resourceId: String? = null,
        val contentDesc: String? = null,
        val text: String? = null,
    )

    fun normalize(value: String?): String {
        if (value == null) return ""
        var out = Normalizer.normalize(value, Normalizer.Form.NFC)
        out = out.replace("\u0000", " ").replace("\uFEFF", " ")
        // NBSP family counts as whitespace, mirroring Python \s on str.
        out = out.replace("\u00A0", " ").replace("\u2007", " ")
            .replace("\u202F", " ")
        val bidi = StringBuilder()
        // LRM, RLM
        for (c in 0x200E..0x200F) bidi.append(c.toChar())
        // LRE, RLE, PDF, LRO, RLO
        for (c in 0x202A..0x202E) bidi.append(c.toChar())
        // LRI, RLI, FSI, PDI
        for (c in 0x2066..0x2069) bidi.append(c.toChar())
        for (ch in bidi) out = out.replace(ch.toString(), " ")
        return out.lowercase().trim().split(Regex("\\s+")).joinToString(" ")
    }

    sealed class Parse {
        data class Ok(val sel: Selectors) : Parse()
        data class Refuse(val code: String, val reason: String) : Parse()
    }

    /**
     * Validate a caller-provided `target` object: shape, selector lengths,
     * secret-shape, unknown keys, index_hint plausibility. Any refusal is
     * fail-closed: the caller must not fall back to guessing an element.
     */
    fun parse(target: Any?): Parse {
        if (target == null) return Parse.Refuse("TARGET_INVALID",
            "missing target (selectors required)")
        if (target !is Map<*, *>) return Parse.Refuse("TARGET_INVALID",
            "target must be an object of selector fields")
        val unknown = target.keys.filterIsInstance<String>()
            .filter { it !in SELECTOR_KEYS }
        if (unknown.isNotEmpty()) return Parse.Refuse("TARGET_INVALID",
            "unknown selector field(s): ${unknown.take(4)}")

        var selCount = 0
        // Length checks performed explicitly so the refusal is precise.
        fun lengthOk(key: String, limit: Int): Boolean {
            val raw = target[key] as? String ?: return true
            return raw.length in 1..limit
        }
        fun secretOk(key: String): Boolean {
            val raw = (target[key] as? String) ?: return true
            return !SECRET_RE.containsMatchIn(raw)
        }
        for (k in listOf("resource_id", "content_desc", "text", "role")) {
            if (target[k] is String) {
                if (!secretOk(k)) return Parse.Refuse("INSECURE_TARGET",
                    "selector $k looks secret-shaped (refused, never matched)")
                if (!lengthOk(k, if (k == "resource_id") MAX_RESOURCE_ID_LEN
                    else if (k == "role") MAX_ROLE_LEN
                    else MAX_CONTENT_DESC_LEN.coerceAtLeast(MAX_TEXT_LEN))) {
                    return Parse.Refuse("TARGET_INVALID",
                        "selector $k too long (limit ${if (k == "resource_id") MAX_RESOURCE_ID_LEN else 120})")
                }
                selCount++
            }
        }
        if (selCount == 0) return Parse.Refuse("TARGET_INVALID",
            "target needs at least one selector (resource_id, " +
                "content_desc, text, or role)")
        var hint: Int? = null
        val rawHint = target["index_hint"]
        if (rawHint != null) {
            val h = (rawHint as? Int)
            if (h == null || h < 0) {
                return Parse.Refuse("TARGET_INVALID",
                    "index_hint must be a non-negative integer")
            }
            hint = h
        }
        return Parse.Ok(Selectors(
            resourceId = target["resource_id"] as? String,
            contentDesc = target["content_desc"] as? String,
            text = target["text"] as? String,
            role = target["role"] as? String,
            indexHint = hint,
        ))
    }

    /** A node matches when EVERY provided selector matches. */
    fun matches(sel: Selectors, f: NodeFacts): Boolean {
        sel.resourceId?.let { if ((f.resourceId ?: "") != it) return false }
        val wantCd = sel.contentDesc
        if (wantCd != null &&
            normalize(f.contentDesc) != normalize(wantCd)) return false
        val wantText = sel.text
        if (wantText != null &&
            normalize(f.text) != normalize(wantText)) return false
        sel.role?.let { if (normalize(f.className) != normalize(it)) return false }
        return true
    }

    /** Ordered indices (fresh-tree DFS order) of every matching node. */
    fun resolve(facts: List<NodeFacts>, sel: Selectors): List<Int> {
        val out = mutableListOf<Int>()
        for (i in facts.indices) {
            if (matches(sel, facts[i])) out.add(i)
        }
        return out
    }

    /** Verdict after resolution: NOT_FOUND / AMBIGUOUS / picked index. */
    sealed class Pick {
        data class Found(val index: Int) : Pick()
        object NotFound : Pick()
        data class Ambiguous(val matches: Int) : Pick()
        data class BadHint(val available: Int) : Pick()
    }

    fun pick(indices: List<Int>, hint: Int?): Pick = when {
        indices.isEmpty() -> Pick.NotFound
        indices.size == 1 -> Pick.Found(indices[0])
        hint != null ->
            if (hint < indices.size) Pick.Found(indices[hint])
            else Pick.BadHint(indices.size)
        else -> Pick.Ambiguous(indices.size)
    }
}
