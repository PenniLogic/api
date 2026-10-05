package com.pennilogic.ledger

import com.pennilogic.migration.MigrationFailed
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import java.sql.Connection
import java.sql.SQLException
import java.time.Duration
import java.util.concurrent.Callable
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@Tag("postgres")
class LedgerConcurrencyPostgresTest {
    @Test
    fun `concurrent candidate posting serializes and only one sealed composition survives at every isolation`() =
        checked("concurrent-posting") {
            for ((isolation, expected) in isolations) {
                val fixture = LedgerFixture()
                fixture.connect().use { setup ->
                    val owner = ledgerId()
                    val first = setup.account(owner)
                    val second = setup.account(owner)
                    val candidate = setup.transaction(owner, status = "candidate", count = 0, booked = null)
                    race(
                        fixture,
                        isolation,
                        snapshot = {
                            same(
                                "candidate",
                                it.scalar("SELECT status FROM pennilogic.transactions WHERE id = ?", candidate),
                                "posting-old-snapshot",
                            )
                        },
                        firstWrite = {
                            it.sql(
                                "UPDATE pennilogic.transactions SET status = 'posted', entry_count = 2, booked_at = ? WHERE id = ?",
                                ledgerNow(),
                                candidate,
                            )
                            it.pair(candidate, owner, first, second)
                        },
                        secondWrite = {
                            it.sql(
                                "UPDATE pennilogic.transactions SET status = 'posted', entry_count = 2, booked_at = ? WHERE id = ?",
                                ledgerNow(),
                                candidate,
                            )
                            it.pair(candidate, owner, first, second)
                        },
                        firstFinish = { it.commit() },
                        expected = expected,
                        caseId = "posting-$isolation",
                    )
                    same("2", setup.scalar("SELECT count(*) FROM pennilogic.entries"), "single-composition-$isolation")
                    same("0", setup.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries"), "concurrent-zero-sum-$isolation")
                }
            }
        }

    @Test
    fun `concurrent corrections cannot write skew through stale snapshots at any isolation`() =
        checked("concurrent-reposts") {
            for ((isolation, expected) in isolations) {
                val fixture = LedgerFixture()
                fixture.connect().use { setup ->
                    val owner = ledgerId()
                    val first = setup.account(owner)
                    val second = setup.account(owner)
                    val occurred = ledgerNow().minusDays(1)
                    setup.autoCommit = false
                    val original = setup.transaction(owner, occurred = occurred)
                    setup.pair(original, owner, first, second)
                    setup.commit()
                    val reversal =
                        setup.transaction(
                            owner,
                            kind = "REVERSAL",
                            reverses = original,
                            occurred = occurred,
                            correction = ledgerId(),
                            reason = "USER_CORRECTION",
                        )
                    setup.pair(reversal, owner, second, first)
                    setup.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", original)
                    setup.commit()
                    setup.autoCommit = true
                    race(
                        fixture,
                        isolation,
                        snapshot = {
                            same(
                                "0",
                                it.scalar("SELECT count(*) FROM pennilogic.transactions WHERE replaces = ?", original),
                                "correction-old-snapshot",
                            )
                        },
                        firstWrite = {
                            val repost =
                                it.transaction(
                                    owner,
                                    kind = "REPOST",
                                    replaces = original,
                                    occurred = occurred,
                                    correction = ledgerId(),
                                    reason = "USER_CORRECTION",
                                )
                            it.pair(repost, owner, first, second)
                        },
                        secondWrite = {
                            val repost =
                                it.transaction(
                                    owner,
                                    kind = "REPOST",
                                    replaces = original,
                                    occurred = occurred,
                                    correction = ledgerId(),
                                    reason = "USER_CORRECTION",
                                )
                            it.pair(repost, owner, first, second)
                        },
                        firstFinish = { it.commit() },
                        expected = expected,
                        caseId = "correction-$isolation",
                    )
                    same(
                        "1",
                        setup.scalar("SELECT count(*) FROM pennilogic.transactions WHERE replaces = ?", original),
                        "single-correction-$isolation",
                    )
                    same("6", setup.scalar("SELECT count(*) FROM pennilogic.entries"), "correction-history-$isolation")
                }
            }
        }

    @Test
    fun `two late balanced appends both fail without changing sealed history`() =
        checked("concurrent-late-append") {
            val fixture = LedgerFixture()
            fixture.connect().use { setup ->
                val owner = ledgerId()
                val first = setup.account(owner)
                val second = setup.account(owner)
                setup.autoCommit = false
                val original = setup.transaction(owner)
                setup.pair(original, owner, first, second)
                setup.commit()
                setup.autoCommit = true
                race(
                    fixture,
                    Connection.TRANSACTION_READ_COMMITTED,
                    snapshot = { same("2", it.scalar("SELECT count(*) FROM pennilogic.entries"), "late-old-snapshot") },
                    firstWrite = { it.pair(original, owner, first, second) },
                    secondWrite = { it.pair(original, owner, first, second) },
                    firstFinish = {
                        rejected("first-late-commit", "23514", "transaction_sealed") { it.commit() }
                        it.rollback()
                    },
                    expected = "23514",
                    caseId = "late-composition",
                )
                same("2", setup.scalar("SELECT count(*) FROM pennilogic.entries"), "late-history-preserved")
            }
        }

    @Test
    fun `the down migration locks before testing emptiness and cannot race a committed account`() =
        checked("reverse-lock-before-guard") {
            val fixture = LedgerFixture()
            fixture.connect().use { writer ->
                fixture.connect().use { migration ->
                    writer.autoCommit = false
                    writer.account(ledgerId())
                    val pid = requireNotNull(migration.scalar("SELECT pg_backend_pid()")).toInt()
                    val executor = Executors.newSingleThreadExecutor()
                    val started = CountDownLatch(1)
                    try {
                        val result =
                            executor.submit(
                                Callable {
                                    started.countDown()
                                    try {
                                        fixture.runner(migration, holder = "ledger-down@junit").migrateDown(1, dryRun = false)
                                        "unexpected-commit"
                                    } catch (error: MigrationFailed) {
                                        error.failure.sqlState
                                    }
                                },
                            )
                        assertTrue(started.await(5, TimeUnit.SECONDS), "reverse-worker-started")
                        awaitBlocked(fixture, pid, "down-table-lock")
                        fixture.connect().use { observer ->
                            same(
                                "ledger-down@junit",
                                observer.scalar("SELECT holder FROM migration_runner.migration_lock"),
                                "runner-lock-held-during-table-wait",
                            )
                        }
                        writer.commit()
                        same("P0001", result.get(15, TimeUnit.SECONDS), "down-race-refused")
                        same(2, fixture.runner(migration).snapshot().currentVersion, "down-race-version")
                        same(null, fixture.runner(migration).snapshot().lock, "down-race-lock-released")
                        same("1", migration.scalar("SELECT count(*) FROM pennilogic.accounts"), "down-race-row-preserved")
                    } finally {
                        writer.rollback()
                        migration.close()
                        executor.shutdownNow()
                        assertTrue(executor.awaitTermination(15, TimeUnit.SECONDS), "down-worker-closed")
                    }
                }
            }
        }

    @Test
    fun `closing an uncommitted writer leaves no partial transaction or entries`() =
        checked("disconnect-rollback") {
            val fixture = LedgerFixture()
            fixture.connect().use { writer ->
                val owner = ledgerId()
                val first = writer.account(owner)
                val second = writer.account(owner)
                writer.autoCommit = false
                val transaction = writer.transaction(owner)
                writer.pair(transaction, owner, first, second)
                fixture.connect().use { observer ->
                    same("0", observer.scalar("SELECT count(*) FROM pennilogic.transactions"), "uncommitted-not-visible")
                }
            }
            fixture.connect().use { observer ->
                same("0", observer.scalar("SELECT count(*) FROM pennilogic.transactions"), "disconnect-transaction-rolled-back")
                same("0", observer.scalar("SELECT count(*) FROM pennilogic.entries"), "disconnect-entries-rolled-back")
            }
        }

    private fun race(
        fixture: LedgerFixture,
        isolation: Int,
        snapshot: (Connection) -> Unit,
        firstWrite: (Connection) -> Unit,
        secondWrite: (Connection) -> Unit,
        firstFinish: (Connection) -> Unit,
        expected: String,
        caseId: String,
    ) {
        fixture.connect().use { first ->
            fixture.connect().use { second ->
                first.transactionIsolation = isolation
                second.transactionIsolation = isolation
                first.autoCommit = false
                second.autoCommit = false
                first.sql("SET LOCAL statement_timeout = '15s'")
                second.sql("SET LOCAL statement_timeout = '15s'")
                val pid = requireNotNull(second.scalar("SELECT pg_backend_pid()")).toInt()
                snapshot(second)
                firstWrite(first)
                val started = CountDownLatch(1)
                val executor = Executors.newSingleThreadExecutor()
                try {
                    val result =
                        executor.submit(
                            Callable {
                                started.countDown()
                                try {
                                    secondWrite(second)
                                    second.commit()
                                    "unexpected-commit"
                                } catch (error: SQLException) {
                                    error.sqlState
                                } finally {
                                    second.rollback()
                                }
                            },
                        )
                    assertTrue(started.await(5, TimeUnit.SECONDS), "$caseId-worker-started")
                    awaitBlocked(fixture, pid, caseId)
                    firstFinish(first)
                    same(expected, result.get(15, TimeUnit.SECONDS), "$caseId-refused")
                    println(
                        """{"event":"ledger_concurrency_case","case":"$caseId","isolation":$isolation,"blocked":true,"sqlState":"$expected"}""",
                    )
                } finally {
                    first.rollback()
                    second.close()
                    executor.shutdownNow()
                    assertTrue(executor.awaitTermination(15, TimeUnit.SECONDS), "$caseId-worker-closed")
                }
            }
        }
    }

    private fun awaitBlocked(
        fixture: LedgerFixture,
        pid: Int,
        caseId: String,
    ) {
        val deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos()
        fixture.connect().use { observer ->
            while (System.nanoTime() < deadline) {
                if (observer.scalar("SELECT (cardinality(pg_blocking_pids(?)) > 0)::text", pid) == "true") return
                Thread.sleep(10)
            }
        }
        throw AssertionError("$caseId-must-observe-real-database-lock")
    }

    private val isolations =
        listOf(
            Connection.TRANSACTION_READ_COMMITTED to "23514",
            Connection.TRANSACTION_REPEATABLE_READ to "40001",
            Connection.TRANSACTION_SERIALIZABLE to "40001",
        )
}
