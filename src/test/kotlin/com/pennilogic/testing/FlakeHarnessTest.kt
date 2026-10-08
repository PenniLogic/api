package com.pennilogic.testing

import io.kotest.property.Arb
import io.kotest.property.RandomSource
import io.kotest.property.Sample
import io.kotest.property.arbitrary.constant
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertSame
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.parallel.Isolated
import org.junit.jupiter.api.parallel.ResourceLock
import org.junit.jupiter.api.parallel.Resources
import org.opentest4j.TestAbortedException
import java.time.Clock
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@Isolated("Captures the property library's process streams")
@ResourceLock(Resources.SYSTEM_OUT)
@ResourceLock(Resources.SYSTEM_ERR)
class FlakeHarnessTest {
    @Test
    fun `normal shared property failures emit advisory evidence`() {
        val captured =
            capturePropertyFailure { record ->
                checkProperty("advisory-money-control", 1, SharedGenerators.ledger(), onResult = record) { case ->
                    case.verify("deliberate-invariant-violation", false)
                }
            }
        captured.report()
        assertTrue(captured.output.contains("\"event\":\"flaky_test_observation\""), "shared-harness-emits-advisory")
        val record = event(captured.output)
        assertTrue(record.contains("\"classification\":\"stable_failure\""), "money-remains-a-real-failure")
        assertTrue(record.contains("\"retries\":0"), "money-is-not-reexecuted")
        println(record)
    }

    @Test
    fun `the real varying fixture flags on the second flake and every retry pass still throws`() {
        var engines = 0
        val observations = mutableListOf<FlakeObservation>()
        val executions = mutableListOf<PropertyExecution>()
        val runner =
            propertyCheck(
                "synthetic-varying-fixture",
                1,
                {
                    engines++
                    fixture()
                },
                setOf("api_service"),
                clock = clock(),
                onResult = { executions.add(it) },
                onObservation = { observations.add(it) },
            ) { case ->
                case.verify("deliberately-varying-result", engines % 2 == 0)
            }
        repeat(3) { index ->
            val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            val observation = observations.last()
            assertEquals(FlakeClassification.NON_DETERMINISTIC, observation.classification)
            assertEquals(index + 1, observation.flakesInWindow)
            assertEquals(index + 1 >= FlakePolicy.accepted().quarantineAfterFlakes, observation.quarantineAdvisory)
            assertTrue(observation.blocked)
            assertEquals(listOf(AttemptOutcome.FAILED, AttemptOutcome.PASSED), observation.attempts.map { it.outcome })
            assertTrue(failure.message.orEmpty().contains("deliberately-varying-result"), "first-failure-retained")
            assertEquals(observation.json(), event(output), "actual-engine-output-is-the-recorded-observation")
        }
        assertEquals(6, engines, "advice-never-skips-execution")
        assertEquals(6, executions.size)
        executions.forEach { println(it.json()) }
        observations.forEach { println(it.json()) }
        if (System.getProperty("pennilogic.testing.flakeNegativeControl") == "true") {
            runner.run()
            error("flake-negative-control-did-not-fail")
        }

        val recovery = mutableListOf<FlakeObservation>()
        propertyCheck(
            "synthetic-recovery",
            1,
            ::fixture,
            setOf("api_service"),
            clock = clock(),
            onObservation = { recovery.add(it) },
        ) { case -> case.verify("stable-recovery", true) }.run()
        assertEquals(1, recovery.single().attempts.size)
        assertEquals(FlakeClassification.STABLE_SUCCESS, recovery.single().classification)
        assertEquals(0, recovery.single().flakesInWindow)
        assertFalse(recovery.single().quarantineAdvisory)
    }

    @Test
    fun `stable failures retain the first assertion and the permitted second failure`() {
        val observations = mutableListOf<FlakeObservation>()
        var calls = 0
        val first = AssertionError("synthetic-first-failure")
        val second = AssertionError("synthetic-second-failure")
        val runner =
            check(observations) {
                calls++
                throw if (calls == 1) first else second
            }
        val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertSame(first, failure)
        assertSame(second, failure.suppressed.single())
        assertEquals(2, calls)
        assertEquals(FlakeClassification.STABLE_FAILURE, observations.single().classification)
        assertEquals(0, observations.single().flakesInWindow)
        assertTrue(observations.single().blocked)
        println(event(output))

        val identical = check(mutableListOf()) { throw first }
        capturePropertyOutput { assertSame(first, assertThrows(AssertionError::class.java) { identical.run() }) }
    }

