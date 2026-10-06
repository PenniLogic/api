package com.pennilogic.migration

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.postgresql.core.BaseConnection
import org.postgresql.core.TransactionState
import java.nio.file.Files
import java.nio.file.Path
import java.sql.Connection
import java.time.Duration

@Tag("postgres")
class MigrationAdmissionPostgresTest {
    @TempDir
    lateinit var directory: Path

    private val identity = Identity("admission@junit", "synthetic-host", 1)

    private fun runner(
        connection: Connection,
        set: MigrationSet,
        emit: (String) -> Unit = {},
    ): MigrationRunner = MigrationRunner(connection, set, identity, Duration.ofSeconds(60), emit)

    private fun controlledSet(
        path: Path,
        deny: (String, JsonArray) -> Boolean,
    ): MigrationSet {
        Fixtures.threePhaseSet(path)
        val admission =
            DatabaseAdmission(AdmissionFixtures.policy, AdmissionFixtures.inventory) { command, request ->
                val selection =
                    AdmissionJson
                        .parse(request, DatabaseAdmission.REQUEST_LIMIT)
                        .jsonObject
                        .getValue("selection")
                        .jsonArray
                if (deny(command, selection)) AdmissionFixtures.deny(command, request) else AdmissionFixtures.accept(command, request)
            }
        return MigrationSet.load(path) { admission }
    }

    private data class ConnectionState(
        val autoCommit: Boolean,
        val isolation: Int,
        val readOnly: Boolean,
        val transaction: TransactionState,
        val standardStrings: String,
    )

    private fun state(connection: Connection): ConnectionState {
        val standardStrings =
            connection.createStatement().use { statement ->
                statement.executeQuery("SHOW standard_conforming_strings").use { rows ->
                    rows.next()
                    rows.getString(1)
                }
            }
        return ConnectionState(
            connection.autoCommit,
            connection.transactionIsolation,
            connection.isReadOnly,
            connection.unwrap(BaseConnection::class.java).transactionState,
            standardStrings,
        )
    }

    @BeforeEach
    fun requirePostgres() {
        assumeTrue(TestDatabase.skipReason == null, TestDatabase.skipReason)
    }

    @Test
    fun `an unapproved binary column is refused before even creating the registry`() {
        val database = TestDatabase.fresh()
        Fixtures.write(
            directory,
            "V001__private_vectors",
            Fixtures.header() + "CREATE TABLE public.private_vectors (payload bytea);\n",
            "DROP TABLE public.private_vectors;\n",
        )
        val output = Output()
        val result =
            MigrationCli(output.stream, database.environment()).run(
                listOf("migrate", "--holder", "admission@junit", "--migrations", directory.toString()),
            )
        val registry = database.scalar("SELECT to_regnamespace('migration_runner')")
        val table = database.scalar("SELECT to_regclass('public.private_vectors')")
        println(
            Json.event(
                "admission_before_bootstrap",
                "exit" to result,
                "registryPresent" to (registry != null),
                "tablePresent" to (table != null),
            ),
        )
        assertEquals(1, result, "unaccepted migration must be refused")
        assertNull(registry, "denial must precede bootstrap")
        assertNull(table, "denial must precede application SQL")
        output.require("migration_admission_refused")
    }

    @Test
    fun `selected plan and pre-apply denials precede all bootstrap and session writes`() {
        for (command in listOf("plan", "admit-apply")) {
            for (dryRun in listOf(false, true)) {
                val database = TestDatabase.fresh()
                val set =
                    controlledSet(directory.resolve("$command-$dryRun")) { stage, selection ->
                        stage == command && selection.isNotEmpty()
                    }
                database.connect().use { connection ->
                    connection.createStatement().use { it.execute("SET standard_conforming_strings = off") }
                    val before = state(connection)
                    assertThrows(AdmissionRefused::class.java) { runner(connection, set).migrate(null, false, dryRun) }
                    assertEquals(before, state(connection))
                    assertNull(database.scalar("SELECT to_regnamespace('migration_runner')"))
                    assertNull(database.scalar("SELECT to_regclass('public.widgets')"))
                }
            }
        }
    }

