package com.pennilogic.testing

import io.kotest.property.Arb
import io.kotest.property.arbitrary.constant
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Nested
import org.junit.jupiter.api.RepeatedTest
import org.junit.jupiter.api.RepetitionInfo
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.extension.AfterAllCallback
import org.junit.jupiter.api.extension.BeforeAllCallback
import org.junit.jupiter.api.extension.ExtendWith
import org.junit.jupiter.api.extension.ExtensionContext
import org.junit.jupiter.api.parallel.Isolated
import org.junit.jupiter.api.parallel.ResourceLock
import org.junit.jupiter.api.parallel.Resources
import org.opentest4j.TestAbortedException
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.OutputStream
import java.io.PrintStream
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@Tag("property")
@Isolated("Captures actual property diagnostics")
@ResourceLock(Resources.SYSTEM_OUT)
@ResourceLock(Resources.SYSTEM_ERR)
@ExtendWith(ClosedPropertyScopeProbe::class, PropertyChecksExtension::class)
class NormalPropertyLifecycleTest {
    @RepeatedTest(3)
    fun `ordinary normal invocations share advice and never skip`(
        checks: PropertyChecks,
        repetition: RepetitionInfo,
    ) {
        normalIntermittent(checks, repetition.currentRepetition)
    }

    @Nested
    inner class NestedHistory {
        @Test
        fun `nested class does not inherit its parents history`(checks: PropertyChecks) {
            normalIntermittent(checks, 1)
        }
    }
}

@Tag("property")
@Isolated("Captures actual property diagnostics")
@ResourceLock(Resources.SYSTEM_OUT)
@ResourceLock(Resources.SYSTEM_ERR)
@ExtendWith(ClosedPropertyScopeProbe::class, PropertyChecksExtension::class)
class SeparatePropertyLifecycleTest {
    @Test
    fun `the identical property starts fresh in another Jupiter class`(checks: PropertyChecks) {
        normalIntermittent(checks, 1)
    }
}

internal class ClosedPropertyScopeProbe :
    BeforeAllCallback,
    AfterAllCallback {
    override fun beforeAll(context: ExtensionContext) {
        val scope = PropertyChecksExtension.scope(context)
        assertFalse(scope.isClosed)
        assertEquals(0, scope.retainedHistories)
    }

    override fun afterAll(context: ExtensionContext) {
        val scope = PropertyChecksExtension.scope(context)
        assertTrue(scope.isClosed, "jupiter-after-all-closed-the-shared-scope")
        assertEquals(0, scope.retainedHistories)
        val failure =
            assertThrows(IllegalStateException::class.java) {
                scope.checkProperty("closed-scope-control", 1, ::lifecycleFixture, setOf("api_service")) {}
            }
        assertEquals("property-history-closed", failure.message)
        println("""{"event":"property_history_cleanup","closed":true,"retained_histories":0}""")
    }
}

private fun normalIntermittent(
    checks: PropertyChecks,
    expectedExecution: Int,
) {
    var calls = 0
    val original = AssertionError("synthetic-normal-original")
    val observations = mutableListOf<FlakeObservation>()

    fun invoke() =
        checks.checkProperty(
            "normal-intermittent",
            1,
            ::lifecycleFixture,
            setOf("api_service"),
            onObservation = { observations.add(it) },
        ) {
            calls++
            if (calls == 1) throw original
        }
    if (System.getProperty("pennilogic.testing.flakeNegativeControl") == "true") {
        invoke()
        error("normal-intermittent-negative-control-did-not-fail")
    }
    val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { invoke() } }
    assertTrue(generateSequence<Throwable>(failure) { it.cause }.any { it === original }, "normal-original-assertion-retained")
    val observation = observations.single()
    assertEquals(expectedExecution, observation.execution)
    assertEquals(expectedExecution, observation.flakesInWindow)
    assertEquals(expectedExecution >= FlakePolicy.accepted().quarantineAfterFlakes, observation.quarantineAdvisory)
    assertEquals(FlakeClassification.NON_DETERMINISTIC, observation.classification)
    assertEquals(listOf(AttemptOutcome.FAILED, AttemptOutcome.PASSED), observation.attempts.map { it.outcome })
    assertTrue(observation.blocked && observation.reportingComplete)
    assertEquals(2, calls)
    assertTrue(output.contains("\"history_scope\":\"junit_test_class\""))
    assertTrue(output.contains("\"quarantined\":false,\"skipped\":false"))
    println(output.trim())
}

