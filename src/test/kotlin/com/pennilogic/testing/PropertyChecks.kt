package com.pennilogic.testing

import com.pennilogic.contracts.money.MoneyWireException
import io.kotest.common.ExperimentalKotest
import io.kotest.property.Arb
import io.kotest.property.EdgeConfig
import io.kotest.property.PropTestConfig
import io.kotest.property.PropTestListener
import io.kotest.property.PropertyContext
import io.kotest.property.ShrinkingMode
import io.kotest.property.checkAll
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.opentest4j.TestAbortedException
import java.io.ByteArrayOutputStream
import java.io.PrintStream
import java.security.MessageDigest
import java.time.Clock
import java.util.concurrent.TimeUnit

internal fun <T> checkProperty(
    id: String,
    cases: Int,
    generator: Arb<FixtureCase<T>>,
    onResult: (PropertyExecution) -> Unit = {},
    check: (FixtureCase<T>) -> Unit,
) {
    propertyCheck(id, cases, { generator }, setOf("money_path"), onResult = onResult, check = check).run()
}

internal fun <T> propertyCheck(
    id: String,
    cases: Int,
    generator: () -> Arb<FixtureCase<T>>,
    changeClasses: Set<String>,
    requestedRetries: Int? = null,
    clock: Clock = Clock.systemUTC(),
    onResult: (PropertyExecution) -> Unit = {},
    onObservation: (FlakeObservation) -> Unit = {},
    check: (FixtureCase<T>) -> Unit,
): AdvisoryCheck {
    require(Regex("[a-z][a-z0-9-]{0,63}").matches(id) && cases in 1..512) { "property-configuration" }
    val policy = FlakePolicy.accepted()
    val classes = changeClasses.toSet()
    val seed = SharedFixtures.corpus.seed
    val configuration =
        buildJsonObject {
            put("property", id)
            put("cases", cases)
            put("seed", seed)
            put("fixtures", flakeDigest(SharedFixtures.resource().toByteArray(Charsets.UTF_8)))
            put("policy", policy.digest)
            put("retries", policy.retries(classes, requestedRetries))
            put("change_classes", classes.sorted().joinToString(","))
        }.toString()
    return AdvisoryCheck(
        policy,
        {
            FlakeIdentity(
                id,
                seed,
                TestCategory.PROPERTY,
                requireNotNull(System.getProperty("pennilogic.testing.task")) { "flake-task-missing" },
                requireNotNull(System.getProperty("pennilogic.testing.source")) { "flake-source-missing" },
                flakeDigest(configuration.toByteArray(Charsets.UTF_8)),
            )
        },
        classes,
        requestedRetries,
        clock,
        onObservation,
    ) {
        val arbitrary =
            try {
                generator()
            } catch (_: AssertionError) {
                throw IllegalStateException("property-execution-incomplete")
            }
        checkPropertyAttempt(id, cases, arbitrary, onResult, check)
    }
}

