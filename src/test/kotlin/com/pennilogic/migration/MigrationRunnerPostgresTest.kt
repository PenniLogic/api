package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@Tag("postgres")
class MigrationRunnerPostgresTest {
    @TempDir
    lateinit var directory: Path

    private val shipped = Path.of("src", "main", "resources", "db", "migrations")

    @BeforeEach
    fun requirePostgres() {
        assumeTrue(TestDatabase.skipReason == null, TestDatabase.skipReason)
    }

    private fun cli(
        database: TestDatabase,
        output: Output,
        vararg args: String,
    ): Int = MigrationCli(output.stream, database.environment()).run(args.toList())

    private fun migrate(
        database: TestDatabase,
        migrations: Path,
        vararg extra: String,
    ): Output {
        val output = Output()
        assertEquals(
            0,
            cli(database, output, "migrate", "--holder", "test@junit", "--migrations", migrations.toString(), *extra),
            output.toString(),
        )
        return output
    }

    private fun status(
        database: TestDatabase,
        migrations: Path,
    ): String {
        val output = Output()
        assertEquals(0, cli(database, output, "status", "--migrations", migrations.toString()), output.toString())
        return output.require("migration_status")
    }

    private fun currentVersion(status: String): Int = Regex("\"currentVersion\":(\\d+)").find(status)!!.groupValues[1].toInt()

    private fun basicSet(): Path {
        val migrations = directory.resolve("migrations")
        Fixtures.write(migrations, "V001__create_t1", Fixtures.header() + "CREATE TABLE t1 (id integer PRIMARY KEY);\n", "DROP TABLE t1;\n")
        Fixtures.write(migrations, "V002__create_t2", Fixtures.header() + "CREATE TABLE t2 (id integer PRIMARY KEY);\n", "DROP TABLE t2;\n")
        return migrations
    }

