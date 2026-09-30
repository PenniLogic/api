package com.pennilogic.migration

import org.postgresql.util.PSQLException
import java.sql.Connection
import java.sql.ResultSet
import java.sql.SQLException
import java.sql.Types
import java.time.OffsetDateTime
import java.time.ZoneOffset

/** Who is running the migration; recorded in every registry row and in the lock claim. */
data class Identity(
    val holder: String,
    val host: String,
    val pid: Long,
)

/** The lock claim/release payload shared with the pull-request check; see docs/migrations/REGISTRY.md. */
data class LockClaim(
    val holder: String,
    val host: String,
    val pid: Long,
    val claimedAt: String,
    val command: String,
    val targetVersion: Int,
) {
    fun payload(): Map<String, Any?> =
        linkedMapOf(
            "holder" to holder,
            "host" to host,
            "pid" to pid,
            "claimedAt" to claimedAt,
            "command" to command,
            "targetVersion" to targetVersion,
        )
}

class LockRefused(
    val claim: LockClaim,
) : IllegalStateException(
        "migration lock is held by ${claim.holder} on ${claim.host} (pid ${claim.pid}) since ${claim.claimedAt}" +
            " running ${claim.command} towards ${MigrationSet.label(claim.targetVersion)}",
    )

enum class RowState {
    APPLIED,
    FAILED,
    REVERSED,
    ;

    val value: String get() = name.lowercase()
}

/** Redacted description of a database failure: SQLSTATE and identifiers only, never row values. */
data class FailureSummary(
    val sqlState: String?,
    val schema: String?,
    val table: String?,
    val constraint: String?,
    val column: String?,
    val position: Int?,
) {
    fun fields(): Map<String, Any?> =
        linkedMapOf(
            "sqlState" to sqlState,
            "schema" to schema,
            "table" to table,
            "constraint" to constraint,
            "column" to column,
            "position" to position,
        )

    companion object {
        fun of(error: SQLException): FailureSummary {
            val server = (error as? PSQLException)?.serverErrorMessage
            return FailureSummary(
                sqlState = error.sqlState,
                schema = server?.schema,
                table = server?.table,
                constraint = server?.constraint,
                column = server?.column,
                position = server?.position?.takeIf { it > 0 },
            )
        }
    }
}

data class RegistryRow(
    val version: Int,
    val id: String,
    val state: RowState,
    val direction: String,
    val checksum: String,
    val reversalKind: ReversalKind,
    val reversalReason: String?,
    val appliedBy: String,
    val host: String,
    val attemptedAt: String,
    val durationMs: Long,
    val failure: FailureSummary?,
) {
    fun attempt(): Map<String, Any?> =
        linkedMapOf(
            "state" to state.value,
            "direction" to direction,
            "appliedBy" to appliedBy,
            "host" to host,
            "attemptedAt" to attemptedAt,
            "durationMs" to durationMs,
            "failure" to failure?.fields(),
        )
}

/**
 * JDBC client for the runner-owned registry tables. The registry is append-only: every attempt
 * is one row and the current state of a version is derived from its latest non-failed row.
 */