private fun lifecycleFixture(): Arb<FixtureCase<Unit>> =
    Arb.constant(FixtureCase("synthetic-lifecycle-case", Unit, ExpectedInvariant.HARNESS_STABILITY))

@Tag("property")
@Isolated("Exercises process metadata and captures actual property diagnostics")
@ResourceLock(Resources.SYSTEM_OUT)
@ResourceLock(Resources.SYSTEM_ERR)
class PropertyLifecycleTest {
    @Test
    fun `normal money and mixed class failures have no retries or flake credit`() {
        for (classes in listOf(setOf("money_path"), setOf("api_service", "money_path"))) {
            PropertyChecks().use { checks ->
                var factories = 0
                val observations = mutableListOf<FlakeObservation>()
                repeat(3) {
                    val original = AssertionError("synthetic-money-original")
                    val (failure, output) =
                        capturePropertyOutput {
                            assertThrows(AssertionError::class.java) {
                                checks.checkProperty("normal-money", 1, {
                                    factories++
                                    lifecycleFixture()
                                }, classes, onObservation = { observations.add(it) }) { throw original }
                            }
                        }
                    assertTrue(generateSequence<Throwable>(failure) { it.cause }.any { it === original })
                    println(output.trim())
                }
                assertEquals(3, factories)
                assertEquals(listOf(1, 2, 3), observations.map { it.execution })
                assertTrue(observations.all { it.classification == FlakeClassification.STABLE_FAILURE && it.attempts.size == 1 })
                assertTrue(observations.all { it.flakesInWindow == 0 && !it.quarantineAdvisory && it.blocked })
                assertThrows(IllegalArgumentException::class.java) {
                    checks.checkProperty("normal-money-invalid", 1, ::lifecycleFixture, classes, requestedRetries = 1) {}
                }
            }
        }
    }

    @Test
    fun `normal history refuses changed actual inputs and recovers without resetting old evidence`() {
        PropertyChecks().use { checks ->
            var factories = 0
            val observations = mutableListOf<FlakeObservation>()
            for (input in listOf(0, 1, 0)) {
                var failed = false
                val (_, output) =
                    capturePropertyOutput {
                        assertThrows(AssertionError::class.java) {
                            checks.checkProperty("normal-input-binding", 1, {
                                factories++
                                Arb.constant(FixtureCase("synthetic-lifecycle-case", input, ExpectedInvariant.HARNESS_STABILITY))
                            }, setOf("api_service"), onObservation = { observations.add(it) }) {
                                if (!failed) {
                                    failed = true
                                    throw AssertionError("synthetic-normal-original")
                                }
                            }
                        }
                    }
                println(output.trim())
            }
            assertEquals(listOf(1, 1, 2), observations.map { it.flakesInWindow })
            assertEquals(listOf(2, 1, 2), observations.map { it.attempts.size })
            assertEquals(FlakeClassification.NOT_COMPARABLE, observations[1].classification)
            assertFalse(observations[1].quarantineAdvisory)
            assertTrue(observations[2].quarantineAdvisory)
            assertEquals(5, factories)
        }
    }

    @Test
    fun `changed retry samples and unverifiable objects never earn normal history credit`() {
        PropertyChecks().use { checks ->
            var factories = 0
            val observations = mutableListOf<FlakeObservation>()
            repeat(3) {
                capturePropertyOutput {
                    assertThrows(AssertionError::class.java) {
                        checks.checkProperty("normal-changing-retry", 1, {
                            val value = factories++ % 2
                            Arb.constant(FixtureCase("synthetic-lifecycle-case", value, ExpectedInvariant.HARNESS_STABILITY))
                        }, setOf("api_service"), onObservation = { observations.add(it) }) { case ->
                            case.verify("synthetic-input-predicate", case.input == 1)
                        }
                    }
                }
            }
            assertEquals(6, factories)
            assertTrue(observations.all { it.classification == FlakeClassification.NOT_COMPARABLE && it.flakesInWindow == 0 })
            var unknownFactories = 0
            var fail = true
            val unknown = mutableListOf<FlakeObservation>()
            val input =
                object {
                    override fun toString(): String = error("synthetic-private-sample")

                    override fun hashCode(): Int = error("synthetic-private-sample")

                    override fun equals(other: Any?): Boolean = error("synthetic-private-sample")
                }

            fun invoke() =
                checks.checkProperty("normal-unverifiable", 1, {
                    unknownFactories++
                    Arb.constant(FixtureCase("synthetic-lifecycle-case", input, ExpectedInvariant.HARNESS_STABILITY))
                }, setOf("api_service"), onObservation = { unknown.add(it) }) {
                    if (fail) throw AssertionError("synthetic-normal-original")
                }
            val (_, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { invoke() } }
            fail = false
            capturePropertyOutput { invoke() }
            assertFalse(output.contains("synthetic-private-sample"))
            assertEquals(2, unknownFactories)
            assertEquals(FlakeClassification.NOT_COMPARABLE, unknown[0].classification)
            assertEquals(FlakeClassification.STABLE_SUCCESS, unknown[1].classification)
            assertTrue(unknown.all { it.flakesInWindow == 0 && !it.quarantineAdvisory })
        }
    }