@OptIn(ExperimentalKotest::class)
private fun <T> checkPropertyAttempt(
    id: String,
    cases: Int,
    generator: Arb<FixtureCase<T>>,
    onResult: (PropertyExecution) -> Unit,
    check: (FixtureCase<T>) -> Unit,
) {
    require(Regex("[a-z][a-z0-9-]{0,63}").matches(id) && cases in 1..512) { "property-configuration" }
    val seed = SharedFixtures.corpus.seed
    val started = System.nanoTime()
    var primary = 0
    var passed = 0
    var evaluations = 0
    var completed = false
    var interrupted = false
    val callbackAssertions = mutableListOf<AssertionError>()
    var primaryPending = false
    val families = mutableMapOf<ExpectedInvariant, Int>()
    val listener =
        object : PropTestListener {
            override suspend fun beforeTest() {
                primary++
                primaryPending = true
            }

            override suspend fun afterTest() {
                passed++
            }
        }
    try {
        val context: PropertyContext =
            runBlocking {
                checkAll(
                    PropTestConfig(
                        seed = seed,
                        iterations = cases,
                        minSuccess = cases,
                        maxFailure = 0,
                        maxDiscardPercentage = 0,
                        shrinkingMode = ShrinkingMode.Bounded(32),
                        edgeConfig = EdgeConfig(0.0),
                        listeners = listOf(listener),
                    ),
                    generator,
                ) { case ->
                    evaluations++
                    if (primaryPending) {
                        families.merge(case.expectedInvariant, 1, Int::plus)
                        primaryPending = false
                    }
                    var outcomeKnown = false
                    try {
                        check(case)
                        outcomeKnown = true
                    } catch (error: AssertionError) {
                        outcomeKnown = true
                        callbackAssertions.add(error)
                        throw error
                    } catch (error: MoneyWireException) {
                        outcomeKnown = true
                        val field =
                            when (error.field) {
                                "" -> "root"
                                "amount" -> "amount"
                                "currency" -> "currency"
                                else -> "unrecognized"
                            }
                        // The accepted provider can reflect arbitrary extra keys; never retain its cause.
                        val failure = AssertionError("case=${case.id} operation=$id reason=${error.reason.wireName} field=$field")
                        callbackAssertions.add(failure)
                        throw failure
                    } catch (_: SerializationException) {
                        outcomeKnown = true
                        val failure = AssertionError("case=${case.id} operation=$id failure=json-decoding")
                        callbackAssertions.add(failure)
                        throw failure
                    } catch (_: TestAbortedException) {
                        throw IllegalStateException("property-execution-incomplete")
                    } finally {
                        if (!outcomeKnown) interrupted = true
                    }
                }
            }
        if (interrupted) throw IllegalStateException("property-execution-incomplete")
        assertTrue(context.evals() == cases && context.successes() == cases && context.failures() == 0, "property=$id exact-execution")
        completed = true
    } catch (error: AssertionError) {
        // Kotest wraps non-assertion callback exceptions in AssertionFailedError too.
        val callbackFailure =
            generateSequence<Throwable>(error) { it.cause }.take(16).any { cause ->
                callbackAssertions.any { it === cause }
            }
        if (interrupted || !callbackFailure) throw IllegalStateException("property-execution-incomplete")
        throw error
    } catch (_: TestAbortedException) {
        throw IllegalStateException("property-execution-incomplete")
    } finally {
        val elapsed = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started)
        val execution = PropertyExecution(id, seed, primary, passed, evaluations, families.toMap(), elapsed, completed)
        try {
            onResult(execution)
        } catch (_: AssertionError) {
            throw IllegalStateException("property-execution-incomplete")
        } finally {
            println(execution.json())
        }
    }
}

internal class PropertyExecution(
    private val id: String,
    private val seed: Long,
    private val primary: Int,
    private val passed: Int,
    private val evaluations: Int,
    private val families: Map<ExpectedInvariant, Int>,
    private val elapsed: Long,
    private val completed: Boolean,
) {
    fun json(): String =
        buildJsonObject {
            put("event", "shared_property_cases")
            put("property", id)
            put("seed", seed)
            put("primary_cases", primary)
            put("passed", passed)
            put("evaluations", evaluations)
            put("shrink_evaluations", evaluations - primary)
            putJsonObject("families") { families.forEach { (family, count) -> put(family.id, count) } }
            put("elapsed_ms", elapsed)
            put("status", if (completed) "passed" else "failed")
        }.toString()
}

internal class CapturedPropertyFailure(
    val failure: AssertionError,
    val output: String,
    private val executions: List<PropertyExecution>,
) {
    fun report() {
        require(executions.size == 1) { "property-control-execution-count" }
        println(executions.single().json())
        val outputHash = digest(output)
        val failureHash = digest(failure.stackTraceToString())
        println("""{"event":"property_failure_control","output_sha256":"$outputHash","failure_sha256":"$failureHash"}""")
    }

    private fun digest(text: String): String = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8)).toHexString()
}

internal fun capturePropertyFailure(action: ((PropertyExecution) -> Unit) -> Unit): CapturedPropertyFailure {
    val executions = mutableListOf<PropertyExecution>()
    val (failure, output) = capturePropertyOutput { assertThrows(AssertionError::class.java) { action { executions.add(it) } } }
    return CapturedPropertyFailure(failure, output, executions.toList())
}

// Call only from isolated tests holding JUnit's SYSTEM_OUT and SYSTEM_ERR resource locks.
internal fun <T> capturePropertyOutput(action: () -> T): Pair<T, String> {
    val originalOut = System.out
    val originalErr = System.err
    val bytes = ByteArrayOutputStream()
    PrintStream(bytes, true, Charsets.UTF_8).use { stream ->
        try {
            System.setOut(stream)
            System.setErr(stream)
            val result = action()
            return result to bytes.toString(Charsets.UTF_8)
        } finally {
            System.setOut(originalOut)
            System.setErr(originalErr)
        }
    }
}
