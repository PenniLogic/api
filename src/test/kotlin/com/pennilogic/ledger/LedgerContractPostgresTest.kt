package com.pennilogic.ledger

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import com.pennilogic.migration.Checksum
import com.pennilogic.migration.MigrationFailed
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import java.sql.Connection

@Tag("postgres")
class LedgerContractPostgresTest {
    @Test
    fun `the PostgreSQL schema consumes the exact accepted ADR inventory and ledger parameters`() =
        checked("accepted-ledger-contract") {
            val ledger = source("adr-017-parameters.json", "f82aa3d76108825f6b7015af496447d9b069bebba1db9a0c4bf351f135a1cef6")
            val crypto = source("adr-018-inventory.json", "86056ca2e2c5c5eeb5faad10a65ef99111ed92de5e60b66f7f3ebbcccea3d7d6")
            val inventories =
                crypto.getValue("tables").jsonArray.associate { item ->
                    item.jsonObject
                        .getValue("table")
                        .jsonPrimitive.content to item.jsonObject
                }
            val account = inventories.getValue("accounts")
            val transaction = inventories.getValue("transactions")
            val appendOnly = ledger.getValue("append_only").jsonObject
            val exchange = ledger.getValue("exchange").jsonObject
            val snapshot = ledger.getValue("statement_snapshot").jsonObject
            val currency = ledger.getValue("ledger_currencies").jsonObject
            // These upstream ingestion/category/dedupe contracts are not supplied by this local unit.
            val unavailableColumns =
                setOf(
                    "source",
                    "source_confidence",
                    "needs_review",
                    "category_id",
                    "source_event_id",
                    "dedupe_key",
                    "dedupe_key_version",
                )
            val expected =
                mapOf(
                    "accounts" to account.strings("constrained") + account.strings("encrypted") +
                        account.strings("blind_indexed") + setOf("system_role", "mask_bidx"),
                    "transactions" to transaction.strings("constrained") - unavailableColumns +
                        transaction.strings("encrypted") + transaction.strings("blind_indexed") +
                        appendOnly.strings("transactions_immutable_columns") +
                        appendOnly.getValue("transactions_write_once_columns").jsonObject.keys +
                        exchange.getValue("descriptive_merchant_amount").jsonObject.strings("columns") +
                        setOf("merchant_bidx", "external_ref_bidx"),
                    "entries" to ledger.getValue("entry").jsonObject.strings("columns"),
                    "ledger_currencies" to currency.strings("columns"),
                    "statement_snapshots" to snapshot.strings("columns"),
                    "exchange_groups" to exchange.strings("columns"),
                )
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                for ((table, columns) in expected) {
                    val actual =
                        requireNotNull(
                            connection.scalar(
                                "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns" +
                                    " WHERE table_schema = 'pennilogic' AND table_name = ?",
                                table,
                            ),
                        ).split(",").toSet()
                    same(columns, actual, "accepted-column-inventory-$table")
                }
                for ((table, constraint, values) in listOf(
                    Triple("accounts", "accounts_type_check", ledger.strings("account_types")),
                    Triple("transactions", "transactions_kind_check", ledger.strings("transaction_kinds")),
                    Triple("transactions", "transactions_reason_code_check", ledger.strings("reason_codes")),
                    Triple("statement_snapshots", "statement_snapshots_source_check", snapshot.strings("sources")),
                    Triple("exchange_groups", "exchange_groups_rate_source_check", exchange.strings("rate_sources")),
                )) {
                    val definition =
                        requireNotNull(
                            connection.scalar(
                                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = ?::regclass AND conname = ?",
                                "pennilogic.$table",
                                constraint,
                            ),
                        )
                    val actual = Regex("'([A-Z_]+)'::text").findAll(definition).map { it.groupValues[1] }.toSet()
                    same(values, actual, "accepted-domain-$constraint")
                }
                same(
                    currency.strings("mvp_admitted"),
                    requireNotNull(
                        connection.scalar("SELECT string_agg(code, ',' ORDER BY code) FROM pennilogic.ledger_currencies"),
                    ).split(",").toSet(),
                    "accepted-mvp-admission",
                )
                for (code in currency.strings("mvp_admitted")) {
                    same(
                        CurrencyRegistry.exponentOf(code).toString(),
                        connection.scalar("SELECT exponent FROM pennilogic.ledger_currencies WHERE code = ?", code),
                        "accepted-currency-exponent",
                    )
                }
                val range =
                    ledger
                        .getValue("money")
                        .jsonObject
                        .getValue("range_minor")
                        .jsonObject
                val maximum = Money.ofMinorUnits(Money.MAX_MINOR_UNITS, "INR")
                val minimum = -maximum
                same(range.getValue("max").jsonPrimitive.content, maximum.minorUnits.toString(), "accepted-maximum")
                same(range.getValue("min").jsonPrimitive.content, minimum.minorUnits.toString(), "accepted-minimum")
                for ((table, columns) in mapOf(
                    "accounts" to account.strings("encrypted") + account.strings("blind_indexed") + "mask_bidx",
                    "transactions" to transaction.strings("encrypted") + transaction.strings("blind_indexed") +
                        setOf("reason_note", "merchant_bidx", "external_ref_bidx", "merchant_amount_minor"),
                )) {
                    for (column in columns) {
                        same(
                            "bytea",
                            connection.scalar(
                                "SELECT data_type FROM information_schema.columns" +
                                    " WHERE table_schema = 'pennilogic' AND table_name = ? AND column_name = ?",
                                table,
                                column,
                            ),
                            "ciphertext-reservation-$table-$column",
                        )
                    }
                }
            }
        }

    @Test
    fun `a non bypass table owner still cannot read history or reverse behind forced RLS`() =
        checked("forced-owner-rls") {
            val fixture = LedgerFixture()
            fixture.connect().use { admin ->
                admin.account(ledgerId())
                val originalOwner = requireNotNull(admin.scalar("SELECT current_user"))
                val ledgerTables =
                    listOf(
                        "accounts",
                        "transactions",
                        "entries",
                        "statement_snapshots",
                        "exchange_groups",
                        "ledger_currencies",
                    ).map { "pennilogic.$it" }
                val ownedTables = ledgerTables + listOf("migration_runner.migration_registry", "migration_runner.migration_lock")
                fixture.restricted { owner, role ->
                    admin.sql("GRANT CREATE ON SCHEMA pennilogic TO $role")
                    admin.sql("ALTER DATABASE ${fixture.database.name} OWNER TO $role")
                    admin.sql("ALTER SCHEMA migration_runner OWNER TO $role")
                    for (table in ownedTables) admin.sql("ALTER TABLE $table OWNER TO $role")
                    try {
                        same("0", owner.scalar("SELECT count(*) FROM pennilogic.accounts"), "force-owner-default-deny")
                        owner.sql("SET row_security = off")
                        rejected("force-owner-refuses-bypass", "42501") { owner.scalar("SELECT count(*) FROM pennilogic.accounts") }
                        owner.sql("SET row_security = on")
                        val runner = fixture.runner(owner)
                        val failure = assertThrows(MigrationFailed::class.java) { runner.migrateDown(1, dryRun = false) }
                        same("42501", failure.failure.sqlState, "force-owner-reverse-refused")
                        same(2, runner.snapshot().currentVersion, "force-owner-version-preserved")
                        same(null, runner.snapshot().lock, "force-owner-lock-released")
                        same("1", admin.scalar("SELECT count(*) FROM pennilogic.accounts"), "force-owner-invisible-history-preserved")
                    } finally {
                        for (table in ownedTables) admin.sql("ALTER TABLE $table OWNER TO \"$originalOwner\"")
                        admin.sql("ALTER SCHEMA migration_runner OWNER TO \"$originalOwner\"")
                        admin.sql("ALTER DATABASE ${fixture.database.name} OWNER TO \"$originalOwner\"")
                    }
                }
                same(
                    "true",
                    admin.scalar(
                        "SELECT (relrowsecurity AND relforcerowsecurity)::text FROM pg_class WHERE oid = 'pennilogic.accounts'::regclass",
                    ),
                    "force-owner-policy-preserved",
                )
            }
        }

    private fun source(
        file: String,
        checksum: String,
    ): JsonObject {
        val text =
            requireNotNull(javaClass.getResourceAsStream("/ledger/$file")) { "accepted-source-missing" }
                .bufferedReader(Charsets.UTF_8)
                .use { it.readText() }
        same(checksum, Checksum.of(text), "accepted-source-checksum-$file")
        return Json.parseToJsonElement(text).jsonObject
    }

    private fun JsonObject.strings(key: String): Set<String> = getValue(key).jsonArray.map { it.jsonPrimitive.content }.toSet()
}
