package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Files
import java.nio.file.Path

class MigrationSetTest {
    @TempDir
    lateinit var directory: Path

    private fun violation(): ConventionViolation = assertThrows(ConventionViolation::class.java) { MigrationSet.load(directory) }

    private fun assertViolation(
        file: String,
        rule: String,
    ) {
        val error = violation()
        assertEquals(file, error.file, error.message)
        assertTrue(error.rule.contains(rule), error.message)
        assertEquals("$file: ${error.rule}", error.message)
    }

    @Test
    fun `loads the shipped migrations with checksums over the whole script`() {
        val set = MigrationSet.load(Path.of("src", "main", "resources", "db", "migrations"))
        assertEquals(1, set.latestVersion)
        val baseline = set.migrations.single()
        assertEquals("V001__create_pennilogic_schema", baseline.id)
        assertEquals("create_pennilogic_schema", baseline.name)
        assertEquals(Phase.EXPAND, baseline.phase)
        assertEquals("api#55", baseline.owner)
        assertEquals(ReversalKind.DOWN, baseline.reversal.kind)
        assertNull(baseline.expandVersion)
        assertEquals(Checksum.of(baseline.sql), baseline.checksum)
        assertEquals(baseline, set.byVersion(1))
        assertNull(set.byVersion(2))
        assertEquals(listOf(baseline.record()), set.records())
        assertEquals("V001", MigrationSet.label(1))
        assertEquals("V1234", MigrationSet.label(1234))
    }

    @Test
    fun `three phase set resolves expand references and compensating evidence`() {
        Fixtures.threePhaseSet(directory)
        val set = MigrationSet.load(directory)
        assertEquals(listOf(1, 2, 3), set.migrations.map { it.version })
        assertEquals(listOf(null, 1, 1), set.migrations.map { it.expandVersion })
        val contract = set.byVersion(3)!!
        assertEquals(Phase.CONTRACT, contract.phase)
        assertEquals(ReversalKind.COMPENSATING, contract.reversal.kind)
        assertEquals("V003__drop_legacy.compensating.sql", contract.reversal.file)
        assertEquals("the dropped legacy values cannot be restored", contract.reversal.reason)
        assertEquals("compensating", contract.record()["reversal"].let { (it as Map<*, *>)["kind"] })
    }

    @Test
    fun `checksum ignores platform line endings but not content`() {
        assertEquals(Checksum.of("a\nb\n"), Checksum.of("a\r\nb\r\n"))
        assertTrue(Checksum.of("a\nb\n") != Checksum.of("a\nb"))
        assertEquals(64, Checksum.of("").length)
    }

    @Test
    fun `missing directory is a violation naming the directory`() {
        val missing = directory.resolve("absent")
        val error = assertThrows(ConventionViolation::class.java) { MigrationSet.load(missing) }
        assertEquals(missing.toString(), error.file)
    }

    @Test
    fun `empty directory is an empty set`() {
        assertEquals(0, MigrationSet.load(directory).latestVersion)
    }

    @Test
    fun `file names must follow the pattern`() {
        Files.writeString(directory.resolve("V001__Create.up.sql"), Fixtures.header() + "SELECT 1;\n")
        assertViolation("V001__Create.up.sql", "file name must match")
    }

    @Test
    fun `version zero and out of range versions are rejected`() {
        Fixtures.write(directory, "V000__zero", Fixtures.header() + "SELECT 1;\n")
        assertViolation("V000__zero.down.sql", "version must be at least V001")
        Files.list(directory).use { stream -> stream.forEach { Files.delete(it) } }
        Fixtures.write(directory, "V99999999999__huge", Fixtures.header() + "SELECT 1;\n")
        assertViolation("V99999999999__huge.down.sql", "out of range")
    }

    @Test
    fun `files of one version must agree on the name`() {
        Files.writeString(directory.resolve("V001__alpha.up.sql"), Fixtures.header() + "SELECT 1;\n")
        Files.writeString(directory.resolve("V001__beta.down.sql"), "SELECT 1;\n")
        assertViolation("V001__beta.down.sql", "disagree on the name: V001__alpha")
    }

    @Test
    fun `versions must be contiguous from V001`() {
        Fixtures.write(directory, "V001__one", Fixtures.header() + "SELECT 1;\n")
        Fixtures.write(directory, "V003__three", Fixtures.header() + "SELECT 1;\n")
        assertViolation("V003__three.down.sql", "V003 follows V001")
    }

