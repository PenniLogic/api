package com.pennilogic.migration

import java.io.IOException
import java.io.PrintStream
import java.net.InetAddress
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.sql.DriverManager
import java.sql.SQLException
import java.time.Duration
import java.util.Properties
import java.util.TimeZone
import kotlin.system.exitProcess

fun main(args: Array<String>) {
    // Registry timestamps are UTC; pinning the JVM zone also keeps the session independent of legacy zone names.
    TimeZone.setDefault(TimeZone.getTimeZone("UTC"))
    exitProcess(MigrationCli(System.out, System.getenv()).run(args.toList()))
}

enum class Command(
    val token: String,
) {
    VALIDATE("validate"),
    STATUS("status"),
    MIGRATE("migrate"),
    MIGRATE_DOWN("migrate-down"),
    RELEASE_LOCK("release-lock"),
}

data class Options(
    val command: Command,
    val migrations: Path,
    /** The identity recorded in the registry; "read-only" for commands that never write. */
    val holder: String,
    /** The exact claim to clear; only release-lock takes it, and only in full. */
    val releaseClaim: Identity?,
    val target: Int?,
    val includeContract: Boolean,
    val dryRun: Boolean,
    val statusFile: Path?,
    val slowThreshold: Duration,
)

/**
 * Command-line entry point. Connection settings come only from the environment
 * (MIGRATION_JDBC_URL, MIGRATION_DB_USER, MIGRATION_DB_PASSWORD) and are never printed.
 * Exit codes: 0 success, 1 refused, failed or ended without its own lock (an event names why), 2 usage error.
 */
