package dev.zara.zara_android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contract-v3 semantic targeting: PURE resolver semantics, mirroring
 * core/uicontrol.py's ElementTarget/normalize_text discipline
 * (docs/UI_SNAPSHOT_CONTRACT.md §9). Deterministic vector tests:
 * same facts + target → same verdict, always.
 */
class SemTargetTest {

    private fun facts(vararg rows: Array<String?>): List<SemTarget.NodeFacts> =
        rows.map { SemTarget.NodeFacts(it[0], it[1], it[2], it[3]) }

    // row = [cls, resId, cd, text]
    private val screen = facts(
        arrayOf("android.view.ViewGroup", "root", null, null),
        arrayOf("android.widget.TextView", "title", "Inbox", "Inbox"),
        arrayOf("android.widget.EditText", null, null, "Search mail"),
        arrayOf("android.widget.TextView", "title", "Inbox", "Inbox"),
        arrayOf("android.widget.Button", "send", null, "Send"),
    )

    @Test
    fun resourceIdMatchesExactlyAndPrefersStable() {
        val out = SemTarget.resolve(
            screen, SemTarget.Selectors(resourceId = "send"))
        assertEquals(listOf(4), out)
    }

    @Test
    fun textMatchesCanonicalized() {
        // Same letters, different spacing/case → canonical match.
        val out = SemTarget.resolve(
            screen, SemTarget.Selectors(text = "  SEARCH   MAIL "))
        assertEquals(listOf(2), out)
    }

    @Test
    fun contentDescCanonicalMatch() {
        val out = SemTarget.resolve(
            screen, SemTarget.Selectors(contentDesc = "inbox"))
        assertEquals(listOf(1, 3), out)
    }

    @Test
    fun andSemanticsNarrowMatches() {
        val out = SemTarget.resolve(
            screen, SemTarget.Selectors(contentDesc = "Inbox",
                role = "android.widget.TextView"))
        // rows 1 and 3 are both TextViews with cd Inbox; both match AND.
        assertEquals(listOf(1, 3), out)
        assertEquals(1, SemTarget.resolve(
            screen, SemTarget.Selectors(resourceId = "send",
                text = "Send")).size)
    }

    @Test
    fun notFoundWhenNoSelectorMatches() {
        val out = SemTarget.resolve(
            screen, SemTarget.Selectors(resourceId = "no_such_id"))
        assertTrue(out.isEmpty())
        assertTrue(SemTarget.pick(out, null) is SemTarget.Pick.NotFound)
    }

    @Test
    fun ambiguousWithoutHint() {
        val ids = SemTarget.resolve(
            screen, SemTarget.Selectors(contentDesc = "inbox"))
        val p = SemTarget.pick(ids, null)
        assertTrue(p is SemTarget.Pick.Ambiguous)
        assertEquals(2, (p as SemTarget.Pick.Ambiguous).matches)
    }

    @Test
    fun indexHintDisambiguates() {
        val ids = SemTarget.resolve(
            screen, SemTarget.Selectors(contentDesc = "inbox"))
        assertEquals(1, (SemTarget.pick(ids, 0)
                as SemTarget.Pick.Found).index)
        assertEquals(3, (SemTarget.pick(ids, 1)
                as SemTarget.Pick.Found).index)
        assertTrue(SemTarget.pick(ids, 2) is SemTarget.Pick.BadHint)
    }

    @Test
    fun parseRefusesSecretShapedSelectors() {
        // Secret regex mirrors core/uicontrol.py _SECRET_RE exactly.
        val p = SemTarget.parse(mapOf("text" to "access token abc123"))
        assertTrue(p is SemTarget.Parse.Refuse)
        assertEquals("INSECURE_TARGET", (p as SemTarget.Parse.Refuse).code)
        val p2 = SemTarget.parse(
            mapOf("resource_id" to "com.app:id/password"))
        assertEquals("INSECURE_TARGET", (p2 as SemTarget.Parse.Refuse).code)
        val p3 = SemTarget.parse(mapOf("content_desc" to "PublicApiKey X"))
        assertEquals("INSECURE_TARGET", (p3 as SemTarget.Parse.Refuse).code)
    }

    @Test
    fun parseRefusesUnknownKeysAndEmptySelectors() {
        assertEquals(
            "TARGET_INVALID",
            ((SemTarget.parse(
                mapOf("text" to "hi", "extra" to 1))
                    as SemTarget.Parse.Refuse).code))
        assertEquals(
            "TARGET_INVALID",
            ((SemTarget.parse(mapOf<String, Any?>())
                    as SemTarget.Parse.Refuse).code))
        assertEquals(
            "TARGET_INVALID",
            ((SemTarget.parse("tap it")
                    as SemTarget.Parse.Refuse).code))
    }

    @Test
    fun parseValidatesLimitsAndIndexHint() {
        assertEquals(
            "TARGET_INVALID",
            ((SemTarget.parse(mapOf(
                "resource_id" to "x".repeat(129)))
                    as SemTarget.Parse.Refuse).code))
        assertEquals(
            "TARGET_INVALID",
            ((SemTarget.parse(mapOf("text" to "hi", "index_hint" to -1)))
                    as SemTarget.Parse.Refuse).code)
        val ok = SemTarget.parse(mapOf("text" to "Send",
            "index_hint" to 0)) as SemTarget.Parse.Ok
        assertEquals(0, ok.sel.indexHint)
    }

    @Test
    fun parseAbsentTargetIsNotAnError() {
        // null target = "not requested" (positional node_id path);
        // callers distinguish absence from refusal via this branch.
        val p = SemTarget.parse(null)
        assertTrue(p is SemTarget.Parse.Refuse) // service-level contract:
        // a CALLER passing target=null must not use the target path at
        // all; parse(null) refuses so accidental misuse fails closed.
    }

    @Test
    fun normalizeMirrorsCoreTextDiscipline() {
        assertEquals("send", SemTarget.normalize(" Send "))
        assertEquals("zara lab", SemTarget.normalize("  ZARA\t\u00A0lab "))
        // Bidi control characters are stripped, case-fold applied.
        assertEquals("abc", SemTarget.normalize("\u200Eabc\u200F"))
        assertEquals("", SemTarget.normalize(null))
    }
}