    @Test
    fun `admitted dry run changes neither registry nor connection settings`() {
        val database = TestDatabase.fresh()
        val set = controlledSet(directory) { _, _ -> false }
        database.connect().use { connection ->
            connection.createStatement().use { it.execute("SET standard_conforming_strings = off") }
            val before = state(connection)
            assertEquals(0, runner(connection, set).migrate(null, false, true))
            assertEquals(0, runner(connection, set).migrateDown(null, true))
            assertEquals(before, state(connection))
            assertNull(database.scalar("SELECT to_regnamespace('migration_runner')"))
            assertNull(database.scalar("SELECT to_regclass('public.widgets')"))
        }
    }

    @Test
    fun `no-op and operator release denial preserve an existing foreign lock and every registry row`() {
        val database = TestDatabase.fresh()
        var denied = false
        val set = controlledSet(directory) { stage, _ -> denied && stage == "admit-apply" }
        database.connect().use { connection ->
            val runner = runner(connection, set)
            assertEquals(0, runner.migrate(2, false, false))
            val registry = Registry(connection)
            val foreign = Identity("other@junit", "other-host", 42)
            registry.claim(foreign, "migrate", 3)
            val beforeRows = registry.rows()
            val beforeLock = registry.currentLock()
            connection.createStatement().use { it.execute("SET standard_conforming_strings = off") }
            val beforeConnection = state(connection)
            denied = true
            for (operation in listOf<() -> Any>(
                { runner.migrate(2, false, false) },
                { runner.migrateDown(2, false) },
                { runner.releaseLock(foreign) },
            )) {
                assertThrows(AdmissionRefused::class.java) { operation() }
                assertEquals(beforeRows, registry.rows())
                assertEquals(beforeLock, registry.currentLock())
                assertEquals(beforeConnection, state(connection))
            }
        }
    }

    @Test
    fun `denied all-held and no-op paths cannot repair a missing singleton`() {
        val database = TestDatabase.fresh()
        var denied = false
        val set =
            controlledSet(directory) { stage, selection ->
                if (denied) assertEquals(JsonArray(emptyList()), selection)
                denied && stage == "admit-apply"
            }
        database.connect().use { connection ->
            val runner = runner(connection, set)
            assertEquals(0, runner.migrate(2, false, false))
            val beforeRows = Registry(connection).rows()
            database.execute("DELETE FROM migration_runner.migration_lock")
            denied = true
            for (operation in listOf<() -> Any>(
                { runner.migrate(null, false, false) },
                { runner.migrate(null, false, true) },
                { runner.migrateDown(2, false) },
                { runner.releaseLock(identity) },
            )) {
                assertThrows(AdmissionRefused::class.java) { operation() }
                assertEquals(0L, database.count("SELECT count(*) FROM migration_runner.migration_lock"))
                assertEquals(beforeRows, Registry(connection).rows())
                assertEquals(
                    "migration_lock singleton row is missing",
                    assertThrows(IllegalStateException::class.java) { runner.snapshot() }.message,
                )
            }
            denied = false
            assertEquals(0, runner.migrate(null, false, false))
            assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock"))
            assertEquals(beforeRows, Registry(connection).rows())
            assertNull(Registry(connection).currentLock())
        }
    }

