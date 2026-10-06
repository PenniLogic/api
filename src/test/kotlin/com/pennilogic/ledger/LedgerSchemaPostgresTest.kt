package com.pennilogic.ledger

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import com.pennilogic.migration.Json
import com.pennilogic.migration.MigrationCli
import com.pennilogic.migration.MigrationFailed
import com.pennilogic.migration.MigrationSet
import com.pennilogic.migration.Output
import com.pennilogic.migration.Registry
import com.pennilogic.migration.RegistryProblem
import com.pennilogic.migration.RowState
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Files
import java.nio.file.Path
import java.sql.Connection
import java.time.LocalDate
import kotlin.random.Random

@Tag("postgres")
class LedgerSchemaPostgresTest {
    @TempDir
    lateinit var directory: Path

    @Test
    fun `raw SQL balance is deferred until commit and rollback restores the database`() =
        checked("raw-zero-sum") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner, "EXPENSE")
                connection.autoCommit = false
                val invalid = connection.transaction(owner)
                connection.rawEntry(invalid, owner, first, 2)
                connection.rawEntry(invalid, owner, second, -1)
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "unbalanced-rows-written")
                rejected("unbalanced-commit", "23514", "transaction_zero_sum") { connection.commit() }
                connection.rollback()
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.transactions"), "unbalanced-transaction-rolled-back")
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "unbalanced-entries-rolled-back")
                val valid = connection.transaction(owner)
                connection.pair(valid, owner, first, second)
                connection.commit()
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "balanced-restoration")
                val abandoned = connection.transaction(owner)
                connection.pair(abandoned, owner, first, second)
                connection.rollback()
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "explicit-rollback-preserved")
            }
        }

    @Test
    fun `immediate constraints expose imbalance sooner and cannot disable it`() =
        checked("early-constraint") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                connection.autoCommit = false
                val transaction = connection.transaction(owner)
                connection.rawEntry(transaction, owner, first, 2)
                connection.rawEntry(transaction, owner, second, -1)
                rejected("immediate-zero-sum", "23514", "transaction_zero_sum") { connection.sql("SET CONSTRAINTS ALL IMMEDIATE") }
                connection.rollback()
                val restored = connection.transaction(owner)
                connection.pair(restored, owner, first, second)
                connection.sql("SET CONSTRAINTS ALL IMMEDIATE")
                connection.commit()
            }
        }

    @Test
    fun `candidates may be empty but posted empty and incomplete transactions cannot commit`() =
        checked("candidate-lifecycle") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                val candidate = connection.transaction(owner, status = "candidate", count = 0, booked = null)
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "empty-candidate")
                rejected("candidate-booking", "23514", "candidate_entry_count") {
                    connection.transaction(owner, status = "candidate", count = 0)
                }
                rejected("candidate-count", "23514", "candidate_entry_count") {
                    connection.transaction(owner, status = "candidate", count = 2)
                }
                rejected("posted-null-booking", "23514", "candidate_entry_count") {
                    connection.transaction(owner, booked = null)
                }
                connection.autoCommit = false
                connection.transaction(owner)
                rejected("empty-posted", "23514", "transaction_sealed") { connection.commit() }
                connection.rollback()
                val incomplete = connection.transaction(owner)
                connection.rawEntry(incomplete, owner, first, 1)
                rejected("single-entry", "23514", "transaction_sealed") { connection.commit() }
                connection.rollback()
                connection.sql(
                    "UPDATE pennilogic.transactions SET status = 'posted', entry_count = 2, booked_at = ? WHERE id = ?",
                    ledgerNow(),
                    candidate,
                )
                connection.pair(candidate, owner, first, second)
                connection.commit()
                same("posted", connection.scalar("SELECT status FROM pennilogic.transactions WHERE id = ?", candidate), "candidate-posted")
                connection.autoCommit = true
                rejected("second-count-write", "23514", "transaction_sealed") {
                    connection.sql("UPDATE pennilogic.transactions SET entry_count = entry_count WHERE id = ?", candidate)
                }
                rejected("second-booking-write", "23514", "transaction_sealed") {
                    connection.sql("UPDATE pennilogic.transactions SET booked_at = booked_at WHERE id = ?", candidate)
                }
                connection.autoCommit = false
                connection.pair(candidate, owner, first, second)
                rejected("late-balanced-append", "23514", "transaction_sealed") { connection.commit() }
                connection.rollback()
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "sealed-history-unchanged")
            }
        }

    @Test
    fun `money bounds nullability orphan and tenant currency foreign keys reject raw bypasses`() =
        checked("entry-domains") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val other = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                val foreign = connection.account(other)
                rejected("currency-not-admitted", "23503") { connection.account(owner, currency = "JPY") }
                connection.sql(
                    "INSERT INTO pennilogic.ledger_currencies VALUES ('JPY', ?, ?)",
                    CurrencyRegistry.exponentOf("JPY"),
                    ledgerNow(),
                )
                val otherCurrency = connection.account(owner, currency = "JPY")
                connection.autoCommit = false
                for ((index, units) in listOf(null, 0L, Long.MIN_VALUE).withIndex()) {
                    val transaction = connection.transaction(owner)
                    rejected("amount-bound-$index", if (units == null) "23502" else "23514") {
                        connection.rawEntry(transaction, owner, first, units)
                    }
                    connection.rollback()
                }
                val mixed = connection.transaction(owner)
                rejected("mixed-transaction-currency", "23503", "transaction_single_currency") {
                    connection.rawEntry(mixed, owner, otherCurrency, 1, "JPY")
                }
                connection.rollback()
                val mismatch = connection.transaction(owner)
                rejected("account-currency", "23503", "entry_account_currency") {
                    connection.rawEntry(mismatch, owner, otherCurrency, 1)
                }
                connection.rollback()
                val foreignAccount = connection.transaction(owner)
                rejected("account-owner", "23503", "entry_account_owner") {
                    connection.rawEntry(foreignAccount, owner, foreign, 1)
                }
                connection.rollback()
                val foreignTransaction = connection.transaction(owner)
                rejected("transaction-owner", "23503", "entry_owner") {
                    connection.rawEntry(foreignTransaction, other, foreign, 1)
                }
                connection.rollback()
                rejected("missing-transaction", "23514", "transaction_sealed") {
                    connection.rawEntry(ledgerId(), owner, first, 1)
                }
                connection.rollback()
                val orphan = connection.transaction(owner)
                rejected("missing-account", "23503") { connection.rawEntry(orphan, owner, ledgerId(), 1) }
                connection.rollback()
                val valid = connection.transaction(owner, count = 4)
                val maximum = Money.ofMinorUnits(Money.MAX_MINOR_UNITS, "INR")
                val minimum = -maximum
                connection.rawEntry(valid, owner, first, maximum.minorUnits)
                connection.rawEntry(valid, owner, first, maximum.minorUnits)
                connection.rawEntry(valid, owner, second, minimum.minorUnits)
                connection.rawEntry(valid, owner, second, minimum.minorUnits)
                connection.commit()
                same("0", connection.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries"), "aggregate-cannot-overflow")
            }
        }

    @Test
    fun `one bulk insert balances each transaction independently`() =
        checked("bulk-entries") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                connection.autoCommit = false
                val one = connection.transaction(owner)
                val two = connection.transaction(owner)
                connection.sql(
                    """INSERT INTO pennilogic.entries (id, transaction_id, owner_id, account_id, amount_minor, currency)
                        VALUES (?, ?, ?, ?, ?, 'INR'), (?, ?, ?, ?, ?, 'INR'),
                               (?, ?, ?, ?, ?, 'INR'), (?, ?, ?, ?, ?, 'INR')""",
                    ledgerId(),
                    one,
                    owner,
                    first,
                    1L,
                    ledgerId(),
                    two,
                    owner,
                    second,
                    -1L,
                    ledgerId(),
                    one,
                    owner,
                    second,
                    -1L,
                    ledgerId(),
                    two,
                    owner,
                    first,
                    1L,
                )
                connection.commit()
                same("4", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "bulk-committed")
                val invalidOne = connection.transaction(owner)
                val invalidTwo = connection.transaction(owner)
                connection.rawEntry(invalidOne, owner, first, 1)
                connection.rawEntry(invalidOne, owner, second, 1)
                connection.rawEntry(invalidTwo, owner, first, -1)
                connection.rawEntry(invalidTwo, owner, second, -1)
                rejected("global-zero-is-not-per-transaction-zero", "23514", "transaction_zero_sum") { connection.commit() }
                connection.rollback()
                same("4", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "bulk-negative-restored")
            }
        }

    @Test
    fun `append only guards protect historical rows even on the synthetic migration connection`() =
        checked("append-only") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                connection.autoCommit = false
                val transaction = connection.transaction(owner)
                connection.pair(transaction, owner, first, second)
                connection.commit()
                connection.autoCommit = true
                for ((index, statement) in listOf(
                    "UPDATE pennilogic.entries SET amount_minor = amount_minor",
                    "DELETE FROM pennilogic.entries",
                    "TRUNCATE pennilogic.entries",
                    "DELETE FROM pennilogic.transactions",
                    "UPDATE pennilogic.transactions SET occurred_at = occurred_at + INTERVAL '1 day'",
                    "UPDATE pennilogic.accounts SET currency = 'JPY'",
                    "UPDATE pennilogic.accounts SET type = 'EQUITY'",
                    "UPDATE pennilogic.ledger_currencies SET exponent = 0",
                    "DELETE FROM pennilogic.ledger_currencies",
                ).withIndex()) {
                    rejected("immutable-$index", "23514") { connection.sql(statement) }
                }
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "immutable-rows-retained")
                same("0", connection.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries"), "immutable-derived-balance")
            }
        }

    @Test
    fun `unavailable descriptive and linked providers cannot store arbitrary bytes or forged provenance`() =
        checked("unavailable-providers") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val account = connection.account(owner)
                for (column in listOf("name", "institution", "mask", "mask_bidx")) {
                    rejected("account-reservation-$column", "23514", "accounts_crypto_unavailable") {
                        connection.sql("UPDATE pennilogic.accounts SET $column = ? WHERE id = ?", byteArrayOf(1), account)
                    }
                }
                val candidate = connection.transaction(owner, status = "candidate", count = 0, booked = null)
                for (column in listOf(
                    "description",
                    "merchant_display",
                    "merchant_bidx",
                    "merchant_raw",
                    "merchant_amount_minor",
                    "note",
                    "external_ref",
                    "external_ref_bidx",
                )) {
                    rejected("transaction-reservation-$column", "23514", "transactions_crypto_unavailable") {
                        connection.sql("UPDATE pennilogic.transactions SET $column = ? WHERE id = ?", byteArrayOf(1), candidate)
                    }
                    rejected("merchant-currency-without-provider", "23514", "transactions_crypto_unavailable") {
                        connection.sql("UPDATE pennilogic.transactions SET merchant_currency = 'JPY' WHERE id = ?", candidate)
                    }
                }
                rejected("forged-administrative-reference", "23514", "reason_provider_unavailable") {
                    connection.transaction(owner, reason = "ADMINISTRATIVE", reasonRef = ledgerId())
                }
                rejected("required-reason-reference", "23514", "reason_ref_shape") {
                    connection.transaction(owner, reason = "ADMINISTRATIVE")
                }
            }
        }

    @Test
    fun `reserved accounts exchange groups and snapshots retain accepted domains without enabling FX`() =
        checked("reserved-shapes") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                for (type in listOf("ASSET", "LIABILITY", "INCOME", "EXPENSE", "EQUITY")) connection.account(owner, type)
                rejected("unknown-account-type", "23514") { connection.account(owner, "UNKNOWN") }
                rejected("clearing-role-required", "23514", "accounts_system_role_type") { connection.account(owner, "CLEARING") }
                rejected("clearing-unavailable", "23514", "exchange_not_admitted") {
                    connection.account(owner, "CLEARING", systemRole = "FX_CLEARING")
                }
                rejected("fee-unavailable", "23514", "exchange_not_admitted") {
                    connection.account(owner, "EXPENSE", systemRole = "FX_FEE")
                }
                connection.account(owner, "EQUITY", systemRole = "OPENING_BALANCE")
                rejected("unique-system-account", "23505", "accounts_system_role_currency") {
                    connection.account(owner, "EQUITY", systemRole = "OPENING_BALANCE")
                }
                for (quote in listOf("INR", "JPY")) {
                    rejected("exchange-reserved-$quote", if (quote == "INR") "23514" else "23503") {
                        connection.sql(
                            """INSERT INTO pennilogic.exchange_groups
                                (id, owner_id, base_currency, quote_currency, quoted_rate_e10, rate_source,
                                 rate_instant, rounding_side, occurred_at, booked_at)
                                VALUES (?, ?, 'INR', ?, ?, 'USER_ENTERED', ?, 'QUOTE', ?, ?)""",
                            ledgerId(),
                            owner,
                            quote,
                            1L,
                            ledgerNow(),
                            ledgerNow(),
                            ledgerNow(),
                        )
                    }
                }
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.exchange_groups"), "exchange-still-empty")
                val account = connection.account(owner, "LIABILITY")
                val snapshot = ledgerId()
                connection.sql(
                    """INSERT INTO pennilogic.statement_snapshots
                        (id, owner_id, account_id, currency, statement_date, statement_balance_minor, source, captured_at, booked_at)
                        VALUES (?, ?, ?, 'INR', ?, ?, 'MANUAL', ?, ?)""",
                    snapshot,
                    owner,
                    account,
                    LocalDate.of(2026, 1, 1),
                    0L,
                    ledgerNow(),
                    ledgerNow(),
                )
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "snapshot-not-a-balance-write")
                rejected("snapshot-no-update", "23514", "append_only") {
                    connection.sql("UPDATE pennilogic.statement_snapshots SET source = 'IMPORT' WHERE id = ?", snapshot)
                }
                rejected("snapshot-no-delete", "23514", "append_only") {
                    connection.sql("DELETE FROM pennilogic.statement_snapshots WHERE id = ?", snapshot)
                }
            }
        }

    @Test
    fun `snapshot money and reconciliation links retain bounds ownership and uniqueness`() =
        checked("snapshot-and-reconciliation") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val account = connection.account(owner, "LIABILITY")
                val equity = connection.account(owner, "EQUITY", systemRole = "RECONCILIATION")
                val date = LocalDate.of(2026, 1, 1)
                val insert =
                    """INSERT INTO pennilogic.statement_snapshots
                        (id, owner_id, account_id, currency, statement_date, statement_balance_minor, source, captured_at, booked_at)
                        VALUES (?, ?, ?, 'INR', ?, ?, 'MANUAL', ?, ?)"""
                for ((index, value) in listOf(null, Long.MIN_VALUE).withIndex()) {
                    rejected("snapshot-bound-$index", if (value == null) "23502" else "23514") {
                        connection.sql(insert, ledgerId(), owner, account, date, value, ledgerNow(), ledgerNow())
                    }
                }
                rejected("snapshot-clock", "23514", "booked_at_plausible") {
                    connection.sql(insert, ledgerId(), owner, account, date, 0L, ledgerNow(), ledgerNow().minusMinutes(6))
                }
                rejected("snapshot-other-owner", "23503") {
                    connection.sql(insert, ledgerId(), ledgerId(), account, date, 0L, ledgerNow(), ledgerNow())
                }
                val snapshot = ledgerId()
                connection.sql(insert, snapshot, owner, account, date, Money.MAX_MINOR_UNITS, ledgerNow(), ledgerNow())
                connection.autoCommit = false
                val adjustment =
                    connection.transaction(
                        owner,
                        kind = "RECONCILIATION_ADJUSTMENT",
                        valueDate = date,
                        reason = "RECONCILIATION",
                        snapshot = snapshot,
                    )
                connection.pair(adjustment, owner, account, equity, Money.ofMinorUnits(Money.MAX_MINOR_UNITS, "INR"))
                connection.commit()
                rejected("snapshot-already-linked", "23505") {
                    connection.transaction(
                        owner,
                        kind = "RECONCILIATION_ADJUSTMENT",
                        valueDate = date,
                        reason = "RECONCILIATION",
                        snapshot = snapshot,
                    )
                }
                connection.rollback()
                rejected("missing-snapshot-provider-row", "23503") {
                    connection.transaction(
                        owner,
                        kind = "RECONCILIATION_ADJUSTMENT",
                        valueDate = date,
                        reason = "RECONCILIATION",
                        snapshot = ledgerId(),
                    )
                }
                connection.rollback()
                same("1", connection.scalar("SELECT count(*) FROM pennilogic.statement_snapshots"), "snapshot-retained")
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "adjustment-links-retained")
            }
        }

    @Test
    fun `posting count required tenant and identifier domains reject raw invalid rows`() =
        checked("required-row-domains") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                rejected("entry-count-one", "23514") { connection.transaction(owner, count = 1) }
                rejected("entry-count-negative", "23514") { connection.transaction(owner, count = -1) }
                rejected("entry-count-smallint-overflow", "22003") { connection.transaction(owner, count = 32768) }
                rejected("tenant-required", "23502") {
                    connection.sql(
                        "INSERT INTO pennilogic.accounts (id, owner_id, type, currency, created_at)" +
                            " VALUES (?, NULL, 'ASSET', 'INR', ?)",
                        ledgerId(),
                        ledgerNow(),
                    )
                }
                rejected("uuid-v7-required", "23514", "accounts_uuid_v7") {
                    connection.sql(
                        "INSERT INTO pennilogic.accounts (id, owner_id, type, currency, created_at)" +
                            " VALUES ('00000000-0000-4000-8000-000000000000', ?, 'ASSET', 'INR', ?)",
                        owner,
                        ledgerNow(),
                    )
                }
                rejected("finite-time-required", "23514") {
                    connection.sql(
                        "INSERT INTO pennilogic.accounts (id, owner_id, type, currency, created_at)" +
                            " VALUES (?, ?, 'ASSET', 'INR', 'infinity')",
                        ledgerId(),
                        owner,
                    )
                }
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.accounts"), "invalid-domains-no-rows")
            }
        }

    @Test
    fun `a clearing entry on an ordinary transaction fails even in a widened synthetic currency fixture`() =
        checked("clearing-membership") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                connection.sql(
                    "INSERT INTO pennilogic.ledger_currencies VALUES ('JPY', ?, ?)",
                    CurrencyRegistry.exponentOf("JPY"),
                    ledgerNow(),
                )
                val first = connection.account(owner)
                val clearing = connection.account(owner, "CLEARING", systemRole = "FX_CLEARING")
                connection.autoCommit = false
                val transaction = connection.transaction(owner)
                connection.pair(transaction, owner, first, clearing)
                rejected("ordinary-clearing-entry", "23514", "clearing_entry_membership") { connection.commit() }
                connection.rollback()
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "clearing-negative-preserved")
            }
        }

    @Test
    fun `posting clock validation rejects both directions and a stale candidate posting`() =
        checked("posting-clock") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                for ((index, booked) in listOf(ledgerNow().minusMinutes(6), ledgerNow().plusMinutes(6)).withIndex()) {
                    rejected("clock-insert-$index", "23514", "booked_at_plausible") { connection.transaction(owner, booked = booked) }
                }
                val candidate = connection.transaction(owner, status = "candidate", count = 0, booked = null)
                rejected("clock-posting", "23514", "booked_at_plausible") {
                    connection.sql(
                        "UPDATE pennilogic.transactions SET status = 'posted', entry_count = 2, booked_at = ? WHERE id = ?",
                        ledgerNow().minusMinutes(6),
                        candidate,
                    )
                }
                same("candidate", connection.scalar("SELECT status FROM pennilogic.transactions WHERE id = ?", candidate), "clock-restored")
            }
        }

    @Test
    fun `seeded properties recompute per account balances with the actual accepted Money primitive`() =
        checked("seeded-balance-property") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owners = List(16) { ledgerId() }
                val accounts = owners.associateWith { owner -> List(5) { connection.account(owner) } }
                val expected =
                    accounts.values
                        .flatten()
                        .associateWith { Money.ofMinorUnits(0, "INR") }
                        .toMutableMap()
                val random = Random(72131)
                repeat(1000) { index ->
                    connection.autoCommit = false
                    val owner = owners[random.nextInt(owners.size)]
                    val owned = accounts.getValue(owner)
                    val transaction = connection.transaction(owner, count = 4)
                    repeat(2) {
                        val first = owned[random.nextInt(owned.size)]
                        val second = owned[random.nextInt(owned.size)]
                        val value = Money.ofMinorUnits(random.nextLong(1, 100000), "INR")
                        connection.pair(transaction, owner, first, second, value)
                        expected[first] = expected.getValue(first) + value
                        expected[second] = expected.getValue(second) - value
                    }
                    connection.commit()
                    same(
                        "0",
                        connection.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries WHERE transaction_id = ?", transaction),
                        "property-transaction-$index",
                    )
                }
                for ((ownerIndex, owner) in owners.withIndex()) {
                    for ((accountIndex, account) in accounts.getValue(owner).withIndex()) {
                        val derived =
                            Money.ofMinorUnits(
                                requireNotNull(
                                    connection.scalar(
                                        "SELECT coalesce(sum(amount_minor), 0)::text FROM pennilogic.entries" +
                                            " WHERE owner_id = ? AND account_id = ?",
                                        owner,
                                        account,
                                    ),
                                ).toLong(),
                                "INR",
                            )
                        same(expected.getValue(account), derived, "property-account-$ownerIndex-$accountIndex")
                    }
                }
                same("4000", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "property-count")
                connection.sql("ANALYZE pennilogic.entries")
                connection.sql("ANALYZE pennilogic.transactions")
                val owner = owners.first()
                val queries =
                    listOf(
                        Triple(
                            "entries_owner_account",
                            "SELECT sum(amount_minor) FROM pennilogic.entries WHERE owner_id = ? AND account_id = ?",
                            arrayOf<Any>(owner, accounts.getValue(owner).first()),
                        ),
                        Triple(
                            "transactions_owner_occurred",
                            "SELECT id FROM pennilogic.transactions WHERE owner_id = ? AND occurred_at >= ?" +
                                " ORDER BY occurred_at DESC, id DESC LIMIT 20",
                            arrayOf<Any>(owner, ledgerNow().minusHours(1)),
                        ),
                        Triple(
                            "transactions_owner_booked",
                            "SELECT id FROM pennilogic.transactions WHERE owner_id = ? AND booked_at <= ?" +
                                " ORDER BY booked_at DESC, id DESC LIMIT 1",
                            arrayOf<Any>(owner, ledgerNow()),
                        ),
                    )
                for ((index, query, values) in queries) {
                    val plan = requireNotNull(connection.scalar("EXPLAIN (FORMAT JSON, COSTS OFF) $query", *values))
                    val selected =
                        Regex("\"Index Name\"\\s*:\\s*\"([a-z0-9_]+)\"")
                            .findAll(plan)
                            .map { it.groupValues[1] }
                            .toSet()
                    assertTrue(index in selected, "planner-uses-$index;selected=$selected")
                    println("""{"event":"ledger_index_plan","index":"$index","selected":true}""")
                }
                connection.commit()
                println("""{"event":"ledger_property_cases","property":"derived_balances","count":1000,"entries":4000}""")
            }
        }

    @Test
    fun `real restricted login proves privileges and default deny without an invented runtime policy`() =
        checked("restricted-permissions") {
            val fixture = LedgerFixture()
            fixture.connect().use { admin ->
                val owner = ledgerId()
                val first = admin.account(owner)
                val second = admin.account(owner)
                admin.autoCommit = false
                val transaction = admin.transaction(owner)
                admin.pair(transaction, owner, first, second)
                admin.commit()
                admin.autoCommit = true
                fixture.restricted { restricted, _ ->
                    for (table in listOf("accounts", "transactions", "entries", "statement_snapshots", "exchange_groups")) {
                        same("0", restricted.scalar("SELECT count(*) FROM pennilogic.$table"), "default-deny-$table")
                    }
                    same("1", restricted.scalar("SELECT count(*) FROM pennilogic.ledger_currencies"), "reference-readable")
                    for ((index, statement) in listOf(
                        "UPDATE pennilogic.entries SET amount_minor = amount_minor",
                        "DELETE FROM pennilogic.entries",
                        "TRUNCATE pennilogic.entries",
                        "UPDATE pennilogic.statement_snapshots SET source = 'MANUAL'",
                        "DELETE FROM pennilogic.statement_snapshots",
                        "UPDATE pennilogic.exchange_groups SET rate_source = 'USER_ENTERED'",
                        "DELETE FROM pennilogic.exchange_groups",
                        "UPDATE pennilogic.transactions SET currency = 'JPY'",
                        "UPDATE pennilogic.accounts SET currency = 'JPY'",
                        "INSERT INTO pennilogic.ledger_currencies VALUES ('JPY', 0, CURRENT_TIMESTAMP)",
                        "UPDATE pennilogic.ledger_currencies SET exponent = 0",
                        "DELETE FROM pennilogic.ledger_currencies",
                        "ALTER TABLE pennilogic.entries DISABLE TRIGGER ALL",
                        "SET session_replication_role = replica",
                    ).withIndex()) {
                        rejected("restricted-operation-$index", "42501") { restricted.sql(statement) }
                    }
                    rejected("default-deny-insert", "42501") { restricted.account(owner) }
                    restricted.scalar("SELECT set_config('app.principal_id', ?, false)", owner.toString())
                    same("0", restricted.scalar("SELECT count(*) FROM pennilogic.entries"), "forged-context-no-policy")
                }
                same("2", admin.scalar("SELECT count(*) FROM pennilogic.entries"), "privilege-negative-preserved")
                same(
                    "0",
                    admin.scalar(
                        "SELECT count(*) FROM pg_policy WHERE polrelid IN" +
                            " (SELECT oid FROM pg_class WHERE relnamespace = 'pennilogic'::regnamespace)",
                    ),
                    "no-admitting-policies",
                )
            }
        }

    @Test
    fun `an insert only negative fixture policy cannot vacuously commit an invisible candidate`() =
        checked("invisible-transaction") {
            val fixture = LedgerFixture()
            val owner = ledgerId()
            fixture.restricted { restricted, role ->
                fixture.connect().use { admin ->
                    admin.sql(
                        "CREATE POLICY ledger_fixture_insert_only ON pennilogic.transactions FOR INSERT TO $role" +
                            " WITH CHECK (owner_id = '$owner'::uuid)",
                    )
                    try {
                        restricted.autoCommit = false
                        restricted.transaction(owner, status = "candidate", count = 0, booked = null)
                        rejected("invisible-candidate-commit", "23514", "transaction_sealed") { restricted.commit() }
                        restricted.rollback()
                        same("0", admin.scalar("SELECT count(*) FROM pennilogic.transactions"), "invisible-transaction-rolled-back")
                    } finally {
                        restricted.rollback()
                        admin.sql("DROP POLICY ledger_fixture_insert_only ON pennilogic.transactions")
                    }
                }
            }
            fixture.connect().use { same("0", it.scalar("SELECT count(*) FROM pg_policy"), "negative-policy-removed") }
        }

    @Test
    fun `catalog retains named query indexes bigint constraints invoker triggers and forced tenant RLS`() =
        checked("catalog-contract") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                same(
                    "5",
                    connection.scalar(
                        "SELECT count(*) FROM pg_class WHERE relnamespace = 'pennilogic'::regnamespace" +
                            " AND relrowsecurity AND relforcerowsecurity",
                    ),
                    "forced-tenant-table-count",
                )
                same(
                    "false",
                    connection.scalar(
                        "SELECT relrowsecurity::text FROM pg_class" +
                            " WHERE oid = 'pennilogic.ledger_currencies'::regclass",
                    ),
                    "reference-exemption",
                )
                same(
                    "bigint:NO",
                    connection.scalar(
                        "SELECT data_type || ':' || is_nullable FROM information_schema.columns" +
                            " WHERE table_schema = 'pennilogic' AND table_name = 'entries' AND column_name = 'amount_minor'",
                    ),
                    "nonnull-integer-money",
                )
                same(
                    "0",
                    connection.scalar(
                        "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'pennilogic'" +
                            " AND column_name LIKE '%balance%'" +
                            " AND NOT (table_name = 'statement_snapshots' AND column_name = 'statement_balance_minor')",
                    ),
                    "no-mutable-balance",
                )
                same("170011", connection.scalar("SHOW server_version_num"), "actual-postgres-version")
                println("""{"event":"ledger_postgres_identity","serverVersion":"17.11","fixtureRole":"synthetic-bootstrap-superuser"}""")
                same(
                    "0",
                    connection.scalar("SELECT count(*) FROM pg_proc WHERE pronamespace = 'ledger'::regnamespace AND prosecdef"),
                    "no-definer",
                )
                same(
                    "4",
                    connection.scalar(
                        "SELECT count(*) FROM pg_trigger" +
                            " WHERE tgname IN ('transaction_zero_sum', 'transaction_sealed', 'exchange_group_shape')" +
                            " AND NOT tgisinternal AND tgdeferrable AND tginitdeferred AND tgenabled = 'O'",
                    ),
                    "deferred-constraints-installed",
                )
                for ((name, fragment) in mapOf(
                    "entries_owner_account" to "(owner_id, account_id, transaction_id, id)",
                    "entries_transaction_currency" to "(transaction_id, currency)",
                    "transactions_owner_occurred" to "(owner_id, occurred_at DESC, id DESC)",
                    "transactions_owner_booked" to "(owner_id, booked_at, id)",
                )) {
                    val definition =
                        requireNotNull(
                            connection.scalar(
                                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'pennilogic' AND indexname = ?",
                                name,
                            ),
                        )
                    assertTrue(definition.contains(fragment), "named-index-$name")
                }
                assertFalse(fixture.events.any { it.contains("password", ignoreCase = true) }, "migration-events-redacted")
            }
        }

    @Test
    fun `populated reversal refuses under the real runner and preserves registry lock and exact row bytes`() =
        checked("history-preservation") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                connection.autoCommit = false
                val transaction = connection.transaction(owner)
                connection.pair(transaction, owner, first, second)
                connection.commit()
                connection.autoCommit = true
                val before = fingerprint(connection)
                val currenciesBefore =
                    connection.scalar("SELECT json_agg(c ORDER BY code)::text FROM pennilogic.ledger_currencies c")
                val runner = fixture.runner(connection)
                val beforeStatus = runner.status()
                val priorRows = Registry(connection).rows()
                same(listOf(1, 2), priorRows.map { it.version }, "prior-shipped-versions")
                same(listOf(RowState.APPLIED, RowState.APPLIED), priorRows.map { it.state }, "prior-applied-transitions")
                val priorBytes =
                    connection.scalar("SELECT json_agg(r ORDER BY id)::text FROM migration_runner.migration_registry r")
                same(0, runner.migrateDown(0, dryRun = true), "populated-down-dry-run")
                same(priorRows, Registry(connection).rows(), "dry-run-preserves-registry")
                val failure = assertThrows(MigrationFailed::class.java) { runner.migrateDown(0, dryRun = false) }
                same("P0001", failure.failure.sqlState, "reverse-refusal-state")
                same("ledger_history_preserved", failure.failure.constraint, "reverse-refusal-constraint")
                same(2, failure.knownVersion, "reverse-known-version")
                same(2, runner.snapshot().currentVersion, "reverse-version-retained")
                same(null, runner.snapshot().lock, "reverse-lock-released")
                same(before, fingerprint(connection), "all-ledger-row-bytes-retained")
                same("3", connection.scalar("SELECT count(*) FROM migration_runner.migration_registry"), "failed-attempt-recorded")
                same(
                    "failed",
                    connection.scalar("SELECT state FROM migration_runner.migration_registry ORDER BY id DESC LIMIT 1"),
                    "failed-direction-retained",
                )
                val history = Registry(connection).rows()
                same(priorRows, history.take(2), "prior-attempts-retained")
                same(
                    priorBytes,
                    connection.scalar(
                        "SELECT json_agg(r ORDER BY id)::text FROM" +
                            " (SELECT * FROM migration_runner.migration_registry ORDER BY id LIMIT 2) r",
                    ),
                    "all-prior-registry-columns-retained",
                )
                same("down", history.last().direction, "failed-down-direction")
                same(failure.failure, history.last().failure, "failed-summary-retained")
                same(
                    priorRows.last(),
                    runner
                        .snapshot()
                        .states
                        .getValue(2)
                        .appliedRow,
                    "applied-row-retained",
                )
                same(0, runner.migrate(null, includeContract = false, dryRun = false), "restore-and-noop")
                same(before, fingerprint(connection), "no-data-cleanup-in-recovery")
                same(
                    currenciesBefore,
                    connection.scalar("SELECT json_agg(c ORDER BY code)::text FROM pennilogic.ledger_currencies c"),
                    "currency-reference-retained",
                )
                same(history, Registry(connection).rows(), "no-op-preserves-failed-attempt")
                val set = MigrationSet.load(shippedLedger)
                for (migration in set.migrations) {
                    same(
                        migration.checksum,
                        connection.scalar(
                            "SELECT checksum FROM migration_runner.migration_registry WHERE version = ? AND state = 'applied'",
                            migration.version,
                        ),
                        "registry-up-checksum-${migration.version}",
                    )
                    same(
                        migration.reversal.checksum,
                        connection.scalar(
                            "SELECT reversal_checksum FROM migration_runner.migration_registry WHERE version = ? AND state = 'applied'",
                            migration.version,
                        ),
                        "registry-down-checksum-${migration.version}",
                    )
                }
                val status = runner.status()
                same(2, status["currentVersion"], "status-effective-version")
                same(beforeStatus["lastApplied"], status["lastApplied"], "status-last-applied-retained")
                same(null, status["lock"], "status-lock-released")
                val record =
                    (status.getValue("migrations") as List<*>)
                        .map { it as Map<*, *> }
                        .single { it["version"] == 2 }
                same(history.last().attempt(), record["lastAttempt"], "status-failed-attempt")
                val output = Output()
                val statusFile = directory.resolve("refused-status.json")
                same(
                    0,
                    MigrationCli(output.stream, fixture.database.environment()).run(
                        listOf("status", "--migrations", shippedLedger.toString(), "--status-file", statusFile.toString()),
                    ),
                    "native-cli-status",
                )
                same(Json.encode(status) + "\n", Files.readString(statusFile), "status-file-matches-library")
                same(
                    Json.encode(linkedMapOf("event" to "migration_status") + status),
                    output.require("migration_status"),
                    "native-cli-matches-library",
                )
                println(
                    Json.event(
                        "ledger_failed_down_status",
                        "currentVersion" to status["currentVersion"],
                        "state" to record["state"],
                        "lastAttempt" to history.last().attempt(),
                        "dataPreserved" to (before == fingerprint(connection)),
                        "priorRowsPreserved" to (priorRows == history.take(2)),
                        "checksumsPreserved" to
                            history.all {
                                val migration = requireNotNull(set.byVersion(it.version))
                                it.checksum == migration.checksum && it.reversalChecksum == migration.reversal.checksum
                            },
                        "lockReleased" to (status["lock"] == null),
                        "cliAndStatusFileAgree" to true,
                    ),
                )
                same("failed", record["state"], "latest-failure-operator-label")
                same(
                    "failed",
                    runner
                        .snapshot()
                        .states
                        .getValue(2)
                        .label,
                    "snapshot-failure-label",
                )
            }
        }

    @Test
    fun `ledger checksums cannot drift and a failed forward apply rolls back only its own migration`() =
        checked("ledger-runner-contract") {
            val fixture = LedgerFixture()
            for (migration in MigrationSet.load(shippedLedger).migrations) {
                Files.copy(shippedLedger.resolve(migration.file), directory.resolve(migration.file))
                Files.copy(shippedLedger.resolve(migration.reversal.file), directory.resolve(migration.reversal.file))
            }
            fixture.connect().use { connection ->
                for ((file, kind) in listOf(
                    "V002__create_ledger.up.sql" to "checksum_drift",
                    "V002__create_ledger.down.sql" to "reversal_checksum_drift",
                )) {
                    val path = directory.resolve(file)
                    val original = Files.readString(path)
                    Files.writeString(path, original + "\n-- synthetic drift probe\n")
                    val error = assertThrows(RegistryProblem::class.java) { fixture.runner(connection, directory).snapshot() }
                    same(kind, error.kind, "ledger-$kind")
                    same("2", connection.scalar("SELECT count(*) FROM migration_runner.migration_registry"), "no-drift-attempt")
                    Files.writeString(path, original)
                    same(2, fixture.runner(connection, directory).snapshot().currentVersion, "drift-restored")
                }
                same(0, fixture.runner(connection).migrateDown(1, dryRun = false), "empty-ledger-reversed")
                connection.sql("CREATE SCHEMA ledger")
                val failure =
                    assertThrows(MigrationFailed::class.java) {
                        fixture.runner(connection).migrate(null, includeContract = false, dryRun = false)
                    }
                same("42P06", failure.failure.sqlState, "conflicting-schema-state")
                same(1, failure.knownVersion, "partial-forward-version")
                same(null, connection.scalar("SELECT to_regclass('pennilogic.accounts')::text"), "no-partial-ledger")
                same(null, fixture.runner(connection).snapshot().lock, "failed-forward-lock-released")
                connection.sql("DROP SCHEMA ledger RESTRICT")
                same(0, fixture.runner(connection).migrate(null, includeContract = false, dryRun = false), "forward-restored")
                same(2, fixture.runner(connection).snapshot().currentVersion, "forward-known-version")
            }
        }

    private fun fingerprint(connection: Connection): List<String?> =
        listOf("accounts", "transactions", "entries", "statement_snapshots", "exchange_groups").map { table ->
            connection.scalar("SELECT md5(coalesce(string_agg(row_to_json(t)::text, '' ORDER BY id), '')) FROM pennilogic.$table t")
        }
}
