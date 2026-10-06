package com.pennilogic.migration

import org.postgresql.core.BaseConnection
import org.postgresql.core.TransactionState
import java.sql.Connection
import java.sql.SQLException
import java.time.Duration
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** A command-line argument combination the runner refuses before touching the database. */
class UsageError(
    message: String,
) : IllegalArgumentException(message)

/** The registry and the migration files disagree; the details always name the version and file involved. */
class RegistryProblem(
    val kind: String,
    val details: Map<String, Any?>,
) : IllegalStateException("$kind ${Json.encode(details)}")

/** A migration statement failed; the database was rolled back to [knownVersion] and a failed row was recorded. */
class MigrationFailed(
    val migration: Migration,
    val direction: String,
    val failure: FailureSummary,
    val knownVersion: Int,
) : RuntimeException("${migration.id} failed while running $direction; database remains at ${MigrationSet.label(knownVersion)}")

/** Derived state of one version: its latest non-failed transition and its latest attempt of any kind. */
data class VersionState(
    val version: Int,
    val transition: RegistryRow?,
    val lastAttempt: RegistryRow,
) {
    val appliedRow: RegistryRow? get() = transition?.takeIf { it.state == RowState.APPLIED }

    val label: String
        get() =
            when {
                lastAttempt.state == RowState.FAILED -> "failed"
                appliedRow != null -> "applied"
                else -> "reversed"
            }
}

data class Snapshot(
    val registryPresent: Boolean,
    val currentVersion: Int,
    val states: Map<Int, VersionState>,
    val lock: LockClaim?,
)

/**
 * Applies and reverses one [MigrationSet] against one connection, recording every attempt in the
 * registry, holding the single lock while writing and emitting one JSON event per decision.
 */