    @Test
    fun `incomplete observation and property reporting cannot poison normal history`() {
        PropertyChecks().use { checks ->
            val healthyReports = mutableListOf<FlakeObservation>()
            val expected = List(4) { "incomplete" } + List(2) { "non_deterministic" }
            var factories = 0
            for (mode in 0..5) {
                var attempt = 0
                val original = AssertionError("synthetic-normal-original")
                val (failure, output) =
                    capturePropertyOutput {
                        assertThrows(Throwable::class.java) {
                            checks.checkProperty("normal-reporting", 1, {
                                attempt++
                                factories++
                                lifecycleFixture()
                            }, setOf("api_service"), onResult = {
                                if (mode == 0) throw AssertionError("synthetic-private-reporter")
                                if (mode == 2) throw TestAbortedException("synthetic-private-reporter")
                                if (mode == 3) throw IllegalStateException("synthetic-private-reporter")
                            }, onObservation = {
                                if (mode == 1) throw TestAbortedException("synthetic-private-reporter")
                                healthyReports.add(it)
                            }) {
                                if (attempt == 1) throw original
                            }
                        }
                    }
                assertFalse(failure is TestAbortedException)
                assertTrue(retainsLifecycleFailure(failure, original), "reporter-retains-original-assertion")
                assertTrue(output.contains("\"classification\":\"${expected[mode]}\""))
                assertFalse((output + failure.stackTraceToString()).contains("synthetic-private-reporter"))
                println(output.trim())
            }
            assertEquals(9, factories)
            assertEquals(listOf(0, 0, 0, 1, 2), healthyReports.map { it.flakesInWindow })
            assertTrue(healthyReports.last().quarantineAdvisory)
        }
    }

    @Test
    fun `an aborted output stream is blocking and cannot credit normal history`() {
        for (abort in listOf(true, false)) {
            PropertyChecks().use { checks ->
                val observations = mutableListOf<FlakeObservation>()
                var breakOutput = true
                var calls = 0
                val output =
                    if (abort) {
                        object : PrintStream(ByteArrayOutputStream()) {
                            override fun println(value: String?): Unit = throw TestAbortedException("synthetic-private-output")

                            override fun println(value: Any?): Unit = throw TestAbortedException("synthetic-private-output")
                        }
                    } else {
                        PrintStream(
                            object : OutputStream() {
                                override fun write(value: Int): Unit = throw IOException("synthetic-private-output")
                            },
                        )
                    }

                fun invoke() =
                    checks.checkProperty("normal-output-failure", 1, ::lifecycleFixture, setOf("api_service"), onObservation = {
                        if (breakOutput) System.setOut(output) else observations.add(it)
                    }) {
                        calls++
                        if (calls % 2 == 1) throw AssertionError("synthetic-normal-original")
                    }
                try {
                    val (failure, captured) =
                        capturePropertyOutput {
                            val original = System.out
                            try {
                                assertThrows(AssertionError::class.java) { invoke() }
                            } finally {
                                System.setOut(original)
                            }
                        }
                    assertTrue(failure.suppressed.any { it.message == "flake-observation-reporting-failed" })
                    assertFalse((failure.stackTraceToString() + captured).contains("synthetic-private-output"))
                    breakOutput = false
                    repeat(2) { capturePropertyOutput { assertThrows(AssertionError::class.java) { invoke() } } }
                    assertEquals(listOf(2, 3), observations.map { it.execution })
                    assertEquals(listOf(1, 2), observations.map { it.flakesInWindow })
                    assertEquals(6, calls)
                } finally {
                    output.close()
                }
            }
        }
    }

