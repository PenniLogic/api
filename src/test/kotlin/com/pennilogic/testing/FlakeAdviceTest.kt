package com.pennilogic.testing

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.time.Duration
import java.time.Instant

class FlakeAdviceTest {
    private val policy = FlakePolicy.accepted()
    private val identity = FlakeIdentity("synthetic-fixture", 7, TestCategory.UNIT, "test", "a".repeat(64), "b".repeat(64))
    private val now = Instant.parse("2026-10-08T00:00:00Z")

    @Test
    fun `the prepared accepted strategy supplies every flake threshold`() {
        assertEquals(1, policy.retryLimit)
        assertEquals(0, policy.restrictedRetryLimit)
        assertEquals(2, policy.quarantineAfterFlakes)
        assertEquals(14, policy.windowDays)
        assertEquals(2, policy.quarantineCeilingPercent)
        assertEquals(14, policy.quarantineMaxAgeDays)
        assertEquals(flakeDigest(policyBytes()), policy.digest)
    }

    @Test
    fun `policy decoding rejects missing malformed duplicate oversized and non UTF8 data safely`() {
        val bytes = policyBytes()
        val bad =
            listOf(
                byteArrayOf(),
                "{}".toByteArray(),
                """{"schema_version":1}""".toByteArray(),
                """{"schema_version":1,"flake_policy":"synthetic-private-marker"}""".toByteArray(),
                bytes.copyOf(bytes.size - 2),
                bytes.toString(Charsets.UTF_8).replaceFirst("{", "{\"flake_policy\":{},").toByteArray(),
                ByteArray(FlakePolicy.MAX_BYTES + 1),
                byteArrayOf(0xc3.toByte(), 0x28),
                byteArrayOf(0xef.toByte(), 0xbb.toByte(), 0xbf.toByte()) + bytes,
            )
        for (input in bad) {
            val error = assertThrows(IllegalArgumentException::class.java) { FlakePolicy.parse(input) }
            assertTrue(error.message.orEmpty().startsWith("flake-policy-"), "static-policy-refusal")
            assertFalse(error.stackTraceToString().contains("synthetic-private-marker"), "no-policy-payload")
        }
    }

    @Test
    fun `policy numbers and declared change classes have no coercions or invented defaults`() {
        val root = Json.parseToJsonElement(policyBytes().toString(Charsets.UTF_8)).jsonObject
        val original = root.getValue("flake_policy").jsonObject
        val invalid =
            listOf(
                "retry_limit" to "true",
                "retry_limit" to "\"1\"",
                "retry_limit" to "-1",
                "retry_limit" to "2",
                "money_path_retry_limit" to "1",
                "quarantine_after_flakes" to "0",
                "quarantine_after_flakes" to (FlakeHistory.MAX_OBSERVATIONS + 1).toString(),
                "flake_window_days" to "0",
                "quarantine_max_age_days" to "0",
                "quarantine_rate_ceiling_percent" to "101",
                "retry_semantics" to "null",
                "unrecognized" to "1",
            )
        val mutations = invalid.map { (key, value) -> original + (key to Json.parseToJsonElement(value)) } + (original - "retry_limit")
        for (fields in mutations) {
            assertThrows(IllegalArgumentException::class.java) {
                FlakePolicy.parse(JsonObject(root + ("flake_policy" to JsonObject(fields))).toString().toByteArray())
            }
        }
        val classes = root.getValue("change_classes").jsonArray
        for (value in listOf(JsonNull, JsonArray(emptyList()), JsonArray(classes + classes.first()))) {
            assertThrows(IllegalArgumentException::class.java) {
                FlakePolicy.parse(JsonObject(root + ("change_classes" to value)).toString().toByteArray())
            }
        }
    }

    @Test
    fun `requested retries exceeding either policy limit are configuration failures`() {
        assertEquals(1, history().retries)
        assertEquals(0, history(retries = 0).retries)
        assertEquals(0, history(classes = setOf("money_path")).retries)
        assertEquals(0, history(classes = setOf("api_service", "money_path")).retries)
        for (limit in listOf(-1, 2, Int.MAX_VALUE)) {
            assertThrows(IllegalArgumentException::class.java) { history(retries = limit) }
        }
        assertThrows(IllegalArgumentException::class.java) { history(classes = setOf("money_path"), retries = 1) }
        assertThrows(IllegalArgumentException::class.java) { history(classes = emptySet()) }
        assertThrows(IllegalArgumentException::class.java) { history(classes = setOf("unrecognized")) }
        assertThrows(IllegalArgumentException::class.java) {
            history(classes = setOf("money_path")).record(1, now, flaky())
        }
    }