    @Test
    fun `an up script is required`() {
        Files.writeString(directory.resolve("V001__only_down.down.sql"), "SELECT 1;\n")
        assertViolation("V001__only_down.down.sql", "has no up script")
    }

    @Test
    fun `exactly one reversal script is required`() {
        Fixtures.write(directory, "V001__both", Fixtures.header() + "SELECT 1;\n", compensating = "SELECT 1;\n")
        assertViolation("V001__both.compensating.sql", "exactly one reversal script")
        Files.delete(directory.resolve("V001__both.compensating.sql"))
        Files.delete(directory.resolve("V001__both.down.sql"))
        assertViolation("V001__both.up.sql", "reversal evidence is required")
    }

    @Test
    fun `header directives are validated`() {
        Fixtures.write(directory, "V001__x", Fixtures.header(phase = "shrink") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "phase must be expand, migrate or contract")
        Fixtures.write(directory, "V001__x", Fixtures.header(reversal = "undo") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "reversal must be down or compensating")
        Fixtures.write(directory, "V001__x", Fixtures.header(reversal = "compensating") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "declares reversal compensating but the reversal script is V001__x.down.sql")
        Fixtures.write(directory, "V001__x", Fixtures.header(reason = "why") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "reason is only valid for a compensating reversal")
        Fixtures.write(directory, "V001__x", "-- phase: expand\n-- owner: api#55\n-- reversal: down\n-- phase: expand\nSELECT 1;\n")
        assertViolation("V001__x.up.sql", "duplicate header directive 'phase'")
        Fixtures.write(directory, "V001__x", "-- phase: expand\n-- owner:\n-- reversal: down\nSELECT 1;\n")
        assertViolation("V001__x.up.sql", "header directive 'owner' has no value")
        Fixtures.write(directory, "V001__x", "-- phase: expand\n-- reversal: down\n-- ticket: api#55\nSELECT 1;\n")
        assertViolation("V001__x.up.sql", "unknown header directive 'ticket'")
        Fixtures.write(directory, "V001__x", "-- phase: expand\n-- reversal: down\n-- Plain comment: allowed\nSELECT 1;\n")
        assertViolation("V001__x.up.sql", "missing header directive 'owner'")
        Fixtures.write(directory, "V001__x", "SELECT 1;\n-- phase: expand\n-- owner: api#55\n-- reversal: down\n")
        assertViolation("V001__x.up.sql", "missing header directive 'phase'")
    }

