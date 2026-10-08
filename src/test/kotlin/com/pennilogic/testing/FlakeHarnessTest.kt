package com.pennilogic.testing

import io.kotest.property.Arb
import io.kotest.property.RTree
import io.kotest.property.RandomSource
import io.kotest.property.Sample
import io.kotest.property.Shrinker
import io.kotest.property.arbitrary.constant
import io.kotest.property.sampleOf
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
import org.opentest4j.AssertionFailedError
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
    fun `an assertion interrupting shrinking cannot become a flake`() {
        var engines = 0
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-shrinking-interruption",
                1,
                {
                    engines++
                    object : Arb<FixtureCase<Unit>>() {
                        override fun edgecase(rs: RandomSource): Sample<FixtureCase<Unit>>? = null

                        override fun sample(rs: RandomSource): Sample<FixtureCase<Unit>> =
                            sampleOf(
                                FixtureCase("synthetic-fixed-input", Unit, ExpectedInvariant.HARNESS_STABILITY),
                                Shrinker { throw AssertionError("synthetic-shrinking-interruption") },
                            )
                    }
                },
                setOf("api_service"),
                onObservation = { observations.add(it) },
            ) { case -> case.verify("synthetic-callback-assertion", engines != 1) }
        val (error, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
        assertEquals("property-execution-incomplete", error.message)
        assertEquals(1, engines)
        assertEquals(FlakeClassification.INCOMPLETE, observations.single().classification)
        assertTrue(observations.single().blocked)
        println(event(output))
    }

    @Test
    fun `an assertion interrupting result reporting cannot become a flake`() {
        var engines = 0
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-reporting-interruption",
                1,
                {
                    engines++
                    fixture()
                },
                setOf("api_service"),
                onResult = { if (engines == 1) throw AssertionError("synthetic-reporting-interruption") },
                onObservation = { observations.add(it) },
            ) {}
        val (error, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
        assertEquals("property-execution-incomplete", error.message)
        assertEquals(1, engines)
        assertEquals(FlakeClassification.INCOMPLETE, observations.single().classification)
        assertTrue(observations.single().blocked)
        println(event(output))
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

    @Test
    fun `observation reporter abort cannot replace an ordinary assertion`() {
        reporterAbort(setOf("api_service"), "synthetic-ordinary-reporter")
    }

    @Test
    fun `observation reporter abort cannot replace a money assertion`() {
        reporterAbort(setOf("money_path"), "synthetic-money-reporter")
    }

    @Test
    fun `observation reporter abort cannot replace a mixed class assertion`() {
        reporterAbort(setOf("api_service", "money_path"), "synthetic-mixed-reporter")
    }

    @Test
    fun `observation reporter errors retain original failure objects without their own payload`() {
        for (reporter in listOf(
            AssertionError("synthetic-private-reporter"),
            IllegalStateException("synthetic-private-reporter"),
            LinkageError("synthetic-private-reporter"),
        )) {
            val first = AssertionError("synthetic-first-assertion")
            var calls = 0
            val runner =
                AdvisoryCheck(
                    FlakePolicy.accepted(),
                    ::identity,
                    setOf("api_service"),
                    onObservation = { throw reporter },
                ) {
                    calls++
                    if (calls == 1) throw first
                }
            val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            assertSame(first, failure)
            assertEquals("flake-observation-reporting-failed", failure.suppressed.single().message)
            assertFalse((failure.stackTraceToString() + output).contains("synthetic-private-reporter"))
            assertEquals(2, calls)
            println(event(output))
        }
    }

    @Test
    fun `reporter abort after success is blocking and after interruption preserves both failures`() {
        val successful =
            AdvisoryCheck(
                FlakePolicy.accepted(),
                ::identity,
                setOf("api_service"),
                onObservation = { throw TestAbortedException("synthetic-private-reporter") },
            ) {}
        val (reportFailure, successOutput) =
            capturePropertyOutput { assertThrows(IllegalStateException::class.java) { successful.run() } }
        assertEquals("flake-observation-reporting-failed", reportFailure.message)
        assertEquals(null, reportFailure.cause)
        assertFalse((reportFailure.stackTraceToString() + successOutput).contains("synthetic-private-reporter"))

        val first = AssertionError("synthetic-first-assertion")
        val interruption = IllegalStateException("synthetic-execution-interrupted")
        var calls = 0
        val interrupted =
            AdvisoryCheck(
                FlakePolicy.accepted(),
                ::identity,
                setOf("api_service"),
                onObservation = { throw TestAbortedException("synthetic-private-reporter") },
            ) {
                calls++
                if (calls == 1) throw first
                throw interruption
            }
        val (failure, output) =
            capturePropertyOutput { assertThrows(IllegalStateException::class.java) { interrupted.run() } }
        assertSame(interruption, failure)
        assertSame(first, failure.suppressed.first())
        assertEquals("flake-observation-reporting-failed", failure.suppressed.last().message)
        assertFalse((failure.stackTraceToString() + output).contains("synthetic-private-reporter"))
        assertTrue(event(output).contains("\"classification\":\"incomplete\""))
        println(event(output))
    }

    @Test
    fun `reporting failure cannot credit history before a later genuine flake`() {
        var engines = 0
        var reporterFails = true
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-reporting-history",
                1,
                {
                    engines++
                    fixture()
                },
                setOf("api_service"),
                onObservation = {
                    if (reporterFails) throw TestAbortedException("synthetic-private-reporter")
                    observations.add(it)
                },
            ) { case -> case.verify("synthetic-original-assertion", engines % 2 == 0) }
        val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertTrue(failure.message.orEmpty().contains("synthetic-original-assertion"))
        assertTrue(failure.suppressed.any { it.message == "flake-observation-reporting-failed" })
        val failedReport = event(output)
        println(failedReport)
        assertTrue(failedReport.contains("\"classification\":\"incomplete\""), "reporting-must-complete-before-credit")
        assertTrue(failedReport.contains("\"flakes_in_window\":0"))
        assertFalse((output + failure.stackTraceToString()).contains("synthetic-private-reporter"))
        reporterFails = false
        repeat(2) { index ->
            val (_, recovered) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            println(event(recovered))
            assertEquals(index + 1, observations.last().flakesInWindow)
            assertEquals(index == 1, observations.last().quarantineAdvisory)
        }
        assertEquals(6, engines)
    }

    @Test
    fun `changed samples between executions cannot accumulate comparable history`() {
        var input = 0
        var fail = true
        var engines = 0
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-history-input-binding",
                1,
                {
                    engines++
                    Arb.constant(FixtureCase("synthetic-same-label", input, ExpectedInvariant.HARNESS_STABILITY))
                },
                setOf("api_service"),
                onObservation = { observations.add(it) },
            ) { case ->
                val passed = !fail
                fail = false
                case.verify("synthetic-original-assertion", passed)
            }
        for (value in listOf(0, 1, 0)) {
            input = value
            fail = true
            val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            assertTrue(failure.message.orEmpty().contains("synthetic-original-assertion"))
            println(event(output))
        }
        assertEquals(FlakeClassification.NON_DETERMINISTIC, observations[0].classification)
        assertEquals(FlakeClassification.NOT_COMPARABLE, observations[1].classification)
        assertEquals(1, observations[1].attempts.size, "changed-history-input-is-not-retried")
        assertEquals(1, observations[1].flakesInWindow)
        assertFalse(observations[1].quarantineAdvisory)
        assertEquals(2, observations[2].flakesInWindow)
        assertTrue(observations[2].quarantineAdvisory)
        assertEquals(5, engines)
    }

    @Test
    fun `different actual samples from fresh factories never count as comparable flakes`() {
        var factories = 0
        val evaluated = mutableListOf<Int>()
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-changing-samples",
                1,
                {
                    val input = factories++ % 2
                    Arb.constant(FixtureCase("synthetic-same-label", input, ExpectedInvariant.HARNESS_STABILITY))
                },
                setOf("api_service"),
                onObservation = { observations.add(it) },
            ) { case ->
                evaluated.add(case.input)
                case.verify("deterministic-input-predicate", case.input == 1)
            }
        repeat(2) {
            val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            assertTrue(failure.message.orEmpty().contains("deterministic-input-predicate"), "original-failure-retained")
            println(event(output))
        }
        assertTrue(evaluated == listOf(0, 1, 0, 1), "actual-inputs-vary-with-identical-seed-and-label")
        assertEquals(4, factories)
        assertTrue(observations.all { it.classification == FlakeClassification.NOT_COMPARABLE }, "changed-inputs-not-flakes")
        assertTrue(observations.all { it.flakesInWindow == 0 && !it.quarantineAdvisory && it.blocked })
        assertTrue(
            observations.all {
                it.attempts.map { attempt ->
                    attempt.outcome
                } == listOf(AttemptOutcome.FAILED, AttemptOutcome.PASSED)
            },
        )
        if (System.getProperty("pennilogic.testing.flakeNegativeControl") == "true") {
            runner.run()
            error("changed-input-negative-control-did-not-fail")
        }
    }

    @Test
    fun `classification compares the whole primary prefix through the original failure`() {
        for (changePrefix in listOf(false, true)) {
            var factories = 0
            var evaluations = 0
            val observations = mutableListOf<FlakeObservation>()
            val runner =
                propertyCheck(
                    "synthetic-primary-prefix",
                    3,
                    {
                        factories++
                        sequenceFixture(if (changePrefix && factories == 2) listOf(2, 1, 3) else listOf(0, 1, 3))
                    },
                    setOf("api_service"),
                    onObservation = { observations.add(it) },
                ) { case ->
                    evaluations++
                    case.verify("synthetic-prefix-predicate", factories != 1 || case.input != 1)
                }
            val (_, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            assertEquals(2, factories)
            assertEquals(5, evaluations, "retry-reaches-failed-primary-and-completes-remaining-cases")
            val result = observations.single()
            assertEquals(
                if (changePrefix) FlakeClassification.NOT_COMPARABLE else FlakeClassification.NON_DETERMINISTIC,
                result.classification,
            )
            assertEquals(if (changePrefix) 0 else 1, result.flakesInWindow)
            assertEquals(!changePrefix, result.attempts.last().comparableTo(result.identity))
            println(event(output))
        }
    }

    @Test
    fun `shrinking cannot substitute a different passing input for the failed primary`() {
        var factories = 0
        val evaluated = mutableListOf<Int>()
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-shrunk-input",
                1,
                {
                    factories++
                    val input = if (factories == 1) 2 else 1
                    object : Arb<FixtureCase<Int>>() {
                        override fun edgecase(rs: RandomSource): Sample<FixtureCase<Int>>? = null

                        override fun sample(rs: RandomSource): Sample<FixtureCase<Int>> =
                            sampleOf(
                                FixtureCase("synthetic-same-label", input, ExpectedInvariant.HARNESS_STABILITY),
                                Shrinker { case ->
                                    if (case.input == 2) {
                                        listOf(FixtureCase("synthetic-same-label", 1, ExpectedInvariant.HARNESS_STABILITY))
                                    } else {
                                        emptyList()
                                    }
                                },
                            )
                    }
                },
                setOf("api_service"),
                onObservation = { observations.add(it) },
            ) { case ->
                evaluated.add(case.input)
                case.verify("deterministic-shrink-predicate", case.input == 1)
            }
        val (_, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertTrue(evaluated == listOf(2, 1, 1), "real-shrink-and-retry-executed")
        assertEquals(FlakeClassification.NOT_COMPARABLE, observations.single().classification)
        assertEquals(0, observations.single().flakesInWindow)
        println(event(output))
    }

    @Test
    fun `unverifiable mutable inputs cannot spoof comparison or authorize a retry`() {
        val input = UnverifiableInput()
        var factories = 0
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-unverifiable-input",
                1,
                {
                    factories++
                    Arb.constant(FixtureCase("synthetic-opaque-input", input, ExpectedInvariant.HARNESS_STABILITY))
                },
                setOf("api_service"),
                onObservation = { observations.add(it) },
            ) { case -> case.verify("synthetic-opaque-predicate", case.input.succeeds) }
        val (_, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertEquals(1, factories, "unverifiable-failed-input-is-not-reexecuted")
        val failed = observations.single()
        assertEquals(FlakeClassification.NOT_COMPARABLE, failed.classification)
        assertFalse(failed.attempts.single().comparableTo(failed.identity))
        assertEquals(0, failed.flakesInWindow)
        assertEquals(0, input.comparisons)
        println(event(output))

        input.succeeds = true
        runner.run()
        assertEquals(2, factories)
        assertEquals(FlakeClassification.STABLE_SUCCESS, observations.last().classification)
        assertEquals(0, observations.last().flakesInWindow)
        assertEquals(0, input.comparisons)
    }

    @Test
    fun `input comparison distinguishes scalar types and exact UTF16 code units`() {
        val changed = listOf<Pair<Any, Any>>(0 to 0L, '0' to "0", "\uD800" to "\uD801")
        for ((first, second) in changed) {
            var factories = 0
            val observations = mutableListOf<FlakeObservation>()
            val runner =
                propertyCheck(
                    "synthetic-typed-inputs",
                    1,
                    {
                        val input = if (factories++ == 0) first else second
                        Arb.constant(FixtureCase("synthetic-same-label", input, ExpectedInvariant.HARNESS_STABILITY))
                    },
                    setOf("api_service"),
                    onObservation = { observations.add(it) },
                ) { case -> case.verify("deterministic-typed-predicate", case.input == second) }
            val (_, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            assertEquals(2, factories)
            assertEquals(FlakeClassification.NOT_COMPARABLE, observations.single().classification)
            assertEquals(0, observations.single().flakesInWindow)
            println(event(output))
        }
    }

    @Test
    fun `immutable sample comparison is bounded private and preserves genuine same input flakes`() {
        val inputs =
            listOf(null, Unit, true, 1.toByte(), 1.toShort(), 1, 1L, 'x', "synthetic-private-sample", "x".repeat(4096), "x".repeat(4097))
        for (input in inputs) {
            var factories = 0
            val observations = mutableListOf<FlakeObservation>()
            val runner =
                propertyCheck(
                    "synthetic-immutable-input",
                    1,
                    {
                        factories++
                        Arb.constant(FixtureCase("synthetic-same-label", input, ExpectedInvariant.HARNESS_STABILITY))
                    },
                    setOf("api_service"),
                    onObservation = { observations.add(it) },
                ) { case -> case.verify("synthetic-varying-assertion", factories == 2) }
            val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
            val supported = input !is String || input.length <= 4096
            assertEquals(if (supported) 2 else 1, factories)
            val result = observations.single()
            assertEquals(
                if (supported) FlakeClassification.NON_DETERMINISTIC else FlakeClassification.NOT_COMPARABLE,
                result.classification,
            )
            assertEquals(if (supported) 1 else 0, result.flakesInWindow)
            assertFalse((failure.stackTraceToString() + output).contains("synthetic-private-sample"))
            assertFalse(output.contains("x".repeat(4096)))
            println(event(output))
        }
    }

    @Test
    fun `caused shrinking interruption in the first attempt is incomplete without retry`() {
        for (engineAssertionType in listOf(false, true)) {
            causedShrinking(setOf("api_service"), 1, engineAssertionType)
        }
    }

    @Test
    fun `caused shrinking interruption in the classification attempt retains both failures`() {
        for (engineAssertionType in listOf(false, true)) {
            causedShrinking(setOf("api_service"), 2, engineAssertionType)
        }
    }

    @Test
    fun `caused shrinking interruption remains incomplete for money and mixed classes`() {
        for (classes in listOf(setOf("money_path"), setOf("api_service", "money_path"))) {
            causedShrinking(classes, 1, false)
        }
    }

    @Test
    fun `caused shrinking value interruption is incomplete in either ordinary attempt`() {
        for (attempt in 1..2) {
            causedShrinking(setOf("api_service"), attempt, false, treeValue = true)
        }
    }

    @Test
    fun `completed shrinking still permits a genuine same input classification attempt`() {
        var factories = 0
        val evaluated = mutableListOf<Int>()
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-completed-shrinking",
                1,
                {
                    factories++
                    object : Arb<FixtureCase<Int>>() {
                        override fun edgecase(rs: RandomSource): Sample<FixtureCase<Int>>? = null

                        override fun sample(rs: RandomSource): Sample<FixtureCase<Int>> =
                            sampleOf(
                                FixtureCase("synthetic-same-input", 2, ExpectedInvariant.HARNESS_STABILITY),
                                Shrinker { case ->
                                    if (case.input == 2) {
                                        listOf(FixtureCase("synthetic-same-input", 1, ExpectedInvariant.HARNESS_STABILITY))
                                    } else {
                                        emptyList()
                                    }
                                },
                            )
                    }
                },
                setOf("api_service"),
                onObservation = { observations.add(it) },
            ) { case ->
                evaluated.add(case.input)
                case.verify("synthetic-varying-assertion", factories == 2 || case.input == 1)
            }
        val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertTrue(evaluated == listOf(2, 1, 2), "completed-shrink-preserves-original-primary-for-retry")
        assertTrue(failure.message.orEmpty().contains("synthetic-varying-assertion"))
        assertEquals(2, factories)
        assertEquals(FlakeClassification.NON_DETERMINISTIC, observations.single().classification)
        assertEquals(1, observations.single().flakesInWindow)
        println(event(output))
    }

    private fun causedShrinking(
        classes: Set<String>,
        interruptedAttempt: Int,
        engineAssertionType: Boolean,
        treeValue: Boolean = false,
    ) {
        var factories = 0
        var fail = true
        val callback = AssertionError("synthetic-original-callback")
        val earlier = AssertionError("synthetic-earlier-attempt")
        val shrinker =
            if (engineAssertionType) {
                AssertionFailedError("synthetic-private-shrinker").apply { initCause(callback) }
            } else {
                AssertionError("synthetic-private-shrinker").apply { initCause(callback) }
            }
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                "synthetic-caused-shrinking",
                1,
                {
                    factories++
                    object : Arb<FixtureCase<Unit>>() {
                        override fun edgecase(rs: RandomSource): Sample<FixtureCase<Unit>>? = null

                        override fun sample(rs: RandomSource): Sample<FixtureCase<Unit>> {
                            val case = FixtureCase("synthetic-same-input", Unit, ExpectedInvariant.HARNESS_STABILITY)
                            if (treeValue) {
                                return Sample(
                                    case,
                                    RTree(
                                        { case },
                                        lazy {
                                            listOf(
                                                RTree({
                                                    if (fail && factories == interruptedAttempt) throw shrinker
                                                    case
                                                }),
                                            )
                                        },
                                    ),
                                )
                            }
                            return sampleOf(
                                case,
                                Shrinker {
                                    if (fail && factories == interruptedAttempt) throw shrinker
                                    emptyList()
                                },
                            )
                        }
                    }
                },
                classes,
                onObservation = { observations.add(it) },
            ) {
                if (fail) {
                    if (factories == interruptedAttempt) throw callback
                    if (factories < interruptedAttempt) throw earlier
                }
            }
        if (System.getProperty("pennilogic.testing.flakeNegativeControl") == "true") {
            runner.run()
            error("caused-shrink-negative-control-did-not-fail")
        }
        val (failure, output) = capturePropertyOutput { assertThrows(IllegalStateException::class.java) { runner.run() } }
        assertEquals("property-execution-incomplete", failure.message)
        assertEquals(interruptedAttempt, factories, "interrupted-shrink-does-not-authorize-reexecution")
        assertTrue(failure.suppressed.any { it === callback }, "interrupted-callback-provenance")
        if (interruptedAttempt == 2) {
            assertTrue(
                failure.suppressed.any { suppressed ->
                    generateSequence(suppressed) { it.cause }.take(16).any { it === earlier }
                },
                "first-attempt-provenance",
            )
        }
        assertFalse((failure.stackTraceToString() + output).contains("synthetic-private-shrinker"))
        val observation = observations.single()
        assertEquals(FlakeClassification.INCOMPLETE, observation.classification)
        assertEquals(AttemptOutcome.INCOMPLETE, observation.attempts.last().outcome)
        assertEquals(interruptedAttempt, observation.attempts.size)
        assertEquals(0, observation.flakesInWindow)
        assertFalse(observation.quarantineAdvisory)
        println(event(output))

        fail = false
        runner.run()
        assertEquals(interruptedAttempt + 1, factories)
        assertEquals(FlakeClassification.STABLE_SUCCESS, observations.last().classification)
        assertEquals(0, observations.last().flakesInWindow)
    }

    private class UnverifiableInput {
        var succeeds = false
        var comparisons = 0

        override fun equals(other: Any?): Boolean {
            comparisons++
            return true
        }

        override fun hashCode(): Int = error("synthetic-untrusted-hash")

        override fun toString(): String = error("synthetic-untrusted-string")
    }

    private fun <T> sequenceFixture(inputs: List<T>): Arb<FixtureCase<T>> =
        object : Arb<FixtureCase<T>>() {
            private var position = 0

            override fun edgecase(rs: RandomSource): Sample<FixtureCase<T>>? = null

            override fun sample(rs: RandomSource): Sample<FixtureCase<T>> =
                sampleOf(
                    FixtureCase("synthetic-same-label", inputs[position++ % inputs.size], ExpectedInvariant.HARNESS_STABILITY),
                    Shrinker { emptyList() },
                )
        }

    private fun reporterAbort(
        classes: Set<String>,
        id: String,
    ) {
        var factories = 0
        var abortReporter = true
        val original = AssertionError("synthetic-original-callback")
        val observations = mutableListOf<FlakeObservation>()
        val runner =
            propertyCheck(
                id,
                1,
                {
                    factories++
                    fixture()
                },
                classes,
                onObservation = {
                    observations.add(it)
                    if (abortReporter) throw TestAbortedException("synthetic-private-reporter")
                },
            ) {
                if (factories == 1) throw original
            }
        if (System.getProperty("pennilogic.testing.flakeNegativeControl") == "true") {
            runner.run()
            error("reporter-negative-control-did-not-fail")
        }
        val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { runner.run() } }
        assertTrue(generateSequence<Throwable>(failure) { it.cause }.take(16).any { it === original }, "original-assertion-provenance")
        assertEquals("flake-observation-reporting-failed", failure.suppressed.single().message)
        assertFalse((failure.stackTraceToString() + output).contains("synthetic-private-reporter"))
        val expectedAttempts = if ("money_path" in classes) 1 else 2
        assertEquals(expectedAttempts, factories)
        assertEquals(expectedAttempts, observations.single().attempts.size)
        assertTrue(observations.single().blocked)
        println(event(output))

        abortReporter = false
        runner.run()
        assertEquals(expectedAttempts + 1, factories, "reporter-failure-releases-runner")
        assertEquals(FlakeClassification.STABLE_SUCCESS, observations.last().classification)
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
        }) { action() }

    private fun event(output: String): String {
        val line = output.lineSequence().single { it.startsWith("{\"event\":\"flaky_test_observation\"") }
        val json = Json.parseToJsonElement(line).jsonObject
        assertTrue(json.keys.none { it in setOf("input", "message", "record", "amount", "minor_units") }, "no-payload-fields")
        assertTrue(line.length <= 2048 && line.all { it.code in 32..126 }, "bounded-single-line-ASCII")
        return line
    }
}
