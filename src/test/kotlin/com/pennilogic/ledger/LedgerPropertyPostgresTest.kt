package com.pennilogic.ledger

import com.pennilogic.contracts.money.Money
import com.pennilogic.testing.ExpectedInvariant
import com.pennilogic.testing.FixtureCase
import com.pennilogic.testing.LedgerInput
import com.pennilogic.testing.PropertyExecution
import com.pennilogic.testing.SharedFixtures
import com.pennilogic.testing.SharedGenerators
import com.pennilogic.testing.capturePropertyFailure
import com.pennilogic.testing.checkProperty
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.parallel.Isolated
import org.junit.jupiter.api.parallel.ResourceLock
import org.junit.jupiter.api.parallel.Resources
import java.sql.Connection

@Tag("postgres")
@Tag("property")
@Isolated("Captures the property library's process streams")
@ResourceLock(Resources.SYSTEM_OUT)
@ResourceLock(Resources.SYSTEM_ERR)
class LedgerPropertyPostgresTest {
    @Test
    fun `shared generated pairs balance in the actual accepted PostgreSQL ledger`() =
        checked("ledger-property-balanced") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                checkProperty("ledger-balanced", 48, SharedGenerators.ledger()) { case ->
                    trial(connection, case) {
                        insert(case, balanced = true)
                        sql("SET CONSTRAINTS ALL IMMEDIATE")
                        val total = balance(scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries"), case)
                        case.verify("database-zero-sum", total == case.input.value + -case.input.value)
                        val first =
                            balance(
                                scalar(
                                    "SELECT sum(amount_minor)::text FROM pennilogic.entries WHERE account_id = ?",
                                    case.input.first.id,
                                ),
                                case,
                            )
                        case.verify("database-account-balance", first == case.input.value)
                    }
                }
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "property-trials-rolled-back")
            }
        }

    @Test
    fun `an out of range database aggregate cannot enter property failure diagnostics`() =
        checked("ledger-property-aggregate-control") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                var initialAggregate: String? = null
                var payloadObserved = false
                val captured =
                    capturePropertyFailure { record ->
                        checkProperty("ledger-aggregate-negative", 1, SharedGenerators.ledger(), onResult = record) { case ->
                            trial(connection, case) {
                                insert(case, balanced = false)
                                val raw = requireNotNull(scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries"))
                                if (initialAggregate == null) initialAggregate = raw
                                val total: Money =
                                    try {
                                        balance(raw, case)
                                    } catch (error: NumberFormatException) {
                                        payloadObserved = payloadObserved || error.message.orEmpty().contains(raw)
                                        throw AssertionError("case=${case.id} operation=unsafe-database-conversion")
                                    }
                                case.verify("database-zero-sum", total == case.input.value + -case.input.value)
                            }
                        }
                    }
                captured.report()
                assertTrue(initialAggregate != null && initialAggregate.toLongOrNull() == null, "database-aggregate-outside-range")
                assertTrue(!payloadObserved, "database-conversion-no-payload")
                val rendered = captured.output + captured.failure.stackTraceToString()
                assertTrue(!rendered.contains(requireNotNull(initialAggregate)), "database-aggregate-diagnostic-projection")
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "aggregate-control-rolled-back")
            }
        }

    @Test
    fun `shared generated imbalances are rejected by the deferred zero sum constraint`() =
        checked("ledger-property-unbalanced") {
            val fixture = LedgerFixture()
            fixture.connect().use { verifyUnbalanced(it, "ledger-unbalanced") }
        }

    @Test
    fun `labeled zero and identifier seeds are exercised rather than discarded`() =
        checked("ledger-property-adversarial") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                checkProperty("ledger-adversarial", 16, SharedGenerators.ledgerAdversarial()) { case ->
                    trial(connection, case) {
                        val constraint =
                            when (case.expectedInvariant) {
                                ExpectedInvariant.ACCOUNT_UUID -> "accounts_uuid_v7"
                                ExpectedInvariant.TRANSACTION_UUID -> "transactions_uuid_v7"
                                ExpectedInvariant.ZERO_ENTRY -> "entry_nonzero_or_range"
                                else -> error("ledger-adversarial-invariant")
                            }
                        rejected(case.id, "23514", constraint) {
                            insert(case, balanced = true)
                            sql("SET CONSTRAINTS ALL IMMEDIATE")
                        }
                    }
                }
            }
        }

    @Test
    fun `an isolated broken constraint makes the real property fail shrink and recover`() =
        checked("ledger-property-negative-control") {
            val fixture = LedgerFixture()
            fixture.connect().use { admin ->
                val original =
                    requireNotNull(admin.scalar("SELECT pg_get_functiondef('ledger.assert_transaction_balanced()'::regprocedure)"))
                try {
                    admin.sql(
                        """CREATE OR REPLACE FUNCTION ledger.assert_transaction_balanced() RETURNS trigger
                            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS
                            'BEGIN RETURN NULL; END;'""",
                    )
                    if (System.getProperty("pennilogic.testing.ledgerNegativeControl") == "true") {
                        fixture.connect().use { verifyUnbalanced(it, "ledger-planted-negative") }
                        error("ledger-negative-control-did-not-fail")
                    }
                    val captured =
                        capturePropertyFailure { record ->
                            fixture.connect().use { verifyUnbalanced(it, "ledger-planted-negative", record) }
                        }
                    val rendered = captured.output + captured.failure.stackTraceToString()
                    assertTrue(
                        captured.failure.message
                            .orEmpty()
                            .contains("ledger-inr-maximum.s0-must-refuse"),
                        "negative-specific-invariant",
                    )
                    assertTrue(
                        captured.failure.message
                            .orEmpty()
                            .contains("seed ${SharedFixtures.corpus.seed}"),
                        "negative-replay-seed",
                    )
                    assertTrue(captured.output.contains("Shrink result"), "negative-real-shrinking")
                    assertTrue(!rendered.contains(SharedFixtures.corpus.acceptedValue("inr-maximum").toString()), "negative-no-value")
                    assertTrue(
                        !rendered.contains(
                            SharedFixtures.corpus.accounts
                                .first()
                                .input.id
                                .toString(),
                        ),
                        "negative-no-account",
                    )
                    captured.report()
                } finally {
                    admin.sql(original)
                    same(
                        original,
                        admin.scalar("SELECT pg_get_functiondef('ledger.assert_transaction_balanced()'::regprocedure)"),
                        "negative-function-restored",
                    )
                    same("0", admin.scalar("SELECT count(*) FROM pennilogic.entries"), "negative-no-retained-entries")
                    same("0", admin.scalar("SELECT count(*) FROM pennilogic.accounts"), "negative-no-retained-accounts")
                }
                fixture.connect().use { verifyUnbalanced(it, "ledger-restored-rejection") }
                admin.autoCommit = false
                val owner = ledgerId()
                val first = admin.account(owner)
                val second = admin.account(owner, "EXPENSE")
                val transaction = admin.transaction(owner)
                admin.pair(transaction, owner, first, second, SharedFixtures.corpus.acceptedValue("inr-maximum"))
                admin.commit()
                same("2", admin.scalar("SELECT count(*) FROM pennilogic.entries"), "restored-genuine-commit")
                same("0", admin.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries"), "restored-committed-zero-sum")
            }
        }

    private fun verifyUnbalanced(
        connection: Connection,
        id: String,
        onResult: (PropertyExecution) -> Unit = {},
    ) {
        checkProperty(id, 32, SharedGenerators.ledger(), onResult = onResult) { case ->
            trial(connection, case) {
                insert(case, balanced = false)
                rejected(case.id, "23514", "transaction_zero_sum") { sql("SET CONSTRAINTS ALL IMMEDIATE") }
            }
        }
    }

    private fun balance(
        raw: String?,
        case: FixtureCase<LedgerInput>,
    ): Money {
        val units = raw?.toLongOrNull()
        case.verify("database-balance-range", units != null)
        return Money.ofMinorUnits(requireNotNull(units), "INR")
    }

    private fun trial(
        connection: Connection,
        case: FixtureCase<LedgerInput>,
        action: Connection.() -> Unit,
    ) = checked(case.id) {
        connection.autoCommit = false
        try {
            connection.action()
        } finally {
            connection.rollback()
            connection.autoCommit = true
        }
    }

    private fun Connection.insert(
        case: FixtureCase<LedgerInput>,
        balanced: Boolean,
    ) {
        val input = case.input
        case.verify("inr-only", input.value.currency == "INR")
        val first = account(input.owner, input.first.type, id = input.first.id)
        val second = account(input.owner, input.second.type, id = input.second.id)
        val transaction =
            transaction(
                input.owner,
                id = input.transaction.id,
                occurred = input.transaction.occurredAt,
                valueDate = input.transaction.valueDate,
            )
        val other = if (balanced) -input.value else input.value
        rawEntry(transaction, input.owner, first, input.value.minorUnits, input.value.currency)
        rawEntry(transaction, input.owner, second, other.minorUnits, other.currency)
    }
}
