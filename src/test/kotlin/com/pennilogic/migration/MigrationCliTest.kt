package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.postgresql.util.PSQLException
import org.postgresql.util.ServerErrorMessage
import java.nio.file.Path
import java.sql.SQLException
import java.time.Duration

class MigrationCliTest {
    @TempDir
    lateinit var directory: Path

    private fun parseError(vararg args: String): String =
        assertThrows(UsageError::class.java) { MigrationCli.parse(args.toList()) }.message!!

    @Test
    fun `parses every option`() {
        val options =
            MigrationCli.parse(
                listOf(
                    "migrate",
                    "--migrations",
                    "db",
                    "--holder",
                    "api#55@ci",
                    "--target",
                    "3",
                    "--include-contract",
                    "--dry-run",
                    "--status-file",
                    "status.json",
                    "--slow-threshold-seconds",
                    "5",
                ),
            )
        assertEquals(Command.MIGRATE, options.command)
        assertEquals(Path.of("db"), options.migrations)
        assertEquals("api#55@ci", options.holder)
        assertEquals(3, options.target)
        assertTrue(options.includeContract)
        assertTrue(options.dryRun)
        assertEquals(Path.of("status.json"), options.statusFile)
        assertEquals(Duration.ofSeconds(5), options.slowThreshold)
        val defaults = MigrationCli.parse(listOf("status"))
        assertEquals(Path.of("src", "main", "resources", "db", "migrations"), defaults.migrations)
        assertEquals("read-only", defaults.holder)
        assertNull(defaults.target)
        assertFalse(defaults.includeContract)
        assertFalse(defaults.dryRun)
        assertNull(defaults.statusFile)
        assertEquals(Duration.ofSeconds(60), defaults.slowThreshold)
    }

    @Test
    fun `rejects malformed invocations before touching anything`() {
        assertTrue(parseError().startsWith("expected a command"))
        assertTrue(parseError("deploy").startsWith("expected a command"))
        assertEquals("unknown option --force", parseError("status", "--force"))
        assertEquals("--target requires a value", parseError("status", "--target"))
        assertEquals("--target requires a non-negative integer version", parseError("status", "--target", "-1"))
        assertEquals("--slow-threshold-seconds requires a positive integer", parseError("status", "--slow-threshold-seconds", "0"))
        assertTrue(parseError("status", "--holder", "bad holder").startsWith("--holder must be"))
        assertTrue(parseError("migrate").startsWith("--holder is required for migrate"))
        assertTrue(parseError("migrate-down").startsWith("--holder is required for migrate-down"))
        assertTrue(parseError("release-lock").startsWith("--holder is required for release-lock"))
        assertTrue(parseError("release-lock", "--holder", "x").startsWith("release-lock requires --holder, --host and --pid"))
        assertTrue(
            parseError("release-lock", "--holder", "x", "--host", "h").startsWith("release-lock requires --holder, --host and --pid"),
        )
        assertTrue(parseError("release-lock", "--holder", "x", "--pid", "1").startsWith("release-lock requires --holder, --host and --pid"))
        assertEquals("--pid requires a non-negative integer", parseError("release-lock", "--holder", "x", "--host", "h", "--pid", "-1"))
        assertEquals("--host and --pid are only valid for release-lock", parseError("status", "--host", "h"))
        assertEquals("--host and --pid are only valid for release-lock", parseError("migrate", "--holder", "x", "--pid", "1"))
        val release = MigrationCli.parse(listOf("release-lock", "--holder", "ghost@host", "--host", "gone", "--pid", "99"))
        assertEquals(Identity("ghost@host", "gone", 99), release.releaseClaim)
        assertNull(MigrationCli.parse(listOf("status")).releaseClaim)
        assertEquals("read-only", MigrationCli.parse(listOf("migrate", "--dry-run")).holder)
        assertEquals("read-only", MigrationCli.parse(listOf("validate")).holder)
    }

    @Test
    fun `usage errors exit with code 2 and print the synopsis`() {
        val output = Output()
        assertEquals(2, MigrationCli(output.stream, emptyMap()).run(listOf("migrate")))
        val event = output.require("usage_error")
        assertTrue(event.contains("--holder is required"))
        assertTrue(event.contains(MigrationCli.USAGE))
    }

