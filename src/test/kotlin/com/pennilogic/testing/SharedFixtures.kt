package com.pennilogic.testing

import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneyReason
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertTrue
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException
import java.util.UUID

internal enum class ExpectedInvariant(
    val id: String,
) {
    ROUND_TRIP("money-round-trip"),
    OUT_OF_RANGE("money-out-of-range"),
    UNKNOWN_CURRENCY("money-currency-unknown"),
    ACCOUNT_ADMITTED("account-admitted"),
    ACCOUNT_UUID("account-uuid"),
    TRANSACTION_ADMITTED("transaction-admitted"),
    TRANSACTION_UUID("transaction-uuid"),
    ZERO_SUM("ledger-zero-sum"),
    ZERO_ENTRY("ledger-zero-entry"),
    HARNESS_STABILITY("harness-stability"),
}

// Not a data class: property printers must never traverse the input's records.
internal class FixtureCase<out T>(
    val id: String,
    val input: T,
    val expectedInvariant: ExpectedInvariant,
) {
    init {
        require(Regex("[a-z][a-z0-9.-]{0,191}").matches(id)) { "fixture-case-identifier" }
    }

    fun verify(
        operation: String,
        condition: Boolean,
    ) {
        require(Regex("[a-z][a-z0-9-]{0,63}").matches(operation)) { "fixture-operation-identifier" }
        assertTrue(condition, "case=$id operation=$operation")
    }

    override fun toString(): String = "case=$id"
}

internal sealed interface ValueInput {
    class Accepted(
        val value: Money,
    ) : ValueInput

    class Refused(
        val units: Long,
        val currency: String,
        val reason: MoneyReason,
    ) : ValueInput
}

internal class AccountInput(
    val id: UUID,
    val type: String,
)

internal class TransactionInput(
    val id: UUID,
    val occurredAt: OffsetDateTime,
    val valueDate: LocalDate?,
)

internal class FixtureCorpus(
    val seed: Long,
    val values: List<FixtureCase<ValueInput>>,
    val accounts: List<FixtureCase<AccountInput>>,
    val transactions: List<FixtureCase<TransactionInput>>,
) {
    fun acceptedValue(id: String): Money {
        val input = values.single { it.id == id }.input
        require(input is ValueInput.Accepted) { "fixture-value-not-accepted" }
        return input.value
    }
}

internal object SharedFixtures {
    const val SCHEMA = "pennilogic.shared-test-fixtures/1"
    val corpus: FixtureCorpus by lazy { parse(resource()) }

    fun resource(): String =
        requireNotNull(javaClass.getResourceAsStream("/testing/shared-fixtures.v1.json")) { "shared-fixture-resource-missing" }
            .bufferedReader(Charsets.UTF_8)
            .use { it.readText() }

    fun parse(text: String): FixtureCorpus {
        val document =
            try {
                Json.parseToJsonElement(text)
            } catch (_: SerializationException) {
                throw IllegalArgumentException("shared-fixture-json")
            }
        val root = fields(document, setOf("schema", "seed", "values", "accounts", "transactions"))
        require(string(root.getValue("schema")) == SCHEMA) { "shared-fixture-version" }
        val seed = integer(root.getValue("seed"))
        val values =
            rows(root.getValue("values")) { input, invariant ->
                val fields = fields(input, setOf("minor_units", "currency"))
                val digits = string(fields.getValue("minor_units"))
                require(Regex("0|-?[1-9][0-9]*").matches(digits)) { "shared-fixture-integer" }
                val units = digits.toLongOrNull() ?: error("shared-fixture-integer")
                val currency = string(fields.getValue("currency"))
                when (invariant) {
                    ExpectedInvariant.ROUND_TRIP -> {
                        try {
                            ValueInput.Accepted(Money.ofMinorUnits(units, currency))
                        } catch (_: MoneyWireException) {
                            throw IllegalArgumentException("shared-fixture-validity-label")
                        }
                    }

                    ExpectedInvariant.OUT_OF_RANGE -> {
                        ValueInput.Refused(units, currency, MoneyReason.OUT_OF_RANGE)
                    }

                    ExpectedInvariant.UNKNOWN_CURRENCY -> {
                        ValueInput.Refused(units, currency, MoneyReason.CURRENCY_UNKNOWN)
                    }

                    else -> {
                        error("shared-fixture-value-invariant")
                    }
                }
            }
        val accounts =
            rows(root.getValue("accounts")) { input, invariant ->
                require(invariant in setOf(ExpectedInvariant.ACCOUNT_ADMITTED, ExpectedInvariant.ACCOUNT_UUID)) {
                    "shared-fixture-account-invariant"
                }
                val fields = fields(input, setOf("id", "type"))
                AccountInput(identifier(fields.getValue("id")), string(fields.getValue("type")))
            }
        val transactions =
            rows(root.getValue("transactions")) { input, invariant ->
                require(invariant in setOf(ExpectedInvariant.TRANSACTION_ADMITTED, ExpectedInvariant.TRANSACTION_UUID)) {
                    "shared-fixture-transaction-invariant"
                }
                val fields = fields(input, setOf("id", "occurred_at", "value_date"))
                try {
                    val occurred = OffsetDateTime.parse(string(fields.getValue("occurred_at")))
                    require(occurred.nano % 1_000_000 == 0) { "shared-fixture-millisecond-precision" }
                    val date = fields.getValue("value_date").let { if (it == JsonNull) null else LocalDate.parse(string(it)) }
                    TransactionInput(identifier(fields.getValue("id")), occurred, date)
                } catch (_: DateTimeParseException) {
                    throw IllegalArgumentException("shared-fixture-date")
                }
            }
        val ids = values.map { it.id } + accounts.map { it.id } + transactions.map { it.id }
        require(ids.toSet().size == ids.size) { "shared-fixture-duplicate-case" }
        return FixtureCorpus(seed, values, accounts, transactions)
    }

    private fun <T> rows(
        value: JsonElement,
        input: (JsonElement, ExpectedInvariant) -> T,
    ): List<FixtureCase<T>> {
        require(value is JsonArray && value.isNotEmpty()) { "shared-fixture-cases" }
        return value.map { row ->
            val fields = fields(row, setOf("id", "input", "expected_invariant", "tolerance"))
            require(integer(fields.getValue("tolerance")) == 0L) { "shared-fixture-exact-tolerance" }
            val name = string(fields.getValue("expected_invariant"))
            val invariant = ExpectedInvariant.entries.singleOrNull { it.id == name } ?: error("shared-fixture-invariant")
            FixtureCase(string(fields.getValue("id")), input(fields.getValue("input"), invariant), invariant)
        }
    }

    private fun fields(
        value: JsonElement,
        expected: Set<String>,
    ): JsonObject {
        require(value is JsonObject && value.keys == expected) { "shared-fixture-fields" }
        return value
    }

    private fun string(value: JsonElement): String {
        require(value is JsonPrimitive && value.isString) { "shared-fixture-string" }
        return value.content
    }

    private fun integer(value: JsonElement): Long {
        require(value is JsonPrimitive && !value.isString && Regex("-?(0|[1-9][0-9]*)").matches(value.content)) {
            "shared-fixture-integer"
        }
        return value.content.toLongOrNull() ?: error("shared-fixture-integer")
    }

    private fun identifier(value: JsonElement): UUID =
        try {
            UUID.fromString(string(value))
        } catch (_: IllegalArgumentException) {
            throw IllegalArgumentException("shared-fixture-uuid")
        }
}