    @Test
    fun `shipped migrations round trip on an empty and on a populated database`() {
        val database = TestDatabase.fresh()
        val applied = migrate(database, shipped, "--include-contract")
        applied.require("migration_lock_claimed")
        assertTrue(applied.require("migration_applied").contains("\"id\":\"V001__create_pennilogic_schema\""))
        applied.require("migration_lock_released")
        assertEquals(1L, database.count("SELECT count(*) FROM pg_namespace WHERE nspname = 'pennilogic'"))
        assertEquals(2, currentVersion(status(database, shipped)))

        val emptyReverse = Output()
        assertEquals(
            0,
            cli(database, emptyReverse, "migrate-down", "--holder", "test@junit", "--migrations", shipped.toString(), "--target", "0"),
        )
        assertTrue(emptyReverse.require("migration_reversed").contains("\"reversal\":\"down\""))
        assertEquals(0L, database.count("SELECT count(*) FROM pg_namespace WHERE nspname = 'pennilogic'"))
        val reversedStatus = status(database, shipped)
        assertEquals(0, currentVersion(reversedStatus))
        assertTrue(reversedStatus.contains("\"state\":\"reversed\""))

        migrate(database, shipped, "--include-contract")
        database.execute(
            "CREATE TABLE public.fixture (id integer PRIMARY KEY, note text); INSERT INTO public.fixture VALUES (1, 'kept'), (2, 'kept');",
        )
        val populatedReverse = Output()
        assertEquals(0, cli(database, populatedReverse, "migrate-down", "--holder", "test@junit", "--migrations", shipped.toString()))
        assertEquals(2L, database.count("SELECT count(*) FROM public.fixture"))
        assertEquals(1, currentVersion(status(database, shipped)))
        assertNull(database.scalar("SELECT to_regclass('pennilogic.entries')"))
        assertEquals(1L, database.count("SELECT count(*) FROM pg_namespace WHERE nspname = 'pennilogic'"))
        val completeReverse = Output()
        assertEquals(
            0,
            cli(database, completeReverse, "migrate-down", "--holder", "test@junit", "--migrations", shipped.toString(), "--target", "0"),
        )
        assertEquals(2L, database.count("SELECT count(*) FROM public.fixture"))
        assertEquals(0, currentVersion(status(database, shipped)))
        migrate(database, shipped)
        val finalStatus = status(database, shipped)
        assertEquals(2, currentVersion(finalStatus))
        assertTrue(finalStatus.contains("\"lastApplied\":{\"version\":2,\"id\":\"V002__create_ledger\""))
        assertEquals(10L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock WHERE holder IS NULL"))
        assertRunnerSchemaHoldsOnlyTheRegistry(database)
    }

    @Test
    fun `an unqualified create by the migration role never lands in the runner schema`() {
        val database = TestDatabase.fresh()
        assertEquals("migration", database.user, "the fixture role is named like the old runner schema on purpose")
        Fixtures.write(
            directory,
            "V001__stray",
            Fixtures.header() + "CREATE TABLE stray_unqualified (id integer);\n",
            "DROP TABLE stray_unqualified;\n",
        )
        migrate(database, directory)
        assertEquals(
            "public",
            database.scalar(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = 'stray_unqualified'",
            ),
        )
        assertNull(database.scalar("SELECT to_regnamespace('migration')"))
        assertRunnerSchemaHoldsOnlyTheRegistry(database)
        val down = Output()
        assertEquals(0, cli(database, down, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString()))
        assertRunnerSchemaHoldsOnlyTheRegistry(database)
    }

    private fun assertRunnerSchemaHoldsOnlyTheRegistry(database: TestDatabase) {
        val relations =
            database.query(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace" +
                    " WHERE n.nspname = '${Registry.SCHEMA}' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') ORDER BY c.relname",
            ) { rows ->
                generateSequence { if (rows.next()) rows.getString(1) else null }.toList()
            }
        assertEquals(listOf("migration_lock", "migration_registry", "migration_registry_id_seq"), relations)
        assertEquals("migration_runner", Registry.SCHEMA)
    }

    @Test
    fun `a reversal that would destroy dependent objects fails and keeps the version`() {
        val database = TestDatabase.fresh()
        migrate(database, shipped)
        database.execute("CREATE TABLE pennilogic.ledger_probe (id integer)")
        val output = Output()
        assertEquals(
            1,
            cli(database, output, "migrate-down", "--holder", "test@junit", "--migrations", shipped.toString(), "--target", "0"),
        )
        val failed = output.require("migration_failed")
        assertTrue(failed.contains("\"direction\":\"down\""))
        assertTrue(failed.contains("\"sqlState\":\"2BP01\""))
        assertTrue(failed.contains("\"knownVersion\":1"))
        assertFalse(output.toString().contains("depend"))
        assertEquals(1, currentVersion(status(database, shipped)))
        database.execute("DROP TABLE pennilogic.ledger_probe")
        val retry = Output()
        assertEquals(0, cli(database, retry, "migrate-down", "--holder", "test@junit", "--migrations", shipped.toString()))
        assertEquals(0, currentVersion(status(database, shipped)))
    }

    @Test
    fun `a checksum that changed after apply fails the run and names the file`() {
        val database = TestDatabase.fresh()
        val migrations = basicSet()
        migrate(database, migrations)
        val file = migrations.resolve("V001__create_t1.up.sql")
        val reviewed = Files.readString(file)
        Files.writeString(file, reviewed + "-- edited after apply\n")
        val output = Output()
        assertEquals(1, cli(database, output, "migrate", "--holder", "test@junit", "--migrations", migrations.toString()))
        val problem = output.require("migration_registry_problem")
        assertTrue(problem.contains("\"kind\":\"checksum_drift\""))
        assertTrue(problem.contains("\"file\":\"V001__create_t1.up.sql\""))
        assertTrue(problem.contains("\"applied\":\"") && problem.contains("\"current\":\""))
        val statusOutput = Output()
        assertEquals(1, cli(database, statusOutput, "status", "--migrations", migrations.toString()))
        statusOutput.require("migration_registry_problem")
        assertEquals(2L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        // RECOVERY.md §3: restore the reviewed content and run again.
        Files.writeString(file, reviewed)
        val restored = migrate(database, migrations)
        assertTrue(restored.require("migration_plan").contains("\"steps\":[]"))
        assertEquals(2, currentVersion(status(database, migrations)))
        // The reversal script is pinned at apply time as well: the reverse that runs must be the recorded evidence.
        val down = migrations.resolve("V002__create_t2.down.sql")
        val reviewedDown = Files.readString(down)
        Files.writeString(down, "DROP TABLE t2 CASCADE;\n")
        val cascade = Output()
        assertEquals(1, cli(database, cascade, "migrate-down", "--holder", "test@junit", "--migrations", migrations.toString()))
        assertTrue(cascade.require("migration_validation_failed").contains("must not DROP or TRUNCATE with CASCADE"))
        Files.writeString(down, "DROP TABLE IF EXISTS t2;\n")
        val reversalDrift = Output()
        assertEquals(1, cli(database, reversalDrift, "migrate-down", "--holder", "test@junit", "--migrations", migrations.toString()))
        val drift = reversalDrift.require("migration_registry_problem")
        assertTrue(drift.contains("\"kind\":\"reversal_checksum_drift\""))
        assertTrue(drift.contains("\"file\":\"V002__create_t2.down.sql\""))
        assertNull(reversalDrift.event("migration_lock_claimed"))
        Files.writeString(down, reviewedDown)
        assertEquals(2, currentVersion(status(database, migrations).also { assertTrue(it.contains("\"lock\":null")) }))
        val reversed = Output()
        assertEquals(0, cli(database, reversed, "migrate-down", "--holder", "test@junit", "--migrations", migrations.toString()))
        reversed.require("migration_reversed")
        assertEquals(1, currentVersion(status(database, migrations)))
    }

    @Test
    fun `renamed or removed applied migrations are refused`() {
        val database = TestDatabase.fresh()
        val migrations = basicSet()
        migrate(database, migrations)
        Files.move(migrations.resolve("V002__create_t2.up.sql"), migrations.resolve("V002__make_t2.up.sql"))
        Files.move(migrations.resolve("V002__create_t2.down.sql"), migrations.resolve("V002__make_t2.down.sql"))
        val renamed = Output()
        assertEquals(1, cli(database, renamed, "status", "--migrations", migrations.toString()))
        val problem = renamed.require("migration_registry_problem")
        assertTrue(problem.contains("\"kind\":\"applied_migration_renamed\""))
        assertTrue(problem.contains("\"applied\":\"V002__create_t2\""))
        Files.delete(migrations.resolve("V002__make_t2.up.sql"))
        Files.delete(migrations.resolve("V002__make_t2.down.sql"))
        val removed = Output()
        assertEquals(1, cli(database, removed, "status", "--migrations", migrations.toString()))
        assertTrue(removed.require("migration_registry_problem").contains("\"kind\":\"applied_migration_missing\""))
        // RECOVERY.md §3: restoring the reviewed files (same content, same checksum) makes the set consistent again.
        basicSet()
        assertTrue(migrate(database, migrations).require("migration_plan").contains("\"steps\":[]"))
        assertEquals(2, currentVersion(status(database, migrations)))
    }

    @Test
    fun `the registry is found regardless of the session search_path`() {
        val database = TestDatabase.fresh()
        Fixtures.write(
            directory,
            "V001__seed_public_acct",
            Fixtures.header() + "CREATE TABLE public.acct (id integer PRIMARY KEY);\nINSERT INTO public.acct VALUES (1);\n",
            "DROP TABLE public.acct;\n",
        )
        migrate(database, directory)
        database.execute("CREATE SCHEMA other; ALTER DATABASE ${database.name} SET search_path = other")
        assertEquals("other", database.scalar("SHOW search_path"))
        val report = status(database, directory)
        assertTrue(report.contains("\"registryPresent\":true"))
        assertEquals(1, currentVersion(report))
        val again = migrate(database, directory)
        assertTrue(again.require("migration_plan").contains("\"steps\":[]"))
        assertNull(again.event("migration_applied"))
        assertEquals(1L, database.count("SELECT count(*) FROM public.acct"))
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        assertNull(database.scalar("SELECT to_regclass('other.migration_registry')"))
        assertNull(database.scalar("SELECT to_regclass('public.migration_registry')"))
        val currentSchema = TestDatabase(database.url + "?currentSchema=other", database.user, database.password)
        assertEquals(1, currentVersion(status(currentSchema, directory)))
        val reversed = Output()
        assertEquals(0, cli(currentSchema, reversed, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString()))
        reversed.require("migration_reversed")
        assertNull(database.scalar("SELECT to_regclass('public.acct')"))
        assertEquals(0, currentVersion(status(database, directory)))
    }

    @Test
    fun `a second runner is refused while the lock is held and the message names the holder`() {
        val database = TestDatabase.fresh()
        val migrations = basicSet()
        migrate(database, migrations, "--target", "1")
        database.connect().use { connection -> Registry(connection).claim(Identity("api#55@alice", "host-a", 4242), "migrate", 2) }
        val output = Output()
        assertEquals(1, cli(database, output, "migrate", "--holder", "bob@ci", "--migrations", migrations.toString()))
        val refused = output.require("migration_lock_refused")
        assertTrue(refused.contains("\"holder\":\"api#55@alice\""))
        assertTrue(refused.contains("\"host\":\"host-a\""))
        assertTrue(refused.contains("\"pid\":4242"))
        assertTrue(refused.contains("\"command\":\"migrate\""))
        assertTrue(refused.contains("\"targetVersion\":2"))
        assertNull(output.event("migration_applied"))
        val down = Output()
        assertEquals(1, cli(database, down, "migrate-down", "--holder", "bob@ci", "--migrations", migrations.toString(), "--target", "0"))
        down.require("migration_plan")
        down.require("migration_lock_refused")
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        val lockStatus = status(database, migrations)
        assertEquals(1, currentVersion(lockStatus))
        assertTrue(lockStatus.contains("\"lock\":{\"holder\":\"api#55@alice\""))
        database.connect().use { connection ->
            val error = assertThrows(LockRefused::class.java) { Registry(connection).claim(Identity("carol", "host-c", 7), "migrate", 2) }
            assertTrue(error.message!!.contains("held by api#55@alice on host-a (pid 4242)"))
            assertTrue(error.message!!.contains("towards V002"))
        }
    }

    @Test
    fun `concurrent claims admit exactly one runner`() {
        val database = TestDatabase.fresh()
        database.connect().use { Registry(it).bootstrap() }
        val start = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)
        try {
            val holders = listOf("racer-1", "racer-2")
            val attempts =
                holders.map { holder ->
                    executor.submit<Any> {
                        database.connect().use { connection ->
                            start.await(10, TimeUnit.SECONDS)
                            try {
                                Registry(connection).claim(Identity(holder, "host", 1), "migrate", 1)
                            } catch (error: LockRefused) {
                                error
                            }
                        }
                    }
                }
            start.countDown()
            val outcomes = holders.zip(attempts.map { it.get(30, TimeUnit.SECONDS) })
            val (winner, claim) = outcomes.single { it.second is LockClaim }
            val (loser, refusal) = outcomes.single { it.second is LockRefused }
            assertEquals(winner, (claim as LockClaim).holder)
            assertEquals(winner, (refusal as LockRefused).claim.holder)
            assertTrue(loser != winner)
        } finally {
            executor.shutdownNow()
        }
    }

    @Test
    fun `a failing migration rolls back, records the failure without data and the recovery path works`() {
        val database = TestDatabase.fresh()
        Fixtures.write(directory, "V001__create_t1", Fixtures.header() + "CREATE TABLE t1 (id integer PRIMARY KEY);\n", "DROP TABLE t1;\n")
        Fixtures.write(
            directory,
            "V002__seed_t2",
            Fixtures.header() +
                "CREATE TABLE t2 (id integer PRIMARY KEY, secret text);\nINSERT INTO t2 VALUES (1, 'row-value');\nINSERT INTO t2 VALUES (1, 'row-value');\n",
            "DROP TABLE t2;\n",
        )
        val output = Output()
        assertEquals(1, cli(database, output, "migrate", "--holder", "test@junit", "--migrations", directory.toString()))
        val failed = output.require("migration_failed")
        assertTrue(failed.contains("\"version\":2"))
        assertTrue(failed.contains("\"direction\":\"up\""))
        assertTrue(failed.contains("\"knownVersion\":1"))
        assertTrue(failed.contains("\"sqlState\":\"23505\""))
        assertTrue(failed.contains("\"table\":\"t2\""))
        assertTrue(failed.contains("\"constraint\":\"t2_pkey\""))
        assertFalse(output.toString().contains("row-value"))
        assertFalse(output.toString().contains("(1)"))
        assertFalse(output.toString().contains("duplicate key"))
        output.require("migration_lock_released")
        assertNull(database.scalar("SELECT to_regclass('t2')"))
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_registry WHERE state = 'failed' AND version = 2"))

        val recovery = status(database, directory)
        assertEquals(1, currentVersion(recovery))
        assertTrue(recovery.contains("\"lock\":null"))
        assertTrue(recovery.contains("\"state\":\"failed\""))
        assertTrue(recovery.contains("\"failure\":{\"sqlState\":\"23505\""))

        Fixtures.write(
            directory,
            "V002__seed_t2",
            Fixtures.header() + "CREATE TABLE t2 (id integer PRIMARY KEY, secret text);\nINSERT INTO t2 VALUES (1, 'row-value');\n",
            "DROP TABLE t2;\n",
        )
        migrate(database, directory)
        val recovered = status(database, directory)
        assertEquals(2, currentVersion(recovered))
        assertFalse(recovered.contains("\"state\":\"failed\""))
        assertEquals(3L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
    }

    @Test
    fun `syntax errors report the position and a dead holder can be released by name`() {
        val database = TestDatabase.fresh()
        Fixtures.write(directory, "V001__typo", Fixtures.header() + "CREAT TABLE t1 (id integer);\n", "DROP TABLE t1;\n")
        val output = Output()
        assertEquals(1, cli(database, output, "migrate", "--holder", "test@junit", "--migrations", directory.toString()))
        val failed = output.require("migration_failed")
        assertTrue(failed.contains("\"sqlState\":\"42601\""))
        assertTrue(Regex("\"position\":[1-9]\\d*").containsMatchIn(failed), failed)
        assertTrue(failed.contains("\"knownVersion\":0"))
        database.connect().use { Registry(it).claim(Identity("ghost@host", "gone", 99), "migrate", 1) }
        val stale = Output()
        assertEquals(1, cli(database, stale, "migrate", "--holder", "test@junit", "--migrations", directory.toString()))
        val refused = stale.require("migration_lock_refused")
        assertTrue(refused.contains("\"holder\":\"ghost@host\",\"host\":\"gone\",\"pid\":99"))
        // The exact claim must be named: the holder string alone, or a wrong host or pid, releases nothing.
        val holderOnly = Output()
        assertEquals(2, cli(database, holderOnly, "release-lock", "--holder", "ghost@host"))
        assertTrue(holderOnly.require("usage_error").contains("release-lock requires --holder, --host and --pid"))
        val wrongHost = Output()
        assertEquals(1, cli(database, wrongHost, "release-lock", "--holder", "ghost@host", "--host", "elsewhere", "--pid", "99"))
        wrongHost.require("migration_lock_not_held")
        val wrongPid = Output()
        assertEquals(1, cli(database, wrongPid, "release-lock", "--holder", "ghost@host", "--host", "gone", "--pid", "98"))
        wrongPid.require("migration_lock_not_held")
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock WHERE holder = 'ghost@host'"))
        val released = Output()
        assertEquals(0, cli(database, released, "release-lock", "--holder", "ghost@host", "--host", "gone", "--pid", "99"))
        val event = released.require("migration_lock_released")
        assertTrue(event.contains("\"releasedBy\":\"operator\""))
        assertTrue(event.contains("\"host\":\"gone\",\"pid\":99"))
        Fixtures.write(directory, "V001__typo", Fixtures.header() + "CREATE TABLE t1 (id integer);\n", "DROP TABLE t1;\n")
        migrate(database, directory)
        assertEquals(1, currentVersion(status(database, directory)))
    }

    @Test
    fun `a script that ends its own transaction is refused and reported for inspection`() {
        val database = TestDatabase.fresh()
        Fixtures.write(
            directory,
            "V001__commits",
            Fixtures.header() + "CREATE TABLE t1 (id integer);\nCOMMIT;\nCREATE TABLE t2 (id integer);\n",
            "DROP TABLE t1;\n",
        )
        val output = Output()
        assertEquals(1, cli(database, output, "migrate", "--holder", "test@junit", "--migrations", directory.toString()))
        val error = output.require("runner_error")
        assertTrue(error.contains("V001__commits ended its own transaction"))
        assertTrue(output.require("migration_lock_released").contains("test@junit"))
        assertEquals(0L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        assertNotNull(database.scalar("SELECT to_regclass('t1')"))
        database.execute("DROP TABLE t1; DROP TABLE t2")
        Fixtures.write(
            directory,
            "V001__commits",
            Fixtures.header() + "CREATE TABLE t1 (id integer);\nCREATE TABLE t2 (id integer);\n",
            "DROP TABLE t1;\n",
        )
        migrate(database, directory)
        assertEquals(1, currentVersion(status(database, directory)))
    }

    @Test
    fun `a registry that changed between planning and claiming is refused under the lock`() {
        val database = TestDatabase.fresh()
        val set = MigrationSet.load(basicSet())
        database.connect().use { connection ->
            val events = mutableListOf<String>()
            val runner =
                MigrationRunner(connection, set, Identity("planner", "host", 1), Duration.ofSeconds(60)) { line ->
                    events += line
                    if (line.contains("\"event\":\"migration_plan\"")) {
                        // Another runner finishes V001 between this runner's plan and its claim.
                        database.connect().use { other ->
                            MigrationRunner(other, set, Identity("racer", "host", 2), Duration.ofSeconds(60)) {}
                                .migrate(1, includeContract = false, dryRun = false)
                        }
                    }
                }
            val error = assertThrows(RegistryProblem::class.java) { runner.migrate(null, includeContract = false, dryRun = false) }
            assertEquals("registry_changed_before_lock", error.kind)
            assertEquals(mapOf("planned" to 0, "current" to 1), error.details)
            assertTrue(events.any { it.contains("\"event\":\"migration_lock_released\"") })
            assertNull(runner.snapshot().lock)
            assertEquals(1, runner.snapshot().currentVersion)
        }
    }

    @Test
    fun `a guarded reversal refuses to run while the table holds rows`() {
        val database = TestDatabase.fresh()
        Fixtures.write(
            directory,
            "V001__create_entries",
            Fixtures.header() +
                "CREATE SCHEMA ledger;\nCREATE TABLE ledger.entries (id integer PRIMARY KEY, amount_minor bigint NOT NULL);\n",
            "DO \$\$ BEGIN\n" +
                "    IF EXISTS (SELECT 1 FROM ledger.entries) THEN\n" +
                "        RAISE EXCEPTION USING MESSAGE = 'committed ledger rows present; reverse refused',\n" +
                "            ERRCODE = 'P0001', SCHEMA = 'ledger', TABLE = 'entries';\n" +
                "    END IF;\n" +
                "END \$\$;\n" +
                "DROP TABLE ledger.entries;\nDROP SCHEMA ledger RESTRICT;\n",
        )
        migrate(database, directory)
        database.execute("INSERT INTO ledger.entries VALUES (1, 4200), (2, -4200)")
        val refused = Output()
        assertEquals(1, cli(database, refused, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString()))
        val failed = refused.require("migration_failed")
        assertTrue(failed.contains("\"sqlState\":\"P0001\""))
        assertTrue(failed.contains("\"schema\":\"ledger\""))
        assertTrue(failed.contains("\"table\":\"entries\""))
        assertTrue(failed.contains("\"knownVersion\":1"))
        assertFalse(refused.toString().contains("committed ledger rows"))
        assertEquals(2L, database.count("SELECT count(*) FROM ledger.entries"))
        assertEquals(1, currentVersion(status(database, directory)))
        // Only after the rows are gone (here: synthetic test data) does the reverse run.
        database.execute("DELETE FROM ledger.entries")
        val reversed = Output()
        assertEquals(0, cli(database, reversed, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString()))
        reversed.require("migration_reversed")
        assertEquals(0, currentVersion(status(database, directory)))
        assertNull(database.scalar("SELECT to_regnamespace('ledger')"))
    }

    @Test
    fun `dry run prints the plan and writes nothing`() {
        val database = TestDatabase.fresh()
        val migrations = basicSet()
        val emptyRun = Output()
        assertEquals(0, cli(database, emptyRun, "migrate", "--dry-run", "--migrations", migrations.toString()))
        val plan = emptyRun.require("migration_plan")
        assertTrue(plan.contains("\"dryRun\":true"))
        assertTrue(plan.contains("\"currentVersion\":0"))
        assertTrue(plan.contains("\"action\":\"apply\""))
        assertNull(emptyRun.event("migration_lock_claimed"))
        assertNull(database.scalar("SELECT to_regclass('migration_runner.migration_registry')"))
        assertNull(database.scalar("SELECT to_regclass('migration_runner.migration_lock')"))
        assertNull(database.scalar("SELECT to_regclass('t1')"))

        migrate(database, migrations, "--target", "1")
        val before = status(database, migrations)
        val populated = Output()
        assertEquals(0, cli(database, populated, "migrate", "--dry-run", "--migrations", migrations.toString()))
        assertTrue(populated.require("migration_plan").contains("\"id\":\"V002__create_t2\""))
        val downRun = Output()
        assertEquals(0, cli(database, downRun, "migrate-down", "--dry-run", "--migrations", migrations.toString(), "--target", "0"))
        assertTrue(downRun.require("migration_plan").contains("\"action\":\"reverse\""))
        assertEquals(before, status(database, migrations))
        assertEquals(1, currentVersion(before))
        assertNull(database.scalar("SELECT to_regclass('t2')"))
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_registry"))
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock WHERE holder IS NULL"))
    }

    @Test
    fun `contract migrations are held until consumers have migrated`() {
        val database = TestDatabase.fresh()
        Fixtures.threePhaseSet(directory)
        val first = migrate(database, directory)
        assertEquals(2, first.events("migration_applied").size)
        val held = first.require("migration_contract_held")
        assertTrue(held.contains("\"id\":\"V003__drop_legacy\""))
        assertTrue(held.contains("\"expandVersion\":1"))
        assertTrue(first.require("migration_plan").contains("\"action\":\"hold\""))
        assertEquals(2, currentVersion(status(database, directory)))
        database.execute("INSERT INTO widgets (id, legacy) VALUES (1, 'old')")

        // A run in which the hold is the only thing that happens still reports it, without taking the lock.
        val onlyHeld = migrate(database, directory)
        assertTrue(onlyHeld.require("migration_contract_held").contains("\"id\":\"V003__drop_legacy\""))
        assertNull(onlyHeld.event("migration_lock_claimed"))
        assertNull(onlyHeld.event("migration_applied"))
        assertEquals(2, currentVersion(status(database, directory)))
        val heldDryRun = Output()
        assertEquals(0, cli(database, heldDryRun, "migrate", "--dry-run", "--migrations", directory.toString()))
        assertTrue(heldDryRun.require("migration_plan").contains("\"action\":\"hold\""))
        assertNull(heldDryRun.event("migration_contract_held"))

        val second = migrate(database, directory, "--include-contract")
        assertTrue(second.require("migration_applied").contains("\"phase\":\"contract\""))
        assertEquals(3, currentVersion(status(database, directory)))
        val nothing = migrate(database, directory, "--include-contract")
        assertNull(nothing.event("migration_lock_claimed"))
        assertTrue(nothing.require("migration_plan").contains("\"steps\":[]"))

        val oneStep = Output()
        assertEquals(0, cli(database, oneStep, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString()))
        val reversed = oneStep.require("migration_reversed")
        assertTrue(reversed.contains("\"reversal\":\"compensating\""))
        assertTrue(reversed.contains("\"reason\":\"the dropped legacy values cannot be restored\""))
        assertEquals(2, currentVersion(status(database, directory)))
        assertEquals(1L, database.count("SELECT count(*) FROM widgets WHERE legacy IS NULL"))
        val rest = Output()
        assertEquals(
            0,
            cli(database, rest, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString(), "--target", "0"),
        )
        assertEquals(2, rest.events("migration_reversed").size)
        assertEquals(0, currentVersion(status(database, directory)))
        val idle = Output()
        assertEquals(0, cli(database, idle, "migrate-down", "--holder", "test@junit", "--migrations", directory.toString()))
        assertTrue(idle.require("migration_plan").contains("\"steps\":[]"))
    }

    @Test
    fun `targets outside the current range are usage errors`() {
        val database = TestDatabase.fresh()
        val migrations = basicSet()
        migrate(database, migrations, "--target", "1")
        val tooHigh = Output()
        assertEquals(2, cli(database, tooHigh, "migrate", "--holder", "test@junit", "--migrations", migrations.toString(), "--target", "3"))
        assertTrue(
            tooHigh.require("usage_error").contains("target V003 must be between the current version V001 and the latest migration V002"),
        )
        val tooLow = Output()
        assertEquals(2, cli(database, tooLow, "migrate", "--holder", "test@junit", "--migrations", migrations.toString(), "--target", "0"))
        tooLow.require("usage_error")
        val downTooHigh = Output()
        assertEquals(
            2,
            cli(database, downTooHigh, "migrate-down", "--holder", "test@junit", "--migrations", migrations.toString(), "--target", "2"),
        )
        assertTrue(downTooHigh.require("usage_error").contains("target V002 must be between V000 and the current version V001"))
        assertEquals(1, currentVersion(status(database, migrations)))
    }

    @Test
    fun `slow migrations raise an alert while still running`() {
        val database = TestDatabase.fresh()
        Fixtures.write(directory, "V001__slow", Fixtures.header() + "SELECT pg_sleep(2.5);\n", "SELECT 1;\n")
        val output = Output()
        assertEquals(
            0,
            cli(
                database,
                output,
                "migrate",
                "--holder",
                "test@junit",
                "--migrations",
                directory.toString(),
                "--slow-threshold-seconds",
                "1",
            ),
        )
        val lines = output.lines()
        val slow = lines.indexOfFirst { it.contains("\"event\":\"migration_slow\"") }
        val applied = lines.indexOfFirst { it.contains("\"event\":\"migration_applied\"") }
        assertTrue(slow in 0 until applied, output.toString())
        assertTrue(lines[slow].contains("\"thresholdSeconds\":1"))
    }

    @Test
    fun `a lock released underneath the runner is an incident that exits 1`() {
        val database = TestDatabase.fresh()
        Fixtures.write(
            directory,
            "V001__steal",
            Fixtures.header() + "UPDATE migration_runner.migration_lock SET holder = 'someone-else';\n",
            "SELECT 1;\n",
        )
        val output = Output()
        assertEquals(1, cli(database, output, "migrate", "--holder", "test@junit", "--migrations", directory.toString()))
        output.require("migration_applied")
        output.require("migration_lock_release_mismatch")
        assertNull(output.event("migration_lock_released"))
        // The applied migration is recorded and the foreign claim is left alone for the operator to inspect.
        val report = status(database, directory)
        assertEquals(1, currentVersion(report))
        assertTrue(report.contains("\"lock\":{\"holder\":\"someone-else\""))
        val rerun = Output()
        assertEquals(0, cli(database, rerun, "migrate", "--holder", "test@junit", "--migrations", directory.toString()))
        assertTrue(rerun.require("migration_plan").contains("\"steps\":[]"))
        assertNull(rerun.event("migration_lock_claimed"))

        // The same incident during a reversal.
        val other = TestDatabase.fresh()
        val reversalSteal = directory.resolve("reversal")
        Fixtures.write(
            reversalSteal,
            "V001__steal_on_down",
            Fixtures.header() + "SELECT 1;\n",
            "UPDATE migration_runner.migration_lock SET holder = 'someone-else';\n",
        )
        migrate(other, reversalSteal)
        val down = Output()
        assertEquals(1, cli(other, down, "migrate-down", "--holder", "test@junit", "--migrations", reversalSteal.toString()))
        down.require("migration_reversed")
        down.require("migration_lock_release_mismatch")
        assertEquals(0, currentVersion(status(other, reversalSteal)))
    }

    @Test
    fun `status can be written to a file and reports registry corruption`() {
        val database = TestDatabase.fresh()
        val migrations = basicSet()
        migrate(database, migrations)
        val file = directory.resolve("status.json")
        val output = Output()
        assertEquals(0, cli(database, output, "status", "--migrations", migrations.toString(), "--status-file", file.toString()))
        val written = Files.readString(file)
        assertTrue(written.startsWith("{\"registryPresent\":true,\"currentVersion\":2,\"latestVersion\":2,"))
        assertTrue(written.contains("\"lock\":null"))
        val unwritable = Output()
        val missingDirectory = directory.resolve("absent").resolve("status.json").toString()
        assertEquals(1, cli(database, unwritable, "status", "--migrations", migrations.toString(), "--status-file", missingDirectory))
        assertTrue(unwritable.require("runner_error").contains("NoSuchFileException"))
        assertFalse(unwritable.toString().contains(database.password))

        database.execute("DELETE FROM migration_runner.migration_registry WHERE version = 1")
        val gap = Output()
        assertEquals(1, cli(database, gap, "status", "--migrations", migrations.toString()))
        assertTrue(gap.require("migration_registry_problem").contains("\"kind\":\"registry_not_contiguous\""))
        // RECOVERY.md §4: reinsert the applied row with the checksums that validate prints.
        val lost = MigrationSet.load(migrations).byVersion(1)!!
        database.execute(
            "INSERT INTO migration_runner.migration_registry (version, migration_id, phase, checksum, reversal_kind, reversal_file," +
                " reversal_checksum, state, direction, applied_by, host, pid, duration_ms) VALUES" +
                " (1, '${lost.id}', '${lost.phase.directive}', '${lost.checksum}', '${lost.reversal.kind.directive}'," +
                " '${lost.reversal.file}', '${lost.reversal.checksum}', 'applied', 'up', 'operator@recovery', 'console', 0, 0)",
        )
        assertEquals(2, currentVersion(status(database, migrations)))

        database.execute("DELETE FROM migration_runner.migration_lock")
        val noLock = Output()
        assertEquals(1, cli(database, noLock, "status", "--migrations", migrations.toString()))
        assertTrue(noLock.require("runner_error").contains("migration_lock singleton row is missing"))
        // RECOVERY.md §4: the next writing command's bootstrap recreates the lock row.
        assertTrue(migrate(database, migrations).require("migration_plan").contains("\"steps\":[]"))
        assertEquals(1L, database.count("SELECT count(*) FROM migration_runner.migration_lock WHERE holder IS NULL"))
        assertTrue(status(database, migrations).contains("\"lock\":null"))
    }

    @Test
    fun `status before any migration reports every migration as pending`() {
        val database = TestDatabase.fresh()
        val report = status(database, basicSet())
        assertTrue(report.contains("\"registryPresent\":false"))
        assertTrue(report.contains("\"lastApplied\":null"))
        assertEquals(2, Regex("\"state\":\"pending\"").findAll(report).count())
    }

    @Test
    fun `the packaged entry point runs from the command line`() {
        val database = TestDatabase.fresh()
        val java = Path.of(System.getProperty("java.home"), "bin", "java").toString()
        val log = directory.resolve("process.log")
        val builder =
            ProcessBuilder(
                java,
                "-cp",
                System.getProperty("app.test.classpath"),
                "com.pennilogic.migration.MigrationCliKt",
                "migrate",
                "--holder",
                "process@junit",
                "--migrations",
                shipped.toAbsolutePath().toString(),
            ).redirectErrorStream(true).redirectOutput(log.toFile())
        builder.environment().apply {
            remove("JAVA_TOOL_OPTIONS")
            remove("_JAVA_OPTIONS")
            remove("JDK_JAVA_OPTIONS")
            putAll(database.environment())
        }
        val process = builder.start()
        assertTrue(process.waitFor(60, TimeUnit.SECONDS))
        val text = Files.readString(log)
        assertEquals(0, process.exitValue(), text)
        assertTrue(text.contains("\"event\":\"migration_applied\""))
        assertFalse(text.contains(database.password))
        assertEquals(2, currentVersion(status(database, shipped)))
    }

    @Test
    fun `runner reports the shipped set through the library API`() {
        val database = TestDatabase.fresh()
        val events = mutableListOf<String>()
        database.connect().use { connection ->
            val runner =
                MigrationRunner(
                    connection,
                    MigrationSet.load(shipped),
                    Identity("library@junit", "host", 1),
                    Duration.ofSeconds(60),
                    events::add,
                )
            assertEquals(0, runner.migrate(null, includeContract = false, dryRun = false))
            val snapshot = runner.snapshot()
            assertEquals(2, snapshot.currentVersion)
            assertEquals("applied", snapshot.states.getValue(1).label)
            assertEquals("applied", snapshot.states.getValue(2).label)
            assertNull(snapshot.lock)
            assertEquals(0, runner.migrateDown(0, dryRun = false))
            assertEquals(
                "reversed",
                runner
                    .snapshot()
                    .states
                    .getValue(1)
                    .label,
            )
            assertThrows(UsageError::class.java) { runner.migrateDown(-1, dryRun = true) }
        }
        assertEquals(
            listOf(
                "migration_plan",
                "migration_lock_claimed",
                "migration_applied",
                "migration_applied",
                "migration_lock_released",
            ),
            events.take(5).map {
                it.substringAfter("\"event\":\"").substringBefore('"')
            },
        )
    }
}