class MigrationCli internal constructor(
    private val out: PrintStream,
    private val environment: Map<String, String>,
    private val admission: () -> DatabaseAdmission,
) {
    constructor(out: PrintStream, environment: Map<String, String>) : this(out, environment, AdmissionInstallation::load)

    fun run(args: List<String>): Int {
        val options =
            try {
                parse(args)
            } catch (error: UsageError) {
                return usage(error)
            }
        return try {
            when (options.command) {
                Command.VALIDATE -> validate(options)
                Command.STATUS -> withRunner(options) { runner -> status(runner, options) }
                Command.MIGRATE -> withRunner(options) { runner -> runner.migrate(options.target, options.includeContract, options.dryRun) }
                Command.MIGRATE_DOWN -> withRunner(options) { runner -> runner.migrateDown(options.target, options.dryRun) }
                Command.RELEASE_LOCK -> withRunner(options) { runner -> releaseLock(runner, requireNotNull(options.releaseClaim)) }
            }
        } catch (error: UsageError) {
            usage(error)
        } catch (error: ConventionViolation) {
            emit(Json.event("migration_validation_failed", "file" to error.file, "rule" to error.rule))
            1
        } catch (error: RegistryProblem) {
            emit(Json.encode(linkedMapOf("event" to "migration_registry_problem", "kind" to error.kind) + error.details))
            1
        } catch (error: LockRefused) {
            emit(Json.encode(linkedMapOf("event" to "migration_lock_refused") + error.claim.payload()))
            1
        } catch (error: AdmissionRefused) {
            emit(Json.event("migration_admission_refused", "code" to error.code))
            1
        } catch (error: IllegalStateException) {
            emit(Json.event("runner_error", "type" to error.javaClass.name, "message" to error.message))
            1
        } catch (error: IOException) {
            // A status file that cannot be written or a host name that cannot be resolved; paths only, never credentials.
            emit(Json.event("runner_error", "type" to error.javaClass.name, "message" to error.message))
            1
        } catch (error: MigrationFailed) {
            emit(
                Json.encode(
                    linkedMapOf(
                        "event" to "migration_failed",
                        "version" to error.migration.version,
                        "id" to error.migration.id,
                        "direction" to error.direction,
                        "knownVersion" to error.knownVersion,
                    ) + error.failure.fields(),
                ),
            )
            1
        } catch (error: SQLException) {
            // The server's free text can contain row values, so only its codes and identifiers are reported.
            emit(Json.encode(linkedMapOf("event" to "database_error", "type" to error.javaClass.name) + FailureSummary.of(error).fields()))
            1
        }
    }

    private fun validate(options: Options): Int {
        val set = MigrationSet.load(options.migrations, admission)
        emit(
            Json.event(
                "migration_set",
                "directory" to set.directory.toString(),
                "count" to set.migrations.size,
                "migrations" to set.records(),
            ),
        )
        return 0
    }

    private fun withRunner(
        options: Options,
        block: (MigrationRunner) -> Int,
    ): Int {
        val set = MigrationSet.load(options.migrations, admission)
        val url = required("MIGRATION_JDBC_URL")
        if (!url.startsWith("jdbc:postgresql:")) {
            throw UsageError("MIGRATION_JDBC_URL must be a jdbc:postgresql: URL")
        }
        val properties =
            Properties().apply {
                setProperty("user", required("MIGRATION_DB_USER"))
                setProperty("password", required("MIGRATION_DB_PASSWORD"))
                setProperty("ApplicationName", "pennilogic-migration")
            }
        val identity = Identity(options.holder, InetAddress.getLocalHost().hostName, ProcessHandle.current().pid())
        DriverManager.getConnection(url, properties).use { connection ->
            return block(MigrationRunner(connection, set, identity, options.slowThreshold, ::emit))
        }
    }

    private fun status(
        runner: MigrationRunner,
        options: Options,
    ): Int {
        val report = runner.status()
        emit(Json.encode(linkedMapOf("event" to "migration_status") + report))
        options.statusFile?.let { writeAtomically(it, Json.encode(report) + "\n") }
        return 0
    }

    /** A reader never sees a partial status file: the content lands in a sibling temp file and is renamed over the target. */
    private fun writeAtomically(
        target: Path,
        content: String,
    ) {
        val directory = target.toAbsolutePath().parent
        val temporary = Files.createTempFile(directory, ".${target.fileName}.", ".tmp")
        try {
            Files.writeString(temporary, content)
            Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } finally {
            Files.deleteIfExists(temporary)
        }
    }

    private fun releaseLock(
        runner: MigrationRunner,
        claim: Identity,
    ): Int {
        val fields = arrayOf("holder" to claim.holder, "host" to claim.host, "pid" to claim.pid)
        return if (runner.releaseLock(claim)) {
            emit(Json.event("migration_lock_released", *fields, "releasedBy" to "operator"))
            0
        } else {
            emit(Json.event("migration_lock_not_held", *fields))
            1
        }
    }

    private fun required(name: String): String = environment[name] ?: throw UsageError("environment variable $name is required")

    private fun usage(error: UsageError): Int {
        emit(Json.event("usage_error", "message" to error.message, "usage" to USAGE))
        return 2
    }

    private fun emit(line: String) {
        out.println(line)
    }

    companion object {
        const val USAGE =
            "validate|status|migrate|migrate-down|release-lock [--migrations DIR] [--holder ID] [--host NAME] [--pid N]" +
                " [--target N] [--include-contract] [--dry-run] [--status-file PATH] [--slow-threshold-seconds N]"
        private val HOLDER = Regex("""^[A-Za-z0-9][A-Za-z0-9._@#/:-]{0,119}$""")

        /** Mutable parse state; each option is handled by one table entry so every path is plainly testable. */
        private class Arguments {
            var migrations: Path = Path.of("src", "main", "resources", "db", "migrations")
            var holder: String? = null
            var host: String? = null
            var pid: Long? = null
            var target: Int? = null
            var includeContract = false
            var dryRun = false
            var statusFile: Path? = null
            var slowThreshold: Duration = Duration.ofSeconds(60)
        }

        private val OPTIONS: Map<String, Arguments.(Iterator<String>, String) -> Unit> =
            mapOf(
                "--include-contract" to { _, _ -> includeContract = true },
                "--dry-run" to { _, _ -> dryRun = true },
                "--migrations" to { iterator, option -> migrations = Path.of(value(iterator, option)) },
                "--holder" to { iterator, option -> holder = value(iterator, option) },
                "--host" to { iterator, option -> host = value(iterator, option) },
                "--pid" to { iterator, option ->
                    pid =
                        value(iterator, option).toLongOrNull()?.takeIf { it >= 0 }
                            ?: throw UsageError("$option requires a non-negative integer")
                },
                "--status-file" to { iterator, option -> statusFile = Path.of(value(iterator, option)) },
                "--target" to { iterator, option ->
                    target =
                        value(iterator, option).toIntOrNull()?.takeIf { it >= 0 }
                            ?: throw UsageError("$option requires a non-negative integer version")
                },
                "--slow-threshold-seconds" to { iterator, option ->
                    slowThreshold =
                        value(iterator, option).toLongOrNull()?.takeIf { it > 0 }?.let(Duration::ofSeconds)
                            ?: throw UsageError("$option requires a positive integer")
                },
            )

        fun parse(args: List<String>): Options {
            val command =
                Command.entries.firstOrNull { it.token == args.firstOrNull() }
                    ?: throw UsageError("expected a command: ${Command.entries.joinToString(", ") { it.token }}")
            val arguments = Arguments()
            val iterator = args.drop(1).iterator()
            while (iterator.hasNext()) {
                val option = iterator.next()
                val handler = OPTIONS[option] ?: throw UsageError("unknown option $option")
                arguments.handler(iterator, option)
            }
            val holder = arguments.holder
            if (holder != null && !HOLDER.matches(holder)) {
                throw UsageError("--holder must be 1-120 characters of letters, digits and . _ @ # / : -")
            }
            val writes = command == Command.RELEASE_LOCK || (command != Command.VALIDATE && command != Command.STATUS && !arguments.dryRun)
            if (writes && holder == null) {
                throw UsageError("--holder is required for ${command.token} because the registry records who applied each change")
            }
            val releaseClaim = releaseClaim(command, holder, arguments.host, arguments.pid)
            return Options(
                command = command,
                migrations = arguments.migrations,
                holder = holder ?: "read-only",
                releaseClaim = releaseClaim,
                target = arguments.target,
                includeContract = arguments.includeContract,
                dryRun = arguments.dryRun,
                statusFile = arguments.statusFile,
                slowThreshold = arguments.slowThreshold,
            )
        }

        /** release-lock names the exact claim from the refusal event; the other commands must not carry host or pid. */
        private fun releaseClaim(
            command: Command,
            holder: String?,
            host: String?,
            pid: Long?,
        ): Identity? {
            if (command != Command.RELEASE_LOCK) {
                if (host != null || pid != null) {
                    throw UsageError("--host and --pid are only valid for release-lock")
                }
                return null
            }
            if (host == null || pid == null) {
                throw UsageError(
                    "release-lock requires --holder, --host and --pid exactly as the migration_lock_refused event reports them",
                )
            }
            return Identity(requireNotNull(holder), host, pid)
        }

        private fun value(
            iterator: Iterator<String>,
            option: String,
        ): String = if (iterator.hasNext()) iterator.next() else throw UsageError("$option requires a value")
    }
}