    @Test
    fun `lost execution metadata preserves the original assertion without replacing history`() {
        PropertyChecks().use { checks ->
            val key = "pennilogic.testing.source"
            val source = requireNotNull(System.getProperty(key))
            val original = AssertionError("synthetic-metadata-original")
            val observations = mutableListOf<FlakeObservation>()
            var removeMetadata = false
            var attempt = 0

            fun invoke() =
                checks.checkProperty(
                    "normal-lost-metadata",
                    1,
                    ::lifecycleFixture,
                    setOf("api_service"),
                    onObservation = { observations.add(it) },
                ) {
                    attempt++
                    if (attempt == 1) {
                        if (removeMetadata) System.clearProperty(key)
                        throw original
                    }
                }
            capturePropertyOutput { assertThrows(AssertionError::class.java) { invoke() } }
            removeMetadata = true
            attempt = 0
            try {
                val (failure, _) =
                    capturePropertyOutput { assertThrows(IllegalArgumentException::class.java) { invoke() } }
                assertTrue(retainsLifecycleFailure(failure, original), "metadata-refusal-must-retain-original-assertion")
            } finally {
                System.setProperty(key, source)
            }
            removeMetadata = false
            attempt = 0
            capturePropertyOutput { assertThrows(AssertionError::class.java) { invoke() } }
            assertEquals(listOf(1, 2), observations.map { it.flakesInWindow })
            assertEquals(listOf(1, 2), observations.map { it.execution })
            assertTrue(observations.last().quarantineAdvisory)
        }
    }

    @Test
    fun `normal history binds source configuration and predicate factories without silent reset`() {
        PropertyChecks().use { checks ->
            var factories = 0
            val observations = mutableListOf<FlakeObservation>()

            fun invoke(
                id: String = "normal-identity",
                cases: Int = 1,
                classes: Set<String> = setOf("api_service"),
            ) = checks.checkProperty(id, cases, {
                factories++
                lifecycleFixture()
            }, classes, onObservation = { observations.add(it) }) {}
            capturePropertyOutput { invoke() }
            val key = "pennilogic.testing.source"
            val original = requireNotNull(System.getProperty(key))
            try {
                System.setProperty(key, "c".repeat(64))
                val failure = assertThrows(IllegalArgumentException::class.java) { invoke("different-property") }
                assertEquals("property-history-environment", failure.message)
                System.clearProperty(key)
                assertEquals("flake-source-missing", assertThrows(IllegalArgumentException::class.java) { invoke() }.message)
            } finally {
                System.setProperty(key, original)
            }
            assertEquals("property-history-binding", assertThrows(IllegalArgumentException::class.java) { invoke(cases = 2) }.message)
            assertEquals(
                "property-history-binding",
                assertThrows(IllegalArgumentException::class.java) { invoke(classes = setOf("money_path")) }.message,
            )
            assertEquals(
                "property-history-binding",
                assertThrows(IllegalArgumentException::class.java) {
                    checks.checkProperty("normal-identity", 1, ::lifecycleFixture, setOf("api_service")) {}
                }.message,
            )
            capturePropertyOutput { invoke() }
            assertEquals(2, factories)
            assertEquals(listOf(1, 2), observations.map { it.execution })
            assertEquals(1, checks.retainedHistories)
        }
    }