class MigrationRunner(
    private val connection: Connection,
    private val set: MigrationSet,
    private val identity: Identity,
    private val slowThreshold: Duration,
    private val emit: (String) -> Unit,
) {
    private val registry = Registry(connection)

    /** Reads the registry without writing and verifies it agrees with the files on disk. */
    fun snapshot(): Snapshot {
        if (!registry.exists()) {
            return Snapshot(false, 0, emptyMap(), null)
        }
        val states =
            registry.rows().groupBy { it.version }.mapValues { (version, history) ->
                VersionState(version, history.lastOrNull { it.state != RowState.FAILED }, history.last())
            }
        val applied = states.values.mapNotNull { it.appliedRow }.sortedWith(Comparator.comparingInt { it.version })
        applied.forEachIndexed { index, row ->
            if (row.version != index + 1) {
                throw RegistryProblem("registry_not_contiguous", mapOf("applied" to applied.map { it.version }))
            }
            val migration =
                set.byVersion(row.version)
                    ?: throw RegistryProblem(
                        "applied_migration_missing",
                        mapOf("version" to row.version, "id" to row.id, "directory" to set.directory.toString()),
                    )
            if (row.id != migration.id) {
                throw RegistryProblem(
                    "applied_migration_renamed",
                    mapOf(
                        "version" to row.version,
                        "applied" to row.id,
                        "file" to migration.file,
                    ),
                )
            }
            if (row.checksum != migration.checksum) {
                throw RegistryProblem(
                    "checksum_drift",
                    mapOf("version" to row.version, "file" to migration.file, "applied" to row.checksum, "current" to migration.checksum),
                )
            }
            // The reverse that runs must be the reverse that was recorded as evidence when the migration was applied.
            if (row.reversalChecksum != migration.reversal.checksum) {
                throw RegistryProblem(
                    "reversal_checksum_drift",
                    mapOf(
                        "version" to row.version,
                        "file" to migration.reversal.file,
                        "appliedFile" to row.reversalFile,
                        "applied" to row.reversalChecksum,
                        "current" to migration.reversal.checksum,
                    ),
                )
            }
        }
        return Snapshot(true, applied.size, states, registry.currentLock())
    }

    fun status(): Map<String, Any?> {
        val snapshot = snapshot()
        val lastApplied =
            snapshot.states.values
                .mapNotNull { it.appliedRow }
                .maxByOrNull { it.version }
        return linkedMapOf(
            "registryPresent" to snapshot.registryPresent,
            "currentVersion" to snapshot.currentVersion,
            "latestVersion" to set.latestVersion,
            "lastApplied" to lastApplied?.let { linkedMapOf("version" to it.version, "id" to it.id) + it.attempt() },
            "lock" to snapshot.lock?.payload(),
            "migrations" to
                set.migrations.map { migration ->
                    val state = snapshot.states[migration.version]
                    migration.record() + linkedMapOf("state" to (state?.label ?: "pending"), "lastAttempt" to state?.lastAttempt?.attempt())
                },
        )
    }

    fun migrate(
        target: Int?,
        includeContract: Boolean,
        dryRun: Boolean,
    ): Int {
        if (!dryRun) {
            registry.bootstrap()
        }
        val snapshot = snapshot()
        val goal = target ?: set.latestVersion
        if (goal !in snapshot.currentVersion..set.latestVersion) {
            throw UsageError(
                "target ${MigrationSet.label(goal)} must be between the current version" +
                    " ${MigrationSet.label(snapshot.currentVersion)} and the latest migration ${MigrationSet.label(set.latestVersion)}",
            )
        }
        var held = false
        val steps =
            set.migrations.filter { it.version in snapshot.currentVersion + 1..goal }.map { migration ->
                if (migration.phase == Phase.CONTRACT && !includeContract) {
                    held = true
                }
                migration to if (held) "hold" else "apply"
            }
        emit(plan("migrate", dryRun, snapshot.currentVersion, goal, steps))
        if (dryRun) {
            return 0
        }
        if (steps.none { it.second == "apply" }) {
            // Nothing is written, so no lock is needed; the hold is still reported.
            steps.forEach { (migration, _) -> emitHeld(migration) }
            return 0
        }
        val claim = registry.claim(identity, "migrate", goal)
        emit(Json.encode(linkedMapOf("event" to "migration_lock_claimed") + claim.payload()))
        var released = false
        try {
            confirmUnchanged(snapshot.currentVersion)
            for ((migration, action) in steps) {
                if (action == "hold") {
                    emitHeld(migration)
                } else {
                    execute(migration, "up", migration.sql, RowState.APPLIED, migration.version - 1)
                }
            }
        } finally {
            released = releaseOwnLock()
        }
        return if (released) 0 else 1
    }

    fun migrateDown(
        target: Int?,
        dryRun: Boolean,
    ): Int {
        if (!dryRun) {
            registry.bootstrap()
        }
        val snapshot = snapshot()
        val current = snapshot.currentVersion
        val goal = target ?: (current - 1).coerceAtLeast(0)
        if (goal !in 0..current) {
            throw UsageError(
                "target ${MigrationSet.label(
                    goal,
                )} must be between ${MigrationSet.label(0)} and the current version ${MigrationSet.label(current)}",
            )
        }
        val steps = (current downTo goal + 1).map { set.migrations[it - 1] to "reverse" }
        emit(plan("migrate-down", dryRun, current, goal, steps))
        if (dryRun || steps.isEmpty()) {
            return 0
        }
        val claim = registry.claim(identity, "migrate-down", goal)
        emit(Json.encode(linkedMapOf("event" to "migration_lock_claimed") + claim.payload()))
        var released = false
        try {
            confirmUnchanged(current)
            for ((migration, _) in steps) {
                execute(migration, "down", migration.reversal.sql, RowState.REVERSED, migration.version)
            }
        } finally {
            released = releaseOwnLock()
        }
        return if (released) 0 else 1
    }

    /** Operator release of a lock whose holder process has died; the exact claim (holder, host, pid) must be named. */
    fun releaseLock(claim: Identity): Boolean {
        registry.bootstrap()
        return registry.release(claim)
    }

    /** The plan was computed before the claim; another runner finishing in between makes it stale. */
    private fun confirmUnchanged(planned: Int) {
        val current = snapshot().currentVersion
        if (current != planned) {
            throw RegistryProblem("registry_changed_before_lock", mapOf("planned" to planned, "current" to current))
        }
    }

    private fun execute(
        migration: Migration,
        direction: String,
        sql: String,
        success: RowState,
        knownVersion: Int,
    ) {
        val started = System.nanoTime()
        var failure: FailureSummary? = null
        connection.autoCommit = false
        try {
            watchdog(migration, direction) { connection.createStatement().use { statement -> statement.execute(sql) } }
            check(connection.unwrap(BaseConnection::class.java).transactionState == TransactionState.OPEN) {
                "${migration.id} ended its own transaction: COMMIT or ROLLBACK inside a migration script is forbidden;" +
                    " inspect the database, its changes may be committed without a registry row"
            }
            registry.record(migration, success, direction, identity, elapsedMillis(started), null)
            connection.commit()
        } catch (error: SQLException) {
            failure = FailureSummary.of(error)
        } finally {
            // A rollback after a commit is a no-op; otherwise it undoes every statement of the partial migration.
            connection.rollback()
            connection.autoCommit = true
        }
        if (failure != null) {
            registry.record(migration, RowState.FAILED, direction, identity, elapsedMillis(started), failure)
            throw MigrationFailed(migration, direction, failure, knownVersion)
        }
        emit(
            Json.event(
                if (success == RowState.APPLIED) "migration_applied" else "migration_reversed",
                "version" to migration.version,
                "id" to migration.id,
                "phase" to migration.phase.directive,
                "checksum" to migration.checksum,
                "reversal" to migration.reversal.kind.directive,
                "reason" to migration.reversal.reason,
                "durationMs" to elapsedMillis(started),
            ),
        )
    }

    private fun <T> watchdog(
        migration: Migration,
        direction: String,
        block: () -> T,
    ): T {
        val executor =
            Executors.newSingleThreadScheduledExecutor { runnable -> Thread(runnable, "migration-watchdog").apply { isDaemon = true } }
        executor.schedule(
            Runnable {
                emit(
                    Json.event(
                        "migration_slow",
                        "version" to migration.version,
                        "id" to migration.id,
                        "direction" to direction,
                        "thresholdSeconds" to slowThreshold.seconds,
                    ),
                )
            },
            slowThreshold.toMillis(),
            TimeUnit.MILLISECONDS,
        )
        try {
            return block()
        } finally {
            executor.shutdownNow()
        }
    }

    /** Releases the runner's own claim; false means the claim was gone and a second writer may have overlapped. */
    private fun releaseOwnLock(): Boolean {
        val released = registry.release(identity)
        emit(
            Json.event(
                if (released) "migration_lock_released" else "migration_lock_release_mismatch",
                "holder" to identity.holder,
                "host" to identity.host,
                "pid" to identity.pid,
            ),
        )
        return released
    }

    private fun emitHeld(migration: Migration) {
        emit(
            Json.event(
                "migration_contract_held",
                "version" to migration.version,
                "id" to migration.id,
                "expandVersion" to migration.expandVersion,
                "reason" to "contract migrations run only with --include-contract once every consumer has migrated",
            ),
        )
    }

    private fun plan(
        command: String,
        dryRun: Boolean,
        current: Int,
        goal: Int,
        steps: List<Pair<Migration, String>>,
    ): String =
        Json.event(
            "migration_plan",
            "command" to command,
            "dryRun" to dryRun,
            "currentVersion" to current,
            "targetVersion" to goal,
            "steps" to
                steps.map { (migration, action) ->
                    linkedMapOf(
                        "version" to migration.version,
                        "id" to migration.id,
                        "phase" to migration.phase.directive,
                        "action" to action,
                        "reversal" to migration.reversal.kind.directive,
                    )
                },
        )

    private fun elapsedMillis(started: Long): Long = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started)
}
