package com.pennilogic.ledger

import com.pennilogic.contracts.money.Money
import com.pennilogic.migration.AdmissionFixtures
import com.pennilogic.migration.Identity
import com.pennilogic.migration.MigrationFailed
import com.pennilogic.migration.MigrationRunner
import com.pennilogic.migration.TestDatabase
import org.junit.jupiter.api.Assertions.assertTrue
import org.postgresql.util.PSQLException
import java.nio.file.Path
import java.sql.Connection
import java.sql.DriverManager
import java.sql.SQLException
import java.sql.Types
import java.time.Duration
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit
import java.util.Properties
import java.util.UUID
import java.util.concurrent.atomic.AtomicLong

internal val shippedLedger: Path = Path.of("src", "main", "resources", "db", "migrations")
private val identifiers = AtomicLong()

internal fun ledgerId(): UUID = UUID.fromString("019a0000-0000-7000-8000-" + identifiers.incrementAndGet().toString(16).padStart(12, '0'))

internal fun ledgerNow(): OffsetDateTime = OffsetDateTime.now(ZoneOffset.UTC).truncatedTo(ChronoUnit.MILLIS)

internal fun <T> same(
    expected: T,
    actual: T,
    caseId: String,
) {
    assertTrue(expected == actual, caseId)
}

internal fun <T> checked(
    caseId: String,
    action: () -> T,
): T =
    try {
        action()
    } catch (error: SQLException) {
        // Never attach the server exception: DETAIL, SQL and bind values can contain money.
        throw AssertionError("$caseId sqlState=${error.sqlState} constraint=${(error as? PSQLException)?.serverErrorMessage?.constraint}")
    } catch (error: MigrationFailed) {
        throw AssertionError(
            "$caseId migration=${error.migration.id} sqlState=${error.failure.sqlState}" +
                " constraint=${error.failure.constraint} position=${error.failure.position}",
        )
    }

internal fun rejected(
    caseId: String,
    state: String,
    constraint: String? = null,
    action: () -> Unit,
) {
    var refused = false
    try {
        action()
    } catch (error: SQLException) {
        refused = true
        same(state, error.sqlState, "$caseId-state")
        if (constraint != null) {
            same(constraint, (error as? PSQLException)?.serverErrorMessage?.constraint, "$caseId-constraint")
        }
    }
    assertTrue(refused, "$caseId-must-refuse")
}

internal fun Connection.sql(
    statement: String,
    vararg values: Any?,
): Int =
    prepareStatement(statement).use { query ->
        values.forEachIndexed { index, value ->
            if (value == null) query.setNull(index + 1, Types.NULL) else query.setObject(index + 1, value)
        }
        query.executeUpdate()
    }

internal fun Connection.scalar(
    statement: String,
    vararg values: Any?,
): String? =
    prepareStatement(statement).use { query ->
        values.forEachIndexed { index, value -> query.setObject(index + 1, value) }
        query.executeQuery().use { rows ->
            check(rows.next()) { "ledger-scalar-row-absent" }
            rows.getString(1)
        }
    }

internal fun Connection.account(
    owner: UUID,
    type: String = "ASSET",
    currency: String = "INR",
    systemRole: String? = null,
    id: UUID = ledgerId(),
): UUID {
    sql(
        "INSERT INTO pennilogic.accounts (id, owner_id, type, currency, system_role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        id,
        owner,
        type,
        currency,
        systemRole,
        ledgerNow(),
    )
    return id
}