    @Test
    fun `normal property count and observation capacity fail explicitly and close clears ownership`() {
        val checks = PropertyChecks()
        var calls = 0

        fun invoke(
            scope: PropertyChecks,
            id: String,
        ) = scope.checkProperty(id, 1, ::lifecycleFixture, setOf("api_service")) { calls++ }
        try {
            capturePropertyOutput {
                repeat(PropertyChecks.MAX_PROPERTIES) { invoke(checks, "bounded-property-$it") }
                assertEquals(
                    "property-history-capacity",
                    assertThrows(IllegalArgumentException::class.java) { invoke(checks, "one-property-too-many") }.message,
                )
                repeat(FlakeHistory.MAX_OBSERVATIONS - 1) { invoke(checks, "bounded-property-0") }
                assertEquals(
                    "flake-history-capacity",
                    assertThrows(IllegalArgumentException::class.java) { invoke(checks, "bounded-property-0") }.message,
                )
            }
            assertEquals(PropertyChecks.MAX_PROPERTIES + FlakeHistory.MAX_OBSERVATIONS - 1, calls)
            assertEquals(PropertyChecks.MAX_PROPERTIES, checks.retainedHistories)
        } finally {
            checks.close()
        }
        assertTrue(checks.isClosed)
        assertEquals(0, checks.retainedHistories)
        checks.close()
        assertEquals(
            "property-history-closed",
            assertThrows(IllegalStateException::class.java) { invoke(checks, "bounded-property-0") }.message,
        )
        PropertyChecks().use { fresh -> capturePropertyOutput { invoke(fresh, "bounded-property-0") } }
        assertEquals(PropertyChecks.MAX_PROPERTIES + FlakeHistory.MAX_OBSERVATIONS, calls)
        println(
            """{"event":"property_history_capacity","properties":64,"observations_per_property":256,"overflow_refused":true,"closed":true}""",
        )
    }

    @Test
    fun `normal concurrent calls share a lease and active close cannot destroy history`() {
        val checks = PropertyChecks()
        val entered = CountDownLatch(1)
        val released = CountDownLatch(1)
        val executor = Executors.newSingleThreadExecutor()

        fun invoke(id: String) =
            checks.checkProperty(id, 1, ::lifecycleFixture, setOf("api_service")) {
                if (id == "normal-concurrent") {
                    entered.countDown()
                    check(released.await(10, TimeUnit.SECONDS)) { "synthetic-latch-timeout" }
                }
            }
        try {
            capturePropertyOutput {
                val first = executor.submit { invoke("normal-concurrent") }
                try {
                    assertTrue(entered.await(10, TimeUnit.SECONDS))
                    assertEquals(
                        "flake-concurrent-execution",
                        assertThrows(IllegalStateException::class.java) { invoke("normal-concurrent") }.message,
                    )
                    assertEquals(
                        "property-history-active-close",
                        assertThrows(IllegalStateException::class.java) { checks.close() }.message,
                    )
                    invoke("normal-independent")
                    assertEquals(2, checks.retainedHistories)
                } finally {
                    released.countDown()
                }
                first.get(10, TimeUnit.SECONDS)
                invoke("normal-concurrent")
            }
        } finally {
            released.countDown()
            executor.shutdown()
            assertTrue(executor.awaitTermination(10, TimeUnit.SECONDS))
            checks.close()
        }
        assertTrue(checks.isClosed)
        assertEquals(0, checks.retainedHistories)
    }

    @Test
    fun `normal history uses the exact inclusive observation window`() {
        for ((extraNanos, expected) in listOf(-1L to 2, 0L to 2, 1L to 1)) {
            val clock = LifecycleClock()
            PropertyChecks(clock).use { checks ->
                val observations = mutableListOf<FlakeObservation>()
                repeat(2) { execution ->
                    if (execution == 1) {
                        clock.now = clock.now.plus(Duration.ofDays(FlakePolicy.accepted().windowDays.toLong())).plusNanos(extraNanos)
                    }
                    var attempt = 0
                    capturePropertyOutput {
                        assertThrows(AssertionError::class.java) {
                            checks.checkProperty(
                                "normal-window",
                                1,
                                ::lifecycleFixture,
                                setOf("api_service"),
                                onObservation = { observations.add(it) },
                            ) {
                                attempt++
                                if (attempt == 1) throw AssertionError("synthetic-window-original")
                            }
                        }
                    }
                }
                assertEquals(expected, observations.last().flakesInWindow)
                assertEquals(expected == 2, observations.last().quarantineAdvisory)
            }
        }
    }
}

private fun retainsLifecycleFailure(
    failure: Throwable,
    original: Throwable,
): Boolean =
    failure === original ||
        failure.cause?.let { retainsLifecycleFailure(it, original) } == true ||
        failure.suppressed.any { retainsLifecycleFailure(it, original) }

private class LifecycleClock : Clock() {
    var now: Instant = Instant.parse("2026-10-08T00:00:00Z")

    override fun instant(): Instant = now

    override fun getZone(): ZoneId = ZoneOffset.UTC

    override fun withZone(zone: ZoneId): Clock {
        require(zone == ZoneOffset.UTC) { "synthetic-clock-zone" }
        return this
    }
}
