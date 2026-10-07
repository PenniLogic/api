package com.pennilogic.testing

import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneySerializer
import com.pennilogic.contracts.money.MoneyWireException
import io.kotest.property.RandomSource
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.parallel.Isolated
import org.junit.jupiter.api.parallel.ResourceLock
import org.junit.jupiter.api.parallel.Resources
import org.opentest4j.AssertionFailedError

@Tag("property")
@Isolated("Captures the property library's process streams")
@ResourceLock(Resources.SYSTEM_OUT)
@ResourceLock(Resources.SYSTEM_ERR)
class SharedPropertyHarnessTest {
    @Test
    fun `same seed recreates actual values accounts transactions and case identifiers`() {
        val first =
            SharedGenerators
                .ledger()
                .samples(RandomSource.seeded(SharedFixtures.corpus.seed))
                .take(40)
                .toList()
        val second =
            SharedGenerators
                .ledger()
                .samples(RandomSource.seeded(SharedFixtures.corpus.seed))
                .take(40)
                .toList()
        for ((left, right) in first.zip(second)) {
            val a = left.value
            val b = right.value
            a.verify("replay-id", a.id == b.id)
            a.verify("replay-value", a.input.value == b.input.value)
            a.verify("replay-owner", a.input.owner == b.input.owner)
            a.verify("replay-accounts", a.input.first.id == b.input.first.id && a.input.second.id == b.input.second.id)
            a.verify("replay-account-types", a.input.first.type == b.input.first.type && a.input.second.type == b.input.second.type)
            a.verify("replay-transaction", a.input.transaction.id == b.input.transaction.id)
            a.verify("replay-dates", a.input.transaction.occurredAt == b.input.transaction.occurredAt)
            a.verify("replay-value-date", a.input.transaction.valueDate == b.input.transaction.valueDate)
        }
        val changed =
            SharedGenerators
                .ledger()
                .samples(RandomSource.seeded(SharedFixtures.corpus.seed + 1))
                .take(40)
                .toList()
        assertTrue(first.zip(changed).any { (a, b) -> a.value.input.value != b.value.input.value }, "different-seed-changes-values")
        val dates = first.map { it.value.input.transaction.occurredAt }.toSet()
        assertTrue(
            SharedFixtures.corpus.transactions
                .filter { it.expectedInvariant == ExpectedInvariant.TRANSACTION_ADMITTED }
                .all { it.input.occurredAt in dates },
            "all-date-seeds-executed",
        )
    }

    @Test
    fun `the real engine shrinks and reports only replay identifiers through JUnit failure diagnostics`() {
        val first = failWithActualEngine()
        val second = failWithActualEngine()
        assertTrue(first.failure.message == second.failure.message, "property-failure-reproducible")
        assertTrue(first.output.contains("Shrink result"), "actual-library-shrink-output")
        assertTrue(
            first.failure.message
                .orEmpty()
                .contains("seed ${SharedFixtures.corpus.seed}"),
            "actual-library-seed-output",
        )
        assertTrue(
            first.failure.message
                .orEmpty()
                .contains("ledger-inr-maximum.s0"),
            "actual-library-minimized-case",
        )
        assertTrue(first.failure is AssertionFailedError, "actual-junit-diff-type")
        val failure = first.failure
        require(failure is AssertionFailedError) { "actual-junit-diff-type" }
        assertTrue(failure.expected.value == true && failure.actual.value == false, "junit-diffs-contain-booleans-only")
        val rendered = first.output + second.output + failure.stackTraceToString() + second.failure.stackTraceToString()
        val forbidden =
            listOf(
                SharedFixtures.corpus.acceptedValue("inr-maximum").toString(),
                SharedFixtures.corpus.acceptedValue("inr-unit").toString(),
                SharedFixtures.corpus.accounts
                    .first()
                    .input.id
                    .toString(),
                "\"amount\"",
                "minorUnits=",
                "AccountInput(",
                "TransactionInput(",
            )
        assertTrue(forbidden.none { rendered.contains(it) }, "engine-and-assertion-diagnostics-contain-no-records")
        first.report()
        second.report()
        checkProperty("harness-clean-recovery", 16, SharedGenerators.ledger()) { case ->
            case.verify("exact-negation", case.input.value + -case.input.value == Money.ofMinorUnits(0, "INR"))
        }
    }

