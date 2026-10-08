package com.pennilogic.testing

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test

@Tag("contract")
class SharedFixtureContractTest {
    @Test
    fun `the exact fixture contract consumes accepted Money and ledger domains without public models`() {
        val corpus = SharedFixtures.corpus
        val ledger =
            Json
                .parseToJsonElement(
                    requireNotNull(javaClass.getResourceAsStream("/ledger/adr-017-parameters.json"))
                        .bufferedReader(Charsets.UTF_8)
                        .use { it.readText() },
                ).jsonObject
        val types =
            ledger
                .getValue("account_types")
                .jsonArray
                .map { it.jsonPrimitive.content }
                .toSet()
        for (case in corpus.values) {
            when (val input = case.input) {
                is ValueInput.Accepted -> {
                    case.verify("registered-currency", input.value.currency in CurrencyRegistry.entries)
                    case.verify("accepted-range", input.value.minorUnits != Long.MIN_VALUE)
                }

                is ValueInput.Refused -> {
                    val error = assertThrows(MoneyWireException::class.java) { Money.ofMinorUnits(input.units, input.currency) }
                    case.verify("labeled-refusal", error.reason == input.reason)
                }
            }
        }
        for (case in corpus.accounts) {
            val valid = case.input.id.version() == 7 && case.input.id.variant() == 2
            case.verify("identifier-label", valid == (case.expectedInvariant == ExpectedInvariant.ACCOUNT_ADMITTED))
            case.verify("accepted-account-type", case.input.type in types)
        }
        for (case in corpus.transactions) {
            val valid = case.input.id.version() == 7 && case.input.id.variant() == 2
            case.verify("identifier-label", valid == (case.expectedInvariant == ExpectedInvariant.TRANSACTION_ADMITTED))
            case.verify("millisecond-precision", case.input.occurredAt.nano % 1_000_000 == 0)
        }
        assertTrue(corpus.accounts.count { it.expectedInvariant == ExpectedInvariant.ACCOUNT_ADMITTED } >= 2, "fixture-account-pair")
    }

    @Test
    fun `fixture schema refuses unknown versions fields tolerance and duplicate case identifiers`() {
        val original = Json.parseToJsonElement(SharedFixtures.resource()).jsonObject
        val rows = original.getValue("values").jsonArray
        val first = rows.first().jsonObject
        val noncanonicalInput = JsonObject(first.getValue("input").jsonObject + ("minor_units" to JsonPrimitive("01")))
        val invalid =
            listOf(
                JsonObject(original + ("schema" to JsonPrimitive("pennilogic.shared-test-fixtures/2"))),
                JsonObject(original + ("extra" to JsonPrimitive(true))),
                JsonObject(original + ("values" to JsonArray(listOf(JsonObject(first + ("tolerance" to JsonPrimitive(1))))))),
                JsonObject(original + ("values" to JsonArray(listOf(JsonObject(first + ("tolerance" to JsonPrimitive("0"))))))),
                JsonObject(original + ("values" to JsonArray(listOf(first, first)))),
                JsonObject(original + ("values" to JsonArray(listOf(JsonObject(first - "expected_invariant"))))),
                JsonObject(original + ("values" to JsonArray(listOf(JsonObject(first + ("input" to noncanonicalInput)))))),
            )
        for (document in invalid) {
            val failure = assertThrows(IllegalArgumentException::class.java) { SharedFixtures.parse(document.toString()) }
            assertTrue(failure.message.orEmpty().startsWith("shared-fixture-"), "fixture-contract-refusal")
            assertTrue(!failure.stackTraceToString().contains("9223372036854775807"), "fixture-contract-diagnostics")
        }
    }
}
