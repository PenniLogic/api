package com.pennilogic.ledger

import com.pennilogic.contracts.money.Money
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import java.sql.Connection
import java.time.LocalDate
import java.time.OffsetDateTime
import java.util.UUID

@Tag("postgres")
class LedgerCorrectionsPostgresTest {
    @Test
    fun `exact reversing links restore every account and are unique terminal and immutable`() =
        checked("exact-reversal") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner, "LIABILITY")
                val occurred = ledgerNow().minusDays(10)
                val date = LocalDate.of(2026, 1, 1)
                connection.autoCommit = false
                val original = connection.transaction(owner, kind = "OPENING_BALANCE", occurred = occurred, valueDate = date)
                connection.pair(original, owner, first, second)
                connection.commit()
                val correction = ledgerId()
                val reversal = connection.reversal(owner, original, first, second, occurred, date, correction)
                connection.commit()
                for ((index, account) in listOf(first, second).withIndex()) {
                    same(
                        "0",
                        connection.scalar(
                            "SELECT sum(amount_minor)::text FROM pennilogic.entries WHERE owner_id = ? AND account_id = ?",
                            owner,
                            account,
                        ),
                        "reversal-account-$index",
                    )
                    same(
                        "0",
                        connection.scalar(
                            """SELECT sum(e.amount_minor)::text FROM pennilogic.entries e
                                JOIN pennilogic.transactions t ON t.id = e.transaction_id
                                WHERE e.account_id = ? AND t.value_date <= ?""",
                            account,
                            date,
                        ),
                        "reversal-asof-$index",
                    )
                }
                same("reversed", connection.scalar("SELECT status FROM pennilogic.transactions WHERE id = ?", original), "reversal-status")
                rejected("duplicate-reversal", "23505") {
                    connection.reversal(owner, original, first, second, occurred, date, ledgerId())
                }
                connection.rollback()
                val terminal =
                    connection.transaction(
                        owner,
                        kind = "REVERSAL",
                        reverses = reversal,
                        occurred = occurred,
                        valueDate = date,
                        correction = ledgerId(),
                        reason = "USER_CORRECTION",
                    )
                connection.pair(terminal, owner, first, second)
                connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", reversal)
                rejected("terminal-reversal", "23514", "reversal_terminal") { connection.commit() }
                connection.rollback()
                same("4", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "terminal-history-restored")
            }
        }

    @Test
    fun `reversal multiset effective dates and target lifecycle are checked at commit`() =
        checked("reversal-negatives") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                val occurred = ledgerNow().minusDays(5)
                val date = LocalDate.of(2026, 1, 2)
                connection.autoCommit = false
                val original = connection.transaction(owner, count = 4, kind = "OPENING_BALANCE", occurred = occurred, valueDate = date)
                connection.pair(original, owner, first, second, Money.ofMinorUnits(1, "INR"))
                connection.pair(original, owner, first, second, Money.ofMinorUnits(3, "INR"))
                connection.commit()
                val wrong =
                    connection.transaction(
                        owner,
                        count = 4,
                        kind = "REVERSAL",
                        reverses = original,
                        occurred = occurred,
                        valueDate = date,
                        correction = ledgerId(),
                        reason = "USER_CORRECTION",
                    )
                connection.pair(wrong, owner, second, first, Money.ofMinorUnits(2, "INR"))
                connection.pair(wrong, owner, second, first, Money.ofMinorUnits(2, "INR"))
                connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", original)
                rejected("multiset-not-just-account-sums", "23514", "reversal_exact_negation") { connection.commit() }
                connection.rollback()
                for ((index, fields) in listOf(occurred.plusDays(1) to date, occurred to date.plusDays(1)).withIndex()) {
                    val badDate =
                        connection.transaction(
                            owner,
                            count = 4,
                            kind = "REVERSAL",
                            reverses = original,
                            occurred = fields.first,
                            valueDate = fields.second,
                            correction = ledgerId(),
                            reason = "USER_CORRECTION",
                        )
                    connection.pair(badDate, owner, second, first, Money.ofMinorUnits(1, "INR"))
                    connection.pair(badDate, owner, second, first, Money.ofMinorUnits(3, "INR"))
                    connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", original)
                    rejected("reversal-date-$index", "23514", "reversal_exact_negation") { connection.commit() }
                    connection.rollback()
                }
                val candidate = connection.transaction(owner, status = "candidate", count = 0, booked = null)
                connection.commit()
                val unposted =
                    connection.transaction(
                        owner,
                        kind = "REVERSAL",
                        reverses = candidate,
                        occurred = occurred,
                        correction = ledgerId(),
                        reason = "USER_CORRECTION",
                    )
                connection.pair(unposted, owner, second, first)
                rejected("unposted-target", "23514", "reversal_terminal") { connection.commit() }
                connection.rollback()
                same("4", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "reversal-negative-restoration")
            }
        }

    @Test
    fun `status cannot claim a reversal and a reversal alone cannot omit its target status`() =
        checked("status-link-equivalence") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                val occurred = ledgerNow().minusDays(1)
                connection.autoCommit = false
                val original = connection.transaction(owner, occurred = occurred)
                connection.pair(original, owner, first, second)
                connection.commit()
                connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", original)
                rejected("forged-reversed-status", "23514", "status_reversed_equivalence") { connection.commit() }
                connection.rollback()
                val reversal =
                    connection.transaction(
                        owner,
                        kind = "REVERSAL",
                        reverses = original,
                        occurred = occurred,
                        correction = ledgerId(),
                        reason = "USER_CORRECTION",
                    )
                connection.pair(reversal, owner, second, first)
                rejected("missing-target-status", "23514", "status_reversed_equivalence") { connection.commit() }
                connection.rollback()
                same("posted", connection.scalar("SELECT status FROM pennilogic.transactions WHERE id = ?", original), "status-restored")
                same("2", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "status-ledger-restored")
            }
        }

    @Test
    fun `reposts require a reversed fact preserve value date and supersede earlier correction groups`() =
        checked("repost-structure") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val owner = ledgerId()
                val first = connection.account(owner)
                val second = connection.account(owner)
                val occurred = ledgerNow().minusDays(3)
                val date = LocalDate.of(2026, 1, 1)
                connection.autoCommit = false
                val original = connection.transaction(owner, occurred = occurred, kind = "OPENING_BALANCE", valueDate = date)
                connection.pair(original, owner, first, second)
                connection.commit()
                connection.repost(owner, original, first, second, occurred, date, ledgerId())
                rejected("repost-without-reversal", "23514", "repost_without_reversal") { connection.commit() }
                connection.rollback()
                val correction = ledgerId()
                connection.reversal(owner, original, first, second, occurred, date, correction)
                val firstRepost = connection.repost(owner, original, first, second, occurred, date, correction)
                connection.commit()
                connection.repost(owner, original, first, second, occurred, null, correction)
                rejected("repost-missing-effective-date", "23514", "repost_without_reversal") { connection.commit() }
                connection.rollback()
                connection.repost(owner, original, first, second, occurred, date, ledgerId())
                rejected("repost-not-superseding", "23514", "repost_supersedes_prior") { connection.commit() }
                connection.rollback()
                val nextCorrection = ledgerId()
                connection.reversal(owner, firstRepost, first, second, occurred, date, nextCorrection)
                connection.repost(owner, original, first, second, occurred, date, nextCorrection)
                connection.commit()
                same(
                    "1",
                    connection.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries WHERE account_id = ?", first),
                    "one-active-fact",
                )
                same("10", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "correction-history-appended")
                connection.repost(owner, original, first, second, occurred, date, nextCorrection)
                connection.rollback()
                same("10", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "partial-correction-rollback")
            }
        }

    private fun Connection.reversal(
        owner: UUID,
        original: UUID,
        first: UUID,
        second: UUID,
        occurred: OffsetDateTime,
        date: LocalDate?,
        correction: UUID,
    ): UUID {
        val id =
            transaction(
                owner,
                kind = "REVERSAL",
                reverses = original,
                occurred = occurred,
                valueDate = date,
                correction = correction,
                reason = "USER_CORRECTION",
            )
        pair(id, owner, second, first)
        sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", original)
        return id
    }

    private fun Connection.repost(
        owner: UUID,
        original: UUID,
        first: UUID,
        second: UUID,
        occurred: OffsetDateTime,
        date: LocalDate?,
        correction: UUID,
    ): UUID {
        val id =
            transaction(
                owner,
                kind = "REPOST",
                replaces = original,
                occurred = occurred,
                valueDate = date,
                correction = correction,
                reason = "USER_CORRECTION",
            )
        pair(id, owner, first, second)
        return id
    }
}