    @Test
    fun `compensating reversal needs a reason`() {
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header(reversal = "compensating") + "SELECT 1;\n",
            down = null,
            compensating = "SELECT 1;\n",
        )
        assertViolation("V001__x.up.sql", "must state why the exact reverse is impossible")
    }

    @Test
    fun `scripts must contain statements`() {
        Fixtures.write(directory, "V001__x", Fixtures.header() + "-- nothing here\n")
        assertViolation("V001__x.up.sql", "script has no SQL statements")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n", down = "-- nothing to undo\n\n")
        assertViolation("V001__x.down.sql", "reversal script has no SQL statements")
    }

    @Test
    fun `block comments are not statements`() {
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n", down = "/* nothing to undo */\n")
        assertViolation("V001__x.down.sql", "reversal script has no SQL statements")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n", down = "/* multi\n   line\n   placeholder */\n")
        assertViolation("V001__x.down.sql", "reversal script has no SQL statements")
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header() + "SELECT 1;\n",
            down = "/* outer /* nested */ still a comment */\n-- and a line\n",
        )
        assertViolation("V001__x.down.sql", "reversal script has no SQL statements")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "/* forward change described here */\n")
        assertViolation("V001__x.up.sql", "script has no SQL statements")
        Files.delete(directory.resolve("V001__x.down.sql"))
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header(reversal = "compensating", reason = "why") + "SELECT 1;\n",
            down = null,
            compensating = "/* TODO */\n",
        )
        assertViolation("V001__x.compensating.sql", "reversal script has no SQL statements")
        Files.delete(directory.resolve("V001__x.compensating.sql"))
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header() + "/* create */ CREATE TABLE t (id integer); -- done\n",
            down = "/* drop */\nDROP TABLE t;\n",
        )
        assertEquals(1, MigrationSet.load(directory).latestVersion)
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n", down = "SELECT '/* not a comment */';\n")
        assertEquals(1, MigrationSet.load(directory).latestVersion)
    }

    @Test
    fun `reversal scripts must not drop or truncate with cascade`() {
        Fixtures.write(directory, "V001__x", Fixtures.header() + "CREATE SCHEMA s;\n", down = "DROP SCHEMA s CASCADE;\n")
        assertViolation("V001__x.down.sql", "must not DROP or TRUNCATE with CASCADE")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "CREATE TABLE t (id integer);\n", down = "drop table t\n  cascade;\n")
        assertViolation("V001__x.down.sql", "must not DROP or TRUNCATE with CASCADE")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "CREATE TABLE t (id integer);\n", down = "TRUNCATE t, u CASCADE;\n")
        assertViolation("V001__x.down.sql", "must not DROP or TRUNCATE with CASCADE")
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header() + "CREATE TABLE t (id integer);\n",
            down = "ALTER TABLE t DROP COLUMN c CASCADE;\n",
        )
        assertViolation("V001__x.down.sql", "must not DROP or TRUNCATE with CASCADE")
        Files.delete(directory.resolve("V001__x.down.sql"))
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header(reversal = "compensating", reason = "why") + "SELECT 1;\n",
            down = null,
            compensating = "DROP TYPE mood CASCADE;\n",
        )
        assertViolation("V001__x.compensating.sql", "must not DROP or TRUNCATE with CASCADE")
        Files.delete(directory.resolve("V001__x.compensating.sql"))
        // A CASCADE that only appears in a comment or as a referential action is not a destructive cascade.
        Fixtures.write(
            directory,
            "V001__x",
            Fixtures.header() + "CREATE TABLE t (id integer);\n",
            down =
                "-- never DROP ... CASCADE here\n" +
                    "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (id) REFERENCES p (id) ON DELETE CASCADE;\nDROP TABLE t;\n",
        )
        assertEquals(1, MigrationSet.load(directory).latestVersion)
        Fixtures.write(directory, "V001__x", Fixtures.header() + "DROP TABLE old CASCADE;\n", down = "SELECT 1;\n")
        assertEquals(1, MigrationSet.load(directory).latestVersion)
        // An E'…' escape string cannot hide the cascade: Postgres closes E'\'' after one character, and so does the lint.
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n", down = "SELECT E'\\'';\nDROP TABLE t CASCADE;\n")
        assertViolation("V001__x.down.sql", "must not DROP or TRUNCATE with CASCADE")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n", down = "DROP TABLE t;\nSELECT E'it\\'s fine';\n")
        assertEquals(1, MigrationSet.load(directory).latestVersion)
    }

    @Test
    fun `expand references are validated per phase`() {
        Fixtures.write(directory, "V001__x", Fixtures.header(phase = "contract") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "a contract migration must name the expand migration")
        Fixtures.write(directory, "V001__x", Fixtures.header(expand = "V001") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "only valid for migrate and contract migrations")
        Fixtures.write(directory, "V001__x", Fixtures.header(phase = "migrate", expand = "one") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "expand must name a version like V001")
        Fixtures.write(directory, "V001__x", Fixtures.header(phase = "migrate", expand = "V001") + "SELECT 1;\n")
        assertViolation("V001__x.up.sql", "expand names V001, which is not an earlier migration of this set")
        Fixtures.write(directory, "V001__x", Fixtures.header(phase = "migrate") + "SELECT 1;\n")
        Fixtures.write(directory, "V002__y", Fixtures.header(phase = "contract", expand = "V001") + "SELECT 1;\n")
        assertViolation("V002__y.up.sql", "expand names V001, whose phase is migrate rather than expand")
        Fixtures.write(directory, "V002__y", Fixtures.header(phase = "contract", expand = "V99999999999") + "SELECT 1;\n")
        assertViolation("V002__y.up.sql", "expand must name a version like V001")
        Fixtures.write(directory, "V001__x", Fixtures.header() + "SELECT 1;\n")
        Fixtures.write(directory, "V002__y", Fixtures.header() + "SELECT 1;\n")
        Fixtures.write(directory, "V003__z", Fixtures.header(phase = "contract", expand = "V002") + "SELECT 1;\n")
        assertEquals(listOf(null, null, 2), MigrationSet.load(directory).migrations.map { it.expandVersion })
    }
}