class Registry(
    private val connection: Connection,
) {
    fun exists(): Boolean =
        connection.createStatement().use { statement ->
            statement
                .executeQuery(
                    "SELECT to_regclass('migration_registry') IS NOT NULL AND to_regclass('migration_lock') IS NOT NULL",
                ).use { rows ->
                    rows.next()
                    rows.getBoolean(1)
                }
        }

    fun bootstrap() {
        connection.createStatement().use { statement -> statement.execute(BOOTSTRAP) }
    }

    fun rows(): List<RegistryRow> =
        connection.createStatement().use { statement ->
            statement.executeQuery(SELECT_ROWS).use { rows ->
                val result = mutableListOf<RegistryRow>()
                while (rows.next()) {
                    result += readRow(rows)
                }
                result
            }
        }

    fun record(
        migration: Migration,
        state: RowState,
        direction: String,
        identity: Identity,
        durationMs: Long,
        failure: FailureSummary?,
    ) {
        connection.prepareStatement(INSERT_ROW).use { statement ->
            statement.setInt(1, migration.version)
            statement.setString(2, migration.id)
            statement.setString(3, migration.phase.directive)
            statement.setString(4, migration.checksum)
            statement.setString(5, migration.reversal.kind.directive)
            statement.setString(6, migration.reversal.file)
            statement.setString(7, migration.reversal.reason)
            statement.setString(8, migration.reversal.checksum)
            statement.setString(9, state.value)
            statement.setString(10, direction)
            statement.setString(11, identity.holder)
            statement.setString(12, identity.host)
            statement.setLong(13, identity.pid)
            statement.setLong(14, durationMs)
            statement.setString(15, failure?.sqlState)
            statement.setString(16, failure?.schema)
            statement.setString(17, failure?.table)
            statement.setString(18, failure?.constraint)
            statement.setString(19, failure?.column)
            statement.setObject(20, failure?.position, Types.INTEGER)
            statement.executeUpdate()
        }
    }

    fun currentLock(): LockClaim? =
        connection.prepareStatement(SELECT_LOCK).use { statement -> statement.executeQuery().use { rows -> readSingleton(rows) } }

    /** Claims the single lock row atomically; a held lock is refused by naming its holder. */
    fun claim(
        identity: Identity,
        command: String,
        targetVersion: Int,
    ): LockClaim {
        connection.autoCommit = false
        try {
            val held =
                connection.prepareStatement("$SELECT_LOCK FOR UPDATE").use { statement ->
                    statement.executeQuery().use { rows -> readSingleton(rows) }
                }
            if (held != null) {
                throw LockRefused(held)
            }
            val claim =
                connection.prepareStatement(CLAIM_LOCK).use { statement ->
                    statement.setString(1, identity.holder)
                    statement.setString(2, identity.host)
                    statement.setLong(3, identity.pid)
                    statement.setString(4, command)
                    statement.setInt(5, targetVersion)
                    statement.executeQuery().use { rows ->
                        rows.next()
                        readClaim(rows)
                    }
                }
            connection.commit()
            return claim
        } catch (error: Exception) {
            connection.rollback()
            throw error
        } finally {
            connection.autoCommit = true
        }
    }

    /** Releases the lock only when the named holder owns it, so a stale claim can never be cleared by accident. */
    fun release(holder: String): Boolean =
        connection.prepareStatement(RELEASE_LOCK).use { statement ->
            statement.setString(1, holder)
            statement.executeUpdate() == 1
        }

    /** Releases the runner's own claim, matching holder, host and pid so a re-claimed lock is left alone. */
    fun releaseOwn(identity: Identity): Boolean =
        connection.prepareStatement("$RELEASE_LOCK AND host = ? AND pid = ?").use { statement ->
            statement.setString(1, identity.holder)
            statement.setString(2, identity.host)
            statement.setLong(3, identity.pid)
            statement.executeUpdate() == 1
        }

    private fun readSingleton(rows: ResultSet): LockClaim? {
        check(rows.next()) { "migration_lock singleton row is missing" }
        return if (rows.getString("holder") == null) null else readClaim(rows)
    }

    private fun readClaim(rows: ResultSet): LockClaim =
        LockClaim(
            holder = rows.getString("holder"),
            host = rows.getString("host"),
            pid = rows.getLong("pid"),
            claimedAt = timestamp(rows, "claimed_at"),
            command = rows.getString("command"),
            targetVersion = rows.getInt("target_version"),
        )

    private fun readRow(rows: ResultSet): RegistryRow {
        val state = RowState.valueOf(rows.getString("state").uppercase())
        val failure =
            if (state == RowState.FAILED) {
                FailureSummary(
                    sqlState = rows.getString("failure_sqlstate"),
                    schema = rows.getString("failure_schema"),
                    table = rows.getString("failure_table"),
                    constraint = rows.getString("failure_constraint"),
                    column = rows.getString("failure_column"),
                    position = rows.getInt("failure_position").takeUnless { rows.wasNull() },
                )
            } else {
                null
            }
        return RegistryRow(
            version = rows.getInt("version"),
            id = rows.getString("migration_id"),
            state = state,
            direction = rows.getString("direction"),
            checksum = rows.getString("checksum"),
            reversalKind = ReversalKind.valueOf(rows.getString("reversal_kind").uppercase()),
            reversalReason = rows.getString("reversal_reason"),
            appliedBy = rows.getString("applied_by"),
            host = rows.getString("host"),
            attemptedAt = timestamp(rows, "attempted_at"),
            durationMs = rows.getLong("duration_ms"),
            failure = failure,
        )
    }

    private fun timestamp(
        rows: ResultSet,
        column: String,
    ): String = rows.getObject(column, OffsetDateTime::class.java).withOffsetSameInstant(ZoneOffset.UTC).toString()

    companion object {
        private val BOOTSTRAP =
            """
            CREATE TABLE IF NOT EXISTS migration_registry (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                version integer NOT NULL CHECK (version > 0),
                migration_id text NOT NULL,
                phase text NOT NULL CHECK (phase IN ('expand', 'migrate', 'contract')),
                checksum text NOT NULL,
                reversal_kind text NOT NULL CHECK (reversal_kind IN ('down', 'compensating')),
                reversal_file text NOT NULL,
                reversal_reason text,
                reversal_checksum text NOT NULL,
                state text NOT NULL CHECK (state IN ('applied', 'failed', 'reversed')),
                direction text NOT NULL CHECK (direction IN ('up', 'down')),
                applied_by text NOT NULL,
                host text NOT NULL,
                pid bigint NOT NULL,
                attempted_at timestamptz NOT NULL DEFAULT now(),
                duration_ms bigint NOT NULL,
                failure_sqlstate text,
                failure_schema text,
                failure_table text,
                failure_constraint text,
                failure_column text,
                failure_position integer
            );
            CREATE TABLE IF NOT EXISTS migration_lock (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                holder text,
                host text,
                pid bigint,
                claimed_at timestamptz,
                command text,
                target_version integer
            );
            INSERT INTO migration_lock (singleton) VALUES (true) ON CONFLICT DO NOTHING;
            """.trimIndent()
        private const val SELECT_ROWS =
            "SELECT version, migration_id, state, direction, checksum, reversal_kind, reversal_reason, applied_by, host," +
                " attempted_at, duration_ms, failure_sqlstate, failure_schema, failure_table, failure_constraint," +
                " failure_column, failure_position FROM migration_registry ORDER BY id"
        private const val INSERT_ROW =
            "INSERT INTO migration_registry (version, migration_id, phase, checksum, reversal_kind, reversal_file," +
                " reversal_reason, reversal_checksum, state, direction, applied_by, host, pid, duration_ms, failure_sqlstate," +
                " failure_schema, failure_table, failure_constraint, failure_column, failure_position)" +
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        private const val SELECT_LOCK = "SELECT holder, host, pid, claimed_at, command, target_version FROM migration_lock WHERE singleton"
        private const val CLAIM_LOCK =
            "UPDATE migration_lock SET holder = ?, host = ?, pid = ?, claimed_at = now(), command = ?, target_version = ?" +
                " WHERE singleton RETURNING holder, host, pid, claimed_at, command, target_version"
        private const val RELEASE_LOCK =
            "UPDATE migration_lock SET holder = NULL, host = NULL, pid = NULL, claimed_at = NULL, command = NULL," +
                " target_version = NULL WHERE singleton AND holder = ?"
    }
}