internal fun Connection.transaction(
    owner: UUID,
    id: UUID = ledgerId(),
    currency: String = "INR",
    kind: String = "STANDARD",
    status: String = "posted",
    count: Int = 2,
    occurred: OffsetDateTime = ledgerNow(),
    booked: OffsetDateTime? = ledgerNow(),
    valueDate: java.time.LocalDate? = null,
    reverses: UUID? = null,
    replaces: UUID? = null,
    correction: UUID? = null,
    reason: String? = null,
    reasonRef: UUID? = null,
    snapshot: UUID? = null,
    group: UUID? = null,
): UUID {
    sql(
        """INSERT INTO pennilogic.transactions
            (id, owner_id, currency, kind, status, entry_count, occurred_at, booked_at, created_at, value_date,
             reverses, replaces, correction_id, reason_code, reason_ref, reconciles_snapshot_id, exchange_group_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        id,
        owner,
        currency,
        kind,
        status,
        count,
        occurred,
        booked,
        ledgerNow(),
        valueDate,
        reverses,
        replaces,
        correction,
        reason,
        reasonRef,
        snapshot,
        group,
    )
    return id
}

internal fun Connection.rawEntry(
    transaction: UUID,
    owner: UUID,
    account: UUID,
    units: Long?,
    currency: String = "INR",
) {
    sql(
        "INSERT INTO pennilogic.entries (id, transaction_id, owner_id, account_id, amount_minor, currency) VALUES (?, ?, ?, ?, ?, ?)",
        ledgerId(),
        transaction,
        owner,
        account,
        units,
        currency,
    )
}

internal fun Connection.pair(
    transaction: UUID,
    owner: UUID,
    first: UUID,
    second: UUID,
    value: Money = Money.ofMinorUnits(1, "INR"),
) {
    val opposite = -value
    rawEntry(transaction, owner, first, value.minorUnits, value.currency)
    rawEntry(transaction, owner, second, opposite.minorUnits, opposite.currency)
}

internal class LedgerFixture {
    val database: TestDatabase = TestDatabase.fresh()
    val events = mutableListOf<String>()

    init {
        // Only this fresh database is changed. Expected SQL failures must not log statement text or values.
        database.execute(
            """ALTER DATABASE ${database.name} SET log_min_error_statement = 'panic';
               ALTER DATABASE ${database.name} SET log_error_verbosity = 'terse';
               ALTER DATABASE ${database.name} SET log_statement = 'none';
               ALTER DATABASE ${database.name} SET log_parameter_max_length = 0;
               ALTER DATABASE ${database.name} SET log_parameter_max_length_on_error = 0;""",
        )
        connect().use { same(0, runner(it).migrate(null, includeContract = false, dryRun = false), "ledger-migration") }
    }

    fun connect(): Connection = database.connect()

    fun runner(
        connection: Connection,
        directory: Path = shippedLedger,
        holder: String = "ledger-test@junit",
    ): MigrationRunner =
        MigrationRunner(connection, AdmissionFixtures.load(directory), Identity(holder, "fixture", 1), Duration.ofSeconds(60), events::add)

    fun <T> restricted(action: (Connection, String) -> T): T {
        val role = "ledger_test_" + UUID.randomUUID().toString().replace("-", "")
        val password = UUID.randomUUID().toString()
        connect().use { admin ->
            admin.sql("CREATE ROLE $role LOGIN PASSWORD '$password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT")
            try {
                admin.sql("GRANT USAGE ON SCHEMA pennilogic, ledger TO $role")
                admin.sql(
                    "GRANT SELECT, INSERT ON pennilogic.accounts, pennilogic.transactions, pennilogic.entries," +
                        " pennilogic.statement_snapshots, pennilogic.exchange_groups TO $role",
                )
                admin.sql("GRANT SELECT ON pennilogic.ledger_currencies TO $role")
                admin.sql("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ledger TO $role")
                val properties =
                    Properties().apply {
                        setProperty("user", role)
                        setProperty("password", password)
                    }
                return DriverManager.getConnection(database.url, properties).use { connection ->
                    same(role, connection.scalar("SELECT current_user"), "distinct-restricted-login")
                    same(
                        "false",
                        connection.scalar("SELECT (rolsuper OR rolbypassrls)::text FROM pg_roles WHERE rolname = current_user"),
                        "restricted-attributes",
                    )
                    action(connection, role)
                }
            } finally {
                admin.sql(
                    "REVOKE ALL ON pennilogic.accounts, pennilogic.transactions, pennilogic.entries," +
                        " pennilogic.statement_snapshots, pennilogic.exchange_groups, pennilogic.ledger_currencies FROM $role",
                )
                admin.sql("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ledger FROM $role")
                admin.sql("REVOKE ALL ON SCHEMA pennilogic, ledger FROM $role")
                admin.sql("DROP ROLE $role")
                same("0", admin.scalar("SELECT count(*) FROM pg_roles WHERE rolname = ?", role), "restricted-role-cleaned")
            }
        }
    }
}
