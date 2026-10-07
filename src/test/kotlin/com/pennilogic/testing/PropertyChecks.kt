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
import java.io.ByteArrayOutputStream
import java.io.PrintStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

@OptIn(ExperimentalKotest::class)
internal fun <T> checkProperty(
    id: String,
    cases: Int,
    generator: Arb<FixtureCase<T>>,
    onResult: (PropertyExecution) -> Unit = {},
    check: (FixtureCase<T>) -> Unit,
) {
    require(Regex("[a-z][a-z0-9-]{0,63}").matches(id) && cases in 1..512) { "property-configuration" }
    val seed = SharedFixtures.corpus.seed
    val started = System.nanoTime()
    var primary = 0
    var passed = 0
    var evaluations = 0
    var completed = false
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
                    try {
                        check(case)
                    } catch (error: MoneyWireException) {
                        val field =
                            when (error.field) {
                                "" -> "root"
                                "amount" -> "amount"
                                "currency" -> "currency"
                                else -> "unrecognized"
                            }
                        // The accepted provider can reflect arbitrary extra keys; never retain its cause.
                        throw AssertionError("case=${case.id} operation=$id reason=${error.reason.wireName} field=$field")
                    } catch (_: SerializationException) {
                        throw AssertionError("case=${case.id} operation=$id failure=json-decoding")
                    }
                }
            }
        assertTrue(context.evals() == cases && context.successes() == cases && context.failures() == 0, "property=$id exact-execution")
        completed = true
    } finally {
        val elapsed = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started)
        val execution = PropertyExecution(id, seed, primary, passed, evaluations, families.toMap(), elapsed, completed)
        onResult(execution)
        println(execution.json())
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

// Call only from isolated tests holding JUnit's SYSTEM_OUT and SYSTEM_ERR resource locks.
internal fun capturePropertyFailure(action: ((PropertyExecution) -> Unit) -> Unit): CapturedPropertyFailure {
    val originalOut = System.out
    val originalErr = System.err
    val bytes = ByteArrayOutputStream()
    val executions = mutableListOf<PropertyExecution>()
    PrintStream(bytes, true, Charsets.UTF_8).use { stream ->
        try {
            System.setOut(stream)
            System.setErr(stream)
            val failure = assertThrows(AssertionError::class.java) { action { executions.add(it) } }
            return CapturedPropertyFailure(failure, bytes.toString(Charsets.UTF_8), executions.toList())
        } finally {
            System.setOut(originalOut)
            System.setErr(originalErr)
        }
    }
}