    @Test
    fun `stable success and stable failure remain distinct from a blocking flake`() {
        val success = history().record(1, now, listOf(attempt(AttemptOutcome.PASSED)))
        assertEquals(FlakeClassification.STABLE_SUCCESS, success.classification)
        assertFalse(success.blocked)
        assertFalse(success.quarantineAdvisory)
        val failed = history().record(1, now, listOf(attempt(AttemptOutcome.FAILED), attempt(AttemptOutcome.FAILED)))
        assertEquals(FlakeClassification.STABLE_FAILURE, failed.classification)
        assertTrue(failed.blocked)
        assertEquals(0, failed.flakesInWindow)
        val restricted = history(classes = setOf("money_path")).record(1, now, listOf(attempt(AttemptOutcome.FAILED)))
        assertEquals(FlakeClassification.STABLE_FAILURE, restricted.classification)
        assertTrue(restricted.blocked)
        val noRetry = history(retries = 0).record(1, now, listOf(attempt(AttemptOutcome.FAILED)))
        assertEquals(FlakeClassification.STABLE_FAILURE, noRetry.classification)
        val flake = history().record(1, now, flaky())
        assertEquals(FlakeClassification.NON_DETERMINISTIC, flake.classification)
        assertTrue(flake.blocked)
        assertEquals(listOf(AttemptOutcome.FAILED, AttemptOutcome.PASSED), flake.attempts.map { it.outcome })
    }

    @Test
    fun `two failure then pass observations flag advice without skipping or erasing earlier outcomes`() {
        val history = history()
        val first = history.record(1, now, flaky())
        assertEquals(1, first.flakesInWindow)
        assertFalse(first.quarantineAdvisory)
        val second = history.record(2, now.plusSeconds(1), flaky())
        assertEquals(2, second.flakesInWindow)
        assertTrue(second.quarantineAdvisory)
        assertTrue(second.blocked)
        val clean = history.record(3, now.plusSeconds(2), listOf(attempt(AttemptOutcome.PASSED)))
        assertTrue(clean.quarantineAdvisory, "a-primary-pass-does-not-erase-advice")
        assertFalse(clean.blocked)
        assertEquals(1, first.flakesInWindow, "older-reports-are-snapshots")
        val json = Json.parseToJsonElement(second.json()).jsonObject
        assertFalse(json.getValue("quarantined").jsonPrimitive.boolean)
        assertFalse(json.getValue("skipped").jsonPrimitive.boolean)
        assertEquals("not_implemented", json.getValue("quarantine_inventory_gates").jsonPrimitive.content)
    }

    @Test
    fun `window includes its exact lower boundary and excludes an observation one nanosecond older`() {
        val lower = now.minus(Duration.ofDays(policy.windowDays.toLong()))
        for ((firstAt, expected) in listOf(lower.minusNanos(1) to 1, lower to 2, lower.plusNanos(1) to 2)) {
            val history = history()
            history.record(1, firstAt, flaky())
            val current = history.record(2, now, flaky())
            assertEquals(expected, current.flakesInWindow)
            assertEquals(expected >= policy.quarantineAfterFlakes, current.quarantineAdvisory)
        }
        val history = history()
        history.record(1, lower.minusNanos(2), flaky())
        history.record(2, lower.minusNanos(1), flaky())
        val expired = history.record(3, now, listOf(attempt(AttemptOutcome.PASSED)))
        assertEquals(0, expired.flakesInWindow)
        assertFalse(expired.quarantineAdvisory)
    }

    @Test
    fun `incomplete and non comparable executions never prove a flake`() {
        val history = history()
        val missingRetry = history.record(1, now, listOf(attempt(AttemptOutcome.FAILED)))
        val interrupted = history.record(2, now, listOf(attempt(AttemptOutcome.FAILED), attempt(AttemptOutcome.INCOMPLETE)))
        val aborted = history.record(3, now, listOf(attempt(AttemptOutcome.INCOMPLETE)))
        for (result in listOf(missingRetry, interrupted, aborted)) {
            assertEquals(FlakeClassification.INCOMPLETE, result.classification)
            assertTrue(result.blocked)
            assertEquals(0, result.flakesInWindow)
            assertFalse(result.quarantineAdvisory)
        }
        for (changed in listOf(identity.copy(source = "c".repeat(64)), identity.copy(configuration = "d".repeat(64)))) {
            val result =
                history().record(
                    1,
                    now,
                    listOf(attempt(AttemptOutcome.FAILED), FlakeAttempt(identity, changed, AttemptOutcome.PASSED, 1)),
                )
            assertEquals(FlakeClassification.NOT_COMPARABLE, result.classification)
            assertTrue(result.blocked)
            assertEquals(0, result.flakesInWindow)
            assertFalse(result.quarantineAdvisory)
            assertThrows(IllegalArgumentException::class.java) { history.nextExecution(changed, now) }
        }
    }

    @Test
    fun `unverified actual inputs never contribute to the flake threshold or permit another retry`() {
        val history = history()
        val changed = attempt(AttemptOutcome.PASSED).copy(inputsComparable = false)
        for (execution in 1..2) {
            val result = history.record(execution, now, listOf(attempt(AttemptOutcome.FAILED), changed))
            assertEquals(FlakeClassification.NOT_COMPARABLE, result.classification)
            assertEquals(0, result.flakesInWindow)
            assertFalse(result.quarantineAdvisory)
            assertTrue(result.blocked)
            val json = Json.parseToJsonElement(result.json()).jsonObject
            assertFalse(
                json
                    .getValue("attempts")
                    .jsonArray
                    .last()
                    .jsonObject
                    .getValue("comparable")
                    .jsonPrimitive.boolean,
            )
        }
        val proven = history.record(3, now, flaky())
        assertEquals(1, proven.flakesInWindow)
        assertFalse(proven.quarantineAdvisory)
        val unverified = attempt(AttemptOutcome.FAILED).copy(inputsComparable = false)
        assertThrows(IllegalArgumentException::class.java) {
            history().record(1, now, listOf(unverified, attempt(AttemptOutcome.PASSED)))
        }
    }