    @Test
    fun `interruption before or after an assertion is blocking and never evidence of nondeterminism`() {
        for (failFirst in listOf(false, true)) {
            val observations = mutableListOf<FlakeObservation>()
            var calls = 0
            val interruption = IllegalStateException("synthetic-incomplete-execution")
            val runner =
                check(observations) {
                    calls++
                    if (failFirst && calls == 1) throw AssertionError("synthetic-first-failure")
                    throw interruption
                }
            val (failure, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
            assertSame(interruption, failure)
            assertEquals(if (failFirst) 2 else 1, calls)
            val observation = observations.single()
            assertEquals(FlakeClassification.INCOMPLETE, observation.classification)
            assertEquals(AttemptOutcome.INCOMPLETE, observation.attempts.last().outcome)
            if (failFirst) assertEquals(AttemptOutcome.FAILED, observation.attempts.first().outcome)
            assertEquals(0, observation.flakesInWindow)
            assertFalse(observation.quarantineAdvisory)
            assertTrue(observation.blocked)
            println(event(output))
        }
    }

    @Test
    fun `an interrupted actual property is not mistaken for a completed assertion failure`() {
        for (interruption in listOf(IllegalStateException("synthetic-interruption"), TestAbortedException("synthetic-abort"))) {
            var engines = 0
            val observations = mutableListOf<FlakeObservation>()
            val runner =
                propertyCheck(
                    "synthetic-property-interruption",
                    1,
                    {
                        engines++
                        fixture()
                    },
                    setOf("api_service"),
                    onObservation = { observations.add(it) },
                ) { if (engines == 1) throw interruption }
            val (failure, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
            assertEquals("property-execution-incomplete", failure.message)
            assertEquals(1, engines, "interrupted-property-is-not-reexecuted")
            assertEquals(FlakeClassification.INCOMPLETE, observations.single().classification)
            assertFalse(observations.single().quarantineAdvisory)
            assertTrue(observations.single().blocked)
            println(event(output))
        }
    }

    @Test
    fun `an aborted shared check becomes a blocking failure rather than a JUnit skip`() {
        val observations = mutableListOf<FlakeObservation>()
        var calls = 0
        val runner =
            check(observations) {
                calls++
                throw TestAbortedException("synthetic-abort")
            }
        val (error, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
        assertEquals("flake-execution-aborted", error.message)
        assertEquals(1, calls)
        assertEquals(FlakeClassification.INCOMPLETE, observations.single().classification)
        assertTrue(observations.single().blocked)
        println(event(output))
    }

    @Test
    fun `assertions in fixture construction or sampling are not completed test failures`() {
        for (duringSampling in listOf(false, true)) {
            var factories = 0
            var callbacks = 0
            val observations = mutableListOf<FlakeObservation>()
            val runner =
                propertyCheck(
                    "synthetic-generator-interruption",
                    1,
                    {
                        factories++
                        if (!duringSampling) throw AssertionError("synthetic-construction-interruption")
                        object : Arb<FixtureCase<Unit>>() {
                            override fun edgecase(rs: RandomSource): Sample<FixtureCase<Unit>>? = null

                            override fun sample(rs: RandomSource): Sample<FixtureCase<Unit>> =
                                throw AssertionError("synthetic-sampling-interruption")
                        }
                    },
                    setOf("api_service"),
                    onObservation = { observations.add(it) },
                ) { callbacks++ }
            val (error, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
            assertEquals("property-execution-incomplete", error.message)
            assertEquals(1, factories)
            assertEquals(0, callbacks)
            assertEquals(FlakeClassification.INCOMPLETE, observations.single().classification)
            assertTrue(observations.single().blocked)
            println(event(output))
        }
    }

    @Test
    fun `changed source or configuration does not become a flake or a new empty history`() {
        for (sourceChanged in listOf(false, true)) {
            val original = identity()
            var current = original
            var calls = 0
            val observations = mutableListOf<FlakeObservation>()
            val runner =
                AdvisoryCheck(FlakePolicy.accepted(), { current }, setOf("api_service"), clock = clock(), onObservation = {
                    observations.add(it)
                }) {
                    calls++
                    current = if (sourceChanged) original.copy(source = "c".repeat(64)) else original.copy(configuration = "d".repeat(64))
                    throw AssertionError("synthetic-first-failure")
                }
            val (_, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            assertEquals(1, calls, "changed-context-is-not-reexecuted")
            assertEquals(FlakeClassification.NOT_COMPARABLE, observations.single().classification)
            assertEquals(0, observations.single().flakesInWindow)
            assertFalse(observations.single().quarantineAdvisory)
            assertThrows(IllegalArgumentException::class.java) { runner.run() }
            assertEquals(1, calls, "changed-context-cannot-reuse-history")
            println(event(output))
        }
    }

    @Test
    fun `money classes refuse retries before execution and run a failure exactly once`() {
        val policy = FlakePolicy.accepted()
        var calls = 0
        assertThrows(IllegalArgumentException::class.java) {
            AdvisoryCheck(policy, ::identity, setOf("api_service", "money_path"), requestedRetries = 1) { calls++ }
        }
        assertEquals(0, calls)
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            AdvisoryCheck(policy, ::identity, setOf("api_service", "money_path"), onObservation = { observations.add(it) }) {
                calls++
                throw AssertionError("synthetic-money-failure")
            }
        capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertEquals(1, calls)
        assertEquals(FlakeClassification.STABLE_FAILURE, observations.single().classification)
        assertEquals(1, observations.single().attempts.size)
        assertTrue(observations.single().blocked)
    }

    @Test
    fun `one runner refuses overlapping execution and remains usable after its owner completes`() {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            check(observations) {
                entered.countDown()
                assertTrue(release.await(5, TimeUnit.SECONDS), "owned-execution-released")
            }
        Executors.newSingleThreadExecutor().use { executor ->
            val first = executor.submit { runner.run() }
            try {
                assertTrue(entered.await(5, TimeUnit.SECONDS), "owned-execution-started")
                val error = assertThrows(IllegalStateException::class.java) { runner.run() }
                assertEquals("flake-concurrent-execution", error.message)
            } finally {
                release.countDown()
            }
            first.get(5, TimeUnit.SECONDS)
        }
        runner.run()
        assertEquals(listOf(1, 2), observations.map { it.execution })
        assertTrue(observations.all { !it.blocked && it.attempts.size == 1 })
    }

    @Test
    fun `a clock moving backwards inside an execution cannot create a flake observation`() {
        var instant = Instant.parse("2026-10-08T00:00:00Z")
        val moving =
            object : Clock() {
                override fun getZone(): ZoneId = ZoneOffset.UTC

                override fun withZone(zone: ZoneId): Clock = throw UnsupportedOperationException("synthetic-clock-zone")

                override fun instant(): Instant = instant
            }
        val observations = mutableListOf<FlakeObservation>()
        var calls = 0
        val runner =
            AdvisoryCheck(FlakePolicy.accepted(), ::identity, setOf("api_service"), clock = moving, onObservation = {
                observations.add(it)
            }) {
                calls++
                if (calls == 1) throw AssertionError("synthetic-first-failure")
                instant = instant.minusNanos(1)
            }
        val error = assertThrows(IllegalArgumentException::class.java) { runner.run() }
        assertEquals("flake-history-time", error.message)
        assertTrue(observations.isEmpty())
    }

    @Test
    fun `normal property use refuses missing execution metadata before exercising its fixture`() {
        var calls = 0
        for (name in listOf("pennilogic.testing.task", "pennilogic.testing.source")) {
            val original = requireNotNull(System.getProperty(name))
            try {
                System.clearProperty(name)
                assertThrows(IllegalArgumentException::class.java) {
                    propertyCheck("synthetic-missing-metadata", 1, {
                        calls++
                        fixture()
                    }, setOf("api_service")) {}
                }
                assertEquals(0, calls)
            } finally {
                System.setProperty(name, original)
            }
        }
        propertyCheck("synthetic-metadata-recovery", 1, {
            calls++
            fixture()
        }, setOf("api_service")) {}.run()
        assertEquals(1, calls)
    }

    private fun fixture(): Arb<FixtureCase<Unit>> =
        Arb.constant(FixtureCase("synthetic-fixed-input", Unit, ExpectedInvariant.HARNESS_STABILITY))

    private fun clock(): Clock = Clock.fixed(Instant.parse("2026-10-08T00:00:00Z"), ZoneOffset.UTC)

    private fun identity(): FlakeIdentity =
        FlakeIdentity("synthetic-execution", 7, TestCategory.UNIT, "test", "a".repeat(64), "b".repeat(64))

    private fun check(
        observations: MutableList<FlakeObservation>,
        action: () -> Unit,
    ): AdvisoryCheck =
        AdvisoryCheck(FlakePolicy.accepted(), ::identity, setOf("api_service"), clock = clock(), onObservation = {
            observations.add(it)
        }, action = action)

    private fun event(output: String): String {
        val line = output.lineSequence().single { it.startsWith("{\"event\":\"flaky_test_observation\"") }
        val json = Json.parseToJsonElement(line).jsonObject
        assertTrue(json.keys.none { it in setOf("input", "message", "record", "amount", "minor_units") }, "no-payload-fields")
        assertTrue(line.length <= 2048 && line.all { it.code in 32..126 }, "bounded-single-line-ASCII")
        return line
    }
}