    @Test
    fun `ordinary down and compensating denials retain effective version and physical schema`() {
        val database = TestDatabase.fresh()
        var blockedDirection: String? = null
        val selections = mutableListOf<List<String>>()
        val set =
            controlledSet(directory) { stage, selection ->
                val directions = selection.map { it.jsonObject.admissionString("direction") }
                if (stage == "admit-apply") selections += directions
                stage == "admit-apply" && blockedDirection in directions
            }
        database.connect().use { connection ->
            val runner = runner(connection, set)
            assertEquals(0, runner.migrate(null, true, false))
            val afterUp = Registry(connection).rows()
            blockedDirection = "compensating"
            assertThrows(AdmissionRefused::class.java) { runner.migrateDown(0, false) }
            assertEquals(listOf("compensating", "down", "down"), selections.last())
            assertEquals(3, runner.snapshot().currentVersion)
            assertEquals(afterUp, Registry(connection).rows())
            assertEquals(
                0L,
                database.count("SELECT count(*) FROM information_schema.columns WHERE table_name = 'widgets' AND column_name = 'legacy'"),
            )
            blockedDirection = null
            assertEquals(0, runner.migrateDown(2, false))
            val afterCompensation = Registry(connection).rows()
            blockedDirection = "down"
            for (dryRun in listOf(false, true)) {
                assertThrows(AdmissionRefused::class.java) { runner.migrateDown(0, dryRun) }
                assertEquals(listOf("down", "down"), selections.last())
                assertEquals(2, runner.snapshot().currentVersion)
                assertEquals(afterCompensation, Registry(connection).rows())
                assertNull(Registry(connection).currentLock())
            }
        }
    }

    @Test
    fun `changes to any direction including unselected compensation block a fresh runner`() {
        for (file in listOf(
            "V001__create_widgets.up.sql",
            "V001__create_widgets.down.sql",
            "V003__drop_legacy.compensating.sql",
        )) {
            val database = TestDatabase.fresh()
            val path = directory.resolve(file)
            val set = controlledSet(path) { _, _ -> false }
            Files.writeString(path.resolve(file), Files.readString(path.resolve(file)) + "\nSELECT 'changed';\n")
            database.connect().use { connection ->
                assertEquals(
                    "MIGRATIONS_CHANGED",
                    assertThrows(AdmissionRefused::class.java) { runner(connection, set).migrate(1, false, false) }.code,
                )
                assertNull(database.scalar("SELECT to_regnamespace('migration_runner')"))
                assertNull(database.scalar("SELECT to_regclass('public.widgets')"))
            }
        }
    }

    @Test
    fun `post-admission file replacement by plan observer never reaches SQL execution`() {
        val database = TestDatabase.fresh()
        val set = controlledSet(directory) { _, _ -> false }
        database.connect().use { connection ->
            val runner =
                runner(connection, set) { event ->
                    if (event.contains("\"event\":\"migration_plan\"")) {
                        Files.writeString(
                            directory.resolve(set.migrations.first().file),
                            Fixtures.header() + "CREATE TABLE substituted(id int);\n",
                        )
                    }
                }
            assertThrows(AdmissionRefused::class.java) { runner.migrate(null, false, false) }
            assertNull(database.scalar("SELECT to_regnamespace('migration_runner')"))
            assertNull(database.scalar("SELECT to_regclass('public.widgets')"))
            assertNull(database.scalar("SELECT to_regclass('public.substituted')"))
        }
    }

    @Test
    fun `caller transactions are refused without rollback commit or bootstrap`() {
        val database = TestDatabase.fresh()
        val set = controlledSet(directory) { _, _ -> false }
        database.connect().use { connection ->
            val runner = runner(connection, set)
            connection.autoCommit = false
            assertThrows(UsageError::class.java) { runner.migrate(null, false, false) }
            assertFalse(connection.autoCommit)
            connection.rollback()
            connection.autoCommit = true
            connection.createStatement().use { it.execute("BEGIN") }
            assertEquals(TransactionState.OPEN, connection.unwrap(BaseConnection::class.java).transactionState)
            assertThrows(UsageError::class.java) { runner.migrateDown(null, false) }
            assertThrows(UsageError::class.java) { runner.releaseLock(identity) }
            assertTrue(connection.autoCommit)
            assertEquals(TransactionState.OPEN, connection.unwrap(BaseConnection::class.java).transactionState)
            connection.createStatement().use { it.execute("ROLLBACK") }
            assertNull(database.scalar("SELECT to_regnamespace('migration_runner')"))
        }
    }
}
