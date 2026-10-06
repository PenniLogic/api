package com.pennilogic.migration

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.postgresql.core.BaseConnection
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.sql.Connection
import java.time.Duration
import java.util.concurrent.TimeUnit

/** These entrypoints use the real application JAR and canonical provider, never protocol doubles. */
@Tag("postgres")
class PackagedAdmissionPostgresTest {
    @TempDir
    lateinit var directory: Path

    private val root = Path.of("").toAbsolutePath().normalize()
    private val shipped = root.resolve("src/main/resources/db/migrations")
    private var invocation = 0

    @BeforeEach
    fun requirePostgres() {
        check(TestDatabase.skipReason == null) { "Run integrationTest with its owned PostgreSQL instance" }
    }

    private data class Result(
        val exit: Int,
        val events: List<JsonObject>,
    ) {
        fun event(name: String): JsonObject = events.single { it.admissionString("event") == name }
    }

    private fun command(
        database: TestDatabase,
        vararg arguments: String,
        workingDirectory: Path = root,
        migrations: Path = shipped,
    ): Result {
        val classpath = requireNotNull(System.getProperty("app.test.classpath"))
        assertTrue(classpath.split(File.pathSeparator).all { it.endsWith(".jar") }, "packaged tests require JARs, not class directories")
        val output = directory.resolve("stdout-${invocation++}.jsonl")
        val errors = directory.resolve("stderr-$invocation.log")
        val builder =
            ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp",
                classpath,
                "com.pennilogic.migration.MigrationCliKt",
                *arguments,
                "--holder",
                "packaged@junit",
                "--migrations",
                migrations.toString(),
            ).directory(workingDirectory.toFile()).redirectOutput(output.toFile()).redirectError(errors.toFile())
        builder.environment().apply {
            keys.retainAll(keys.filter { it.uppercase() in setOf("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") }.toSet())
            putAll(database.environment())
        }
        val process = builder.start()
        try {
            assertTrue(process.waitFor(60, TimeUnit.SECONDS), "packaged migration process timed out")
            assertEquals(0L, Files.size(errors), "packaged migration process emitted diagnostics")
            assertTrue(Files.size(output) in 1L..65536L, "packaged migration output exceeded its test bound")
            val events =
                Files.readAllLines(output).map { line ->
                    AdmissionJson.parse(line.toByteArray(Charsets.UTF_8), DatabaseAdmission.OUTPUT_LIMIT).jsonObject
                }
            assertFalse(Files.readString(output).contains(database.password), "packaged output leaked a credential")
            return Result(process.exitValue(), events)
        } finally {
            if (process.isAlive) {
                val children = process.descendants().use { it.toList() }
                children.forEach { it.destroyForcibly() }
                process.destroyForcibly()
                check(process.waitFor(5, TimeUnit.SECONDS)) { "owned packaged process did not terminate" }
            }
        }
    }

    private fun version(
        database: TestDatabase,
        expected: Int,
    ) {
        val status = command(database, "status")
        assertEquals(0, status.exit)
        assertEquals(expected.toString(), status.event("migration_status").getValue("currentVersion").toString())
    }

    private fun noBootstrap(database: TestDatabase) {
        assertNull(database.scalar("SELECT to_regnamespace('migration_runner')"), "refusal created the registry")
        assertNull(database.scalar("SELECT to_regnamespace('pennilogic')"), "refusal created the ledger schema")
        assertNull(database.scalar("SELECT to_regclass('public.unapproved')"), "refusal executed altered SQL")
    }

    private fun copyMigrations(target: Path): Path {
        Files.createDirectories(target)
        Files.list(shipped).use { files ->
            files.forEach { Files.copy(it, target.resolve(it.fileName)) }
        }
        return target
    }

    @Test
    fun `packaged accepted ledger validates and round trips through dry runs no-ops and reapply`() {
        val database = TestDatabase.fresh()
        assertEquals(0, command(database, "validate").exit)
        assertEquals(0, command(database, "migrate", "--dry-run").exit)
        assertEquals(0, command(database, "migrate-down", "--target", "0", "--dry-run").exit)
        version(database, 0)
        noBootstrap(database)

        val applied = command(database, "migrate")
        assertEquals(0, applied.exit)
        assertEquals(2, applied.events.count { it.admissionString("event") == "migration_applied" })
        version(database, 2)
        assertEquals(
            70L,
            database.count("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'pennilogic'"),
        )
        val rows = database.count("SELECT count(*) FROM migration_runner.migration_registry")
        assertEquals(0, command(database, "migrate").exit)
        assertEquals(0, command(database, "migrate-down", "--target", "2").exit)
        assertEquals(0, command(database, "migrate-down", "--target", "0", "--dry-run").exit)
        assertEquals(rows, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        version(database, 2)

        val reversed = command(database, "migrate-down", "--target", "0")
        assertEquals(0, reversed.exit)
        assertEquals(2, reversed.events.count { it.admissionString("event") == "migration_reversed" })
        version(database, 0)
        assertNull(database.scalar("SELECT to_regnamespace('pennilogic')"))
        assertEquals(0, command(database, "migrate").exit)
        version(database, 2)
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock WHERE holder IS NULL"))

        val historyRows = database.count("SELECT count(*) FROM migration_runner.migration_registry")
        database.connect().use { connection ->
            Registry(connection).claim(Identity("packaged@junit", "synthetic-host", 1), "migrate", 2)
        }
        val released = command(database, "release-lock", "--host", "synthetic-host", "--pid", "1")
        assertEquals(0, released.exit)
        assertEquals("operator", released.event("migration_lock_released").admissionString("releasedBy"))
        database.execute("DELETE FROM migration_runner.migration_lock")
        assertEquals(0, command(database, "migrate").exit)
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock WHERE holder IS NULL"))
        assertEquals(historyRows, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
    }

    @Test
    fun `packaged denied source blocks every load surface before the first registry write`() {
        val database = TestDatabase.fresh()
        val migrations = copyMigrations(directory.resolve("altered"))
        val up = migrations.resolve("V002__create_ledger.up.sql")
        Files.writeString(up, Files.readString(up) + "\nCREATE TABLE public.unapproved (opaque bytea);\n")
        for (arguments in listOf(
            arrayOf("validate"),
            arrayOf("status"),
            arrayOf("migrate"),
            arrayOf("migrate", "--dry-run"),
            arrayOf("migrate-down", "--target", "0"),
            arrayOf("migrate-down", "--target", "0", "--dry-run"),
            arrayOf("release-lock", "--host", "synthetic-host", "--pid", "1"),
        )) {
            val refused = command(database, *arguments, migrations = migrations)
            assertEquals(1, refused.exit)
            assertEquals("PROVIDER_DENIED", refused.event("migration_admission_refused").admissionString("code"))
            noBootstrap(database)
        }
    }

    private fun copyInstallation(target: Path) {
        for (name in listOf(
            "src/main/resources/database-admission-installation.json",
            "scripts/prepare_database_admission.py",
            "scripts/materialize_money_sources.py",
            "build/database-admission/scripts/database_admission.py",
            "build/database-admission/scripts/database_sql.py",
            "build/database-admission/scripts/database_baseline.py",
            "build/database-admission/database/admission-trust.json",
            "build/database-admission/inputs.json",
        )) {
            val output = target.resolve(name)
            Files.createDirectories(output.parent)
            Files.copy(root.resolve(name), output)
        }
    }

    @Test
    fun `packaged compiled authority rejects substituted launcher resource and payloads before execution`() {
        for ((name, code) in listOf(
            "scripts/prepare_database_admission.py" to "SOURCE_CHANGED",
            "src/main/resources/database-admission-installation.json" to "SOURCE_CHANGED",
            "build/database-admission/inputs.json" to "SOURCE_CHANGED",
            "build/database-admission/scripts/database_sql.py" to "SOURCE_CHANGED",
            "scripts/materialize_money_sources.py" to "OUTPUT_INVALID",
        )) {
            val database = TestDatabase.fresh()
            val installation = directory.resolve("installation-$invocation")
            copyInstallation(installation)
            val replacement =
                if (name.endsWith(".py")) {
                    "from pathlib import Path\nPath('untrusted-launcher-ran').write_text('unexpected')\n"
                } else {
                    "substituted"
                }
            Files.writeString(installation.resolve(name), replacement)
            val refused = command(database, "migrate", workingDirectory = installation)
            assertEquals(1, refused.exit)
            assertEquals(code, refused.event("migration_admission_refused").admissionString("code"))
            assertFalse(Files.exists(installation.resolve("untrusted-launcher-ran")))
            noBootstrap(database)
        }
    }

    @Test
    fun `packaged missing launcher and additional payload cannot trigger preparation or bootstrap`() {
        for (missing in listOf(true, false)) {
            val database = TestDatabase.fresh()
            val installation = directory.resolve("installation-$missing")
            copyInstallation(installation)
            if (missing) {
                Files.delete(installation.resolve("scripts/prepare_database_admission.py"))
            } else {
                Files.writeString(installation.resolve("build/database-admission/scripts/additional.py"), "unapproved")
            }
            val refused = command(database, "migrate-down", "--target", "0", workingDirectory = installation)
            assertEquals(1, refused.exit)
            assertEquals(
                if (missing) "SOURCE_UNAVAILABLE" else "SOURCE_INVALID",
                refused.event("migration_admission_refused").admissionString("code"),
            )
            noBootstrap(database)
        }
    }

    private fun state(connection: Connection): List<String> {
        val strings =
            connection.createStatement().use { statement ->
                statement.executeQuery("SHOW standard_conforming_strings").use { rows ->
                    check(rows.next())
                    rows.getString(1)
                }
            }
        return listOf(
            connection.autoCommit.toString(),
            connection.transactionIsolation.toString(),
            connection.isReadOnly.toString(),
            connection.unwrap(BaseConnection::class.java).transactionState.name,
            strings,
        )
    }

    @Test
    fun `real admitted buffers preserve session preconditions and deny changed unselected reverse`() {
        val database = TestDatabase.fresh()
        val files = copyMigrations(directory.resolve("buffers"))
        val set = MigrationSet.load(files)
        database.connect().use { connection ->
            val runner =
                MigrationRunner(connection, set, Identity("real@junit", "synthetic-host", 1), Duration.ofSeconds(60)) {}
            connection.createStatement().use { it.execute("SET standard_conforming_strings = off") }
            val before = state(connection)
            assertEquals(0, runner.migrate(1, false, true))
            assertEquals(0, runner.migrateDown(0, true))
            assertEquals(before, state(connection))
            noBootstrap(database)

            connection.autoCommit = false
            val transaction = state(connection)
            assertThrows(UsageError::class.java) { runner.migrate(null, false, false) }
            assertEquals(transaction, state(connection))
            connection.rollback()
            connection.autoCommit = true

            val reverse =
                files.resolve(
                    set.migrations
                        .last()
                        .reversal.file,
                )
            Files.writeString(reverse, Files.readString(reverse) + "\nSELECT 1;\n")
            assertEquals(
                "MIGRATIONS_CHANGED",
                assertThrows(AdmissionRefused::class.java) { runner.migrate(1, false, false) }.code,
            )
            assertEquals(before, state(connection))
            noBootstrap(database)
        }
    }
}