    @Test
    fun `payload bearing codec failures are projected before actual property reporting`() {
        val value = SharedFixtures.corpus.acceptedValue("inr-maximum")
        val encoded = Json.encodeToString(MoneySerializer, value)
        val document = Json.parseToJsonElement(encoded).jsonObject
        val digits = document.getValue("amount").jsonPrimitive.content
        val account =
            SharedFixtures.corpus.accounts
                .first()
                .input.id
                .toString()
        val extra = brokenWire(value, account, extraKey = true)
        val malformed = brokenWire(value, account, extraKey = false)
        val extraFailure = rawWireFailure<MoneyWireException>(extra)
        assertTrue(
            listOf(account, digits, "\u001b", "\n").all {
                extraFailure.field.contains(it) && extraFailure.message.orEmpty().contains(it)
            },
            "raw-extra-key-path-confirmed",
        )
        val syntaxFailure = rawWireFailure<SerializationException>(malformed)
        assertTrue(syntaxFailure.javaClass.simpleName == "JsonDecodingException", "raw-json-decoder-path-confirmed")
        assertTrue(
            syntaxFailure.message.orEmpty().contains(digits) && syntaxFailure.message.orEmpty().contains(account),
            "raw-json-payload-path-confirmed",
        )
        for (operation in listOf("wire-extra-key-control", "wire-json-syntax-control")) {
            val captured =
                capturePropertyFailure { record ->
                    checkProperty(operation, 8, SharedGenerators.values(), onResult = record) { case ->
                        val input = case.input
                        require(input is ValueInput.Accepted) { "wire-control-valid-input" }
                        Json.decodeFromString(
                            MoneySerializer,
                            brokenWire(input.value, account, extraKey = operation == "wire-extra-key-control"),
                        )
                    }
                }
            val rendered = captured.output + captured.failure.stackTraceToString()
            assertTrue(listOf(digits, account, "\u001b", "JSON input:").none { rendered.contains(it) }, "wire-property-payload-projection")
            assertTrue(
                rendered.contains("operation=$operation") && rendered.contains("seed ${SharedFixtures.corpus.seed}"),
                "wire-replay-attribution",
            )
            val category = if (operation == "wire-extra-key-control") "reason=shape field=unrecognized" else "failure=json-decoding"
            assertTrue(rendered.contains(category), "wire-safe-failure-category")
            assertTrue(
                generateSequence(captured.failure.cause) { it.cause }.none {
                    MoneyWireException::class.java.isInstance(it) || SerializationException::class.java.isInstance(it)
                },
                "wire-no-payload-bearing-cause",
            )
            captured.report()
        }
        assertTrue(value == Json.decodeFromString(MoneySerializer, encoded), "wire-diagnostic-clean-recovery")
        println("""{"event":"property_diagnostic_controls","raw_paths_confirmed":2,"projected_paths_checked":2,"clean_recovery":true}""")
    }

    private fun brokenWire(
        value: Money,
        account: String,
        extraKey: Boolean,
    ): String {
        val document = Json.parseToJsonElement(Json.encodeToString(MoneySerializer, value)).jsonObject
        if (extraKey) {
            val digits = document.getValue("amount").jsonPrimitive.content
            val key = "synthetic-$account-$digits\u001b[31m\n"
            return JsonObject(document + (key to JsonPrimitive("synthetic"))).toString()
        }
        return JsonObject(document + ("account" to JsonPrimitive(account))).toString().dropLast(1)
    }

    private inline fun <reified T : Exception> rawWireFailure(wire: String): T {
        val failure: Exception =
            try {
                Json.decodeFromString(MoneySerializer, wire)
                throw AssertionError("raw-wire-control-must-fail")
            } catch (error: MoneyWireException) {
                error
            } catch (error: SerializationException) {
                error
            }
        require(failure is T) { "raw-wire-control-failure-type" }
        return failure
    }

    private fun failWithActualEngine(): CapturedPropertyFailure {
        var finalInput: Money? = null
        val captured =
            capturePropertyFailure { record ->
                checkProperty("harness-negative-control", 8, SharedGenerators.ledger(), onResult = record) { case ->
                    finalInput = case.input.value
                    case.verify("deliberate-invariant-violation", false)
                }
            }
        assertTrue(finalInput == SharedFixtures.corpus.acceptedValue("inr-unit"), "actual-shrink-minimum")
        return captured
    }
}