    @Test
    fun `malformed missing repeated out of order and over limit history is refused`() {
        val history = history()
        assertThrows(IllegalArgumentException::class.java) { history.record(1, now, emptyList()) }
        assertThrows(IllegalArgumentException::class.java) { history.record(1, now, flaky() + attempt(AttemptOutcome.PASSED)) }
        assertThrows(IllegalArgumentException::class.java) {
            history.record(1, now, listOf(attempt(AttemptOutcome.PASSED), attempt(AttemptOutcome.PASSED)))
        }
        assertThrows(IllegalArgumentException::class.java) {
            history.record(1, now, listOf(attempt(AttemptOutcome.INCOMPLETE), attempt(AttemptOutcome.PASSED)))
        }
        assertThrows(IllegalArgumentException::class.java) { history.record(2, now, flaky()) }
        history.record(1, now, flaky())
        assertThrows(IllegalArgumentException::class.java) { history.record(1, now, flaky()) }
        assertThrows(IllegalArgumentException::class.java) { history.record(2, now.minusNanos(1), flaky()) }
        assertThrows(IllegalArgumentException::class.java) { history.nextExecution(identity, now.minusNanos(1)) }
        assertThrows(IllegalArgumentException::class.java) { history().record(1, Instant.MIN, flaky()) }
        assertThrows(IllegalArgumentException::class.java) { FlakeAttempt(identity, identity, AttemptOutcome.PASSED, -1) }
        for (index in 2..FlakeHistory.MAX_OBSERVATIONS) {
            history.record(index, now, listOf(attempt(AttemptOutcome.PASSED)))
        }
        assertThrows(IllegalArgumentException::class.java) { history.nextExecution(identity, now) }
        assertThrows(IllegalArgumentException::class.java) {
            history.record(FlakeHistory.MAX_OBSERVATIONS + 1, now, listOf(attempt(AttemptOutcome.PASSED)))
        }
    }

    @Test
    fun `history snapshots are isolated from caller mutation and every new history starts explicitly empty`() {
        val attempts = flaky().toMutableList()
        val firstHistory = history()
        val first = firstHistory.record(1, now, attempts)
        attempts.clear()
        assertEquals(2, first.attempts.size)
        assertThrows(UnsupportedOperationException::class.java) { (first.attempts as MutableList).clear() }
        assertEquals(2, firstHistory.record(2, now, flaky()).flakesInWindow)
        assertEquals(1, history().record(1, now, flaky()).flakesInWindow)
    }

    @Test
    fun `diagnostic metadata is bounded ASCII and rejects untrusted identifiers`() {
        for (id in listOf("", "x".repeat(65), "synthetic-private-marker\n", "\u001b[31mfixture", "fixture/../path", "fixture\u00e9")) {
            val error = assertThrows(IllegalArgumentException::class.java) { identity.copy(test = id) }
            assertEquals("flake-test-identifier", error.message)
        }
        assertThrows(IllegalArgumentException::class.java) { identity.copy(task = "synthetic-private-marker") }
        assertThrows(IllegalArgumentException::class.java) { identity.copy(source = "synthetic-private-marker") }
        assertThrows(IllegalArgumentException::class.java) { identity.copy(configuration = "synthetic-private-marker") }
        val record = history().record(1, now, flaky())
        val output = record.json()
        assertTrue(output.length <= 2048 && output.all { it.code in 32..126 }, "bounded-single-line-ASCII")
        val json = Json.parseToJsonElement(output).jsonObject
        assertEquals(1, json.getValue("retries").jsonPrimitive.int)
        assertEquals(2, json.getValue("attempts").jsonArray.size)
        assertTrue(
            json.getValue("attempts").jsonArray.all { it.jsonObject.keys == setOf("attempt", "outcome", "elapsed_ms", "comparable") },
        )
        assertFalse(listOf("input", "message", "stack", "record", "money", "amount", "minor_units").any { it in json.keys })
    }

    private fun policyBytes(): ByteArray = requireNotNull(javaClass.getResourceAsStream(FlakePolicy.RESOURCE)).use { it.readAllBytes() }

    private fun history(
        classes: Set<String> = setOf("api_service"),
        retries: Int? = null,
    ): FlakeHistory = FlakeHistory(policy, identity, classes, retries)

    private fun attempt(outcome: AttemptOutcome): FlakeAttempt = FlakeAttempt(identity, identity, outcome, 1)

    private fun flaky(): List<FlakeAttempt> = listOf(attempt(AttemptOutcome.FAILED), attempt(AttemptOutcome.PASSED))
}