    @Test
    fun `validate reports the machine readable set and names convention failures`() {
        Fixtures.threePhaseSet(directory)
        val output = Output()
        assertEquals(0, MigrationCli(output.stream, emptyMap()).run(listOf("validate", "--migrations", directory.toString())))
        val set = output.require("migration_set")
        assertTrue(set.contains("\"count\":3"))
        assertTrue(set.contains("\"id\":\"V003__drop_legacy\""))
        Fixtures.write(directory, "V004__broken", Fixtures.header(phase = "later") + "SELECT 1;\n")
        val failing = Output()
        assertEquals(1, MigrationCli(failing.stream, emptyMap()).run(listOf("validate", "--migrations", directory.toString())))
        assertEquals(
            """{"event":"migration_validation_failed","file":"V004__broken.up.sql","rule":"phase must be expand, migrate or contract"}""",
            failing.event("migration_validation_failed"),
        )
    }

    @Test
    fun `database commands require the connection environment and a postgres url`() {
        Fixtures.threePhaseSet(directory)
        val args = listOf("status", "--migrations", directory.toString())
        val missingUrl = Output()
        assertEquals(2, MigrationCli(missingUrl.stream, emptyMap()).run(args))
        assertTrue(missingUrl.event("usage_error")!!.contains("MIGRATION_JDBC_URL is required"))
        val wrongDriver = Output()
        assertEquals(2, MigrationCli(wrongDriver.stream, mapOf("MIGRATION_JDBC_URL" to "jdbc:h2:mem:x")).run(args))
        assertTrue(wrongDriver.event("usage_error")!!.contains("must be a jdbc:postgresql: URL"))
        val missingUser = Output()
        assertEquals(2, MigrationCli(missingUser.stream, mapOf("MIGRATION_JDBC_URL" to "jdbc:postgresql://127.0.0.1:1/x")).run(args))
        assertTrue(missingUser.event("usage_error")!!.contains("MIGRATION_DB_USER is required"))
        val missingPassword = Output()
        val environment = mapOf("MIGRATION_JDBC_URL" to "jdbc:postgresql://127.0.0.1:1/x", "MIGRATION_DB_USER" to "migration")
        assertEquals(2, MigrationCli(missingPassword.stream, environment).run(args))
        assertTrue(missingPassword.event("usage_error")!!.contains("MIGRATION_DB_PASSWORD is required"))
    }

    @Test
    fun `an unreachable database is reported by code without the driver message`() {
        Fixtures.threePhaseSet(directory)
        val output = Output()
        val environment =
            mapOf(
                "MIGRATION_JDBC_URL" to "jdbc:postgresql://127.0.0.1:1/x?connectTimeout=2",
                "MIGRATION_DB_USER" to "migration",
                "MIGRATION_DB_PASSWORD" to "not-a-real-secret",
            )
        assertEquals(1, MigrationCli(output.stream, environment).run(listOf("status", "--migrations", directory.toString())))
        val event = output.require("database_error")
        assertTrue(event.contains("\"type\":\"org.postgresql.util.PSQLException\""))
        assertTrue(event.contains("\"sqlState\":\"08001\""))
        assertFalse(output.toString().contains("not-a-real-secret"))
        assertFalse(output.toString().contains("refused"))
    }

    @Test
    fun `failure summaries keep identifiers and drop the server text`() {
        val raw =
            "SERROR\u0000C23505\u0000Mduplicate key value violates unique constraint \"t2_pkey\"" +
                "\u0000DKey (id)=(1) already exists.\u0000spublic\u0000tt2\u0000nt2_pkey\u0000cid\u0000P17\u0000"
        val summary = FailureSummary.of(PSQLException(ServerErrorMessage(raw)))
        assertEquals(FailureSummary("23505", "public", "t2", "t2_pkey", "id", 17), summary)
        assertFalse(Json.encode(summary.fields()).contains("(1)"))
        val positionless = FailureSummary.of(PSQLException(ServerErrorMessage("SERROR\u0000C42P01\u0000Mrelation missing\u0000")))
        assertEquals(FailureSummary("42P01", null, null, null, null, null), positionless)
        assertEquals(FailureSummary("08006", null, null, null, null, null), FailureSummary.of(SQLException("boom", "08006")))
    }
}
