package com.pennilogic.ledger

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import java.sql.Connection
import java.time.OffsetDateTime
import java.util.UUID

@Tag("postgres")
class LedgerReservationPostgresTest {
    @Test
    fun `reserved groups reject incomplete legs signs ownership and provenance in synthetic widened admission`() =
        checked("reserved-group-negatives") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val shape = connection.exchangeShape()
                connection.autoCommit = false
                connection.group(shape)
                rejected("empty-exchange-group", "23514", "exchange_group_shape") { connection.commit() }
                connection.rollback()
                for (case in listOf("one-leg", "same-currency", "wrong-sign", "no-clearing", "no-customer", "different-instant")) {
                    val group = connection.group(shape)
                    val base = connection.transaction(shape.owner, kind = "EXCHANGE_LEG", occurred = shape.occurred, group = group)
                    val negativeAccount =
                        when (case) {
                            "wrong-sign" -> shape.baseClearing
                            "no-customer" -> shape.ancillaryAccount
                            else -> shape.baseAccount
                        }
                    val positiveAccount = if (case in setOf("wrong-sign", "no-clearing")) shape.baseAccount else shape.baseClearing
                    connection.pair(base, shape.owner, positiveAccount, negativeAccount, Money.ofMinorUnits(100, "INR"))
                    if (case != "one-leg") {
                        val sameCurrency = case == "same-currency"
                        val quote =
                            connection.transaction(
                                shape.owner,
                                currency = if (sameCurrency) "INR" else "JPY",
                                kind = "EXCHANGE_LEG",
                                occurred = if (case == "different-instant") shape.occurred.plusDays(1) else shape.occurred,
                                group = group,
                            )
                        connection.pair(
                            quote,
                            shape.owner,
                            if (sameCurrency) shape.baseAccount else shape.quoteAccount,
                            if (sameCurrency) shape.baseClearing else shape.quoteClearing,
                            Money.ofMinorUnits(1, if (sameCurrency) "INR" else "JPY"),
                        )
                    }
                    rejected("exchange-$case", "23514", "exchange_group_shape") { connection.commit() }
                    connection.rollback()
                }
                val group = connection.group(shape)
                rejected("exchange-other-owner", "23503", "exchange_group_owner") {
                    connection.transaction(ledgerId(), kind = "EXCHANGE_LEG", occurred = shape.occurred, group = group)
                }
                connection.rollback()
                for (rate in listOf(0L, -1L)) {
                    rejected("nonpositive-rate", "23514") { connection.group(shape, rate = rate) }
                    connection.rollback()
                }
                rejected("missing-feed", "23514", "exchange_rate_feed_pair") {
                    connection.group(shape, rateSource = "REFERENCE_FEED")
                }
                connection.rollback()
                rejected("invented-feed", "23514", "exchange_rate_feed_unavailable") {
                    connection.group(shape, rateSource = "REFERENCE_FEED", feed = "synthetic-unavailable")
                }
                connection.rollback()
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.exchange_groups"), "negative-groups-rolled-back")
                same("0", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "negative-legs-rolled-back")
            }
        }

    @Test
    fun `reserved exchange reversal is paired exact and terminal without an exchange write implementation`() =
        checked("reserved-paired-reversal") {
            val fixture = LedgerFixture()
            fixture.connect().use { connection ->
                val shape = connection.exchangeShape()
                connection.autoCommit = false
                val original = connection.group(shape)
                val base = connection.transaction(shape.owner, kind = "EXCHANGE_LEG", occurred = shape.occurred, group = original)
                val quote =
                    connection.transaction(
                        shape.owner,
                        currency = "JPY",
                        kind = "EXCHANGE_LEG",
                        occurred = shape.occurred,
                        group = original,
                    )
                connection.pair(base, shape.owner, shape.baseClearing, shape.baseAccount, Money.ofMinorUnits(100, "INR"))
                connection.pair(quote, shape.owner, shape.quoteAccount, shape.quoteClearing, Money.ofMinorUnits(1, "JPY"))
                connection.commit()
                val unpaired =
                    connection.transaction(
                        shape.owner,
                        kind = "REVERSAL",
                        occurred = shape.occurred,
                        reverses = base,
                        correction = ledgerId(),
                        reason = "EXCHANGE_REVERSAL",
                    )
                connection.pair(unpaired, shape.owner, shape.baseAccount, shape.baseClearing, Money.ofMinorUnits(100, "INR"))
                connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id = ?", base)
                rejected("unpaired-exchange-reversal", "23514", "exchange_group_reversal") { connection.commit() }
                connection.rollback()
                val reversalGroup = connection.group(shape, reverses = original)
                val correction = ledgerId()
                val baseReversal =
                    connection.transaction(
                        shape.owner,
                        kind = "REVERSAL",
                        occurred = shape.occurred,
                        reverses = base,
                        correction = correction,
                        reason = "EXCHANGE_REVERSAL",
                        group = reversalGroup,
                    )
                val quoteReversal =
                    connection.transaction(
                        shape.owner,
                        currency = "JPY",
                        kind = "REVERSAL",
                        occurred = shape.occurred,
                        reverses = quote,
                        correction = correction,
                        reason = "EXCHANGE_REVERSAL",
                        group = reversalGroup,
                    )
                connection.pair(baseReversal, shape.owner, shape.baseAccount, shape.baseClearing, Money.ofMinorUnits(100, "INR"))
                connection.pair(quoteReversal, shape.owner, shape.quoteClearing, shape.quoteAccount, Money.ofMinorUnits(1, "JPY"))
                connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id IN (?, ?)", base, quote)
                connection.commit()
                for ((index, account) in listOf(
                    shape.baseAccount,
                    shape.quoteAccount,
                    shape.baseClearing,
                    shape.quoteClearing,
                ).withIndex()) {
                    same(
                        "0",
                        connection.scalar("SELECT sum(amount_minor)::text FROM pennilogic.entries WHERE account_id = ?", account),
                        "paired-restoration-$index",
                    )
                }
                val terminalGroup = connection.group(shape, reverses = reversalGroup)
                val terminalCorrection = ledgerId()
                val terminalBase =
                    connection.transaction(
                        shape.owner,
                        kind = "REVERSAL",
                        occurred = shape.occurred,
                        reverses = baseReversal,
                        correction = terminalCorrection,
                        reason = "EXCHANGE_REVERSAL",
                        group = terminalGroup,
                    )
                val terminalQuote =
                    connection.transaction(
                        shape.owner,
                        currency = "JPY",
                        kind = "REVERSAL",
                        occurred = shape.occurred,
                        reverses = quoteReversal,
                        correction = terminalCorrection,
                        reason = "EXCHANGE_REVERSAL",
                        group = terminalGroup,
                    )
                connection.pair(terminalBase, shape.owner, shape.baseClearing, shape.baseAccount, Money.ofMinorUnits(100, "INR"))
                connection.pair(terminalQuote, shape.owner, shape.quoteAccount, shape.quoteClearing, Money.ofMinorUnits(1, "JPY"))
                connection.sql("UPDATE pennilogic.transactions SET status = 'reversed' WHERE id IN (?, ?)", baseReversal, quoteReversal)
                rejected("terminal-exchange-group", "23514", "exchange_group_reversal") { connection.commit() }
                connection.rollback()
                same("8", connection.scalar("SELECT count(*) FROM pennilogic.entries"), "paired-history-retained")
            }
        }

    private fun Connection.exchangeShape(): Shape {
        sql("INSERT INTO pennilogic.ledger_currencies VALUES ('JPY', ?, ?)", CurrencyRegistry.exponentOf("JPY"), ledgerNow())
        val owner = ledgerId()
        return Shape(
            owner,
            account(owner),
            account(owner, currency = "JPY"),
            account(owner, "CLEARING", systemRole = "FX_CLEARING"),
            account(owner, "CLEARING", currency = "JPY", systemRole = "FX_CLEARING"),
            account(owner, "EXPENSE", systemRole = "FX_FEE"),
            ledgerNow().minusDays(1),
        )
    }

    private fun Connection.group(
        shape: Shape,
        reverses: UUID? = null,
        rate: Long = 10000000000,
        rateSource: String = "USER_ENTERED",
        feed: String? = null,
    ): UUID {
        val id = ledgerId()
        sql(
            """INSERT INTO pennilogic.exchange_groups
                (id, owner_id, base_currency, quote_currency, quoted_rate_e10, rate_source, rate_feed,
                 rate_instant, rounding_side, occurred_at, booked_at, reverses_group_id)
                VALUES (?, ?, 'INR', 'JPY', ?, ?, ?, ?, 'QUOTE', ?, ?, ?)""",
            id,
            shape.owner,
            rate,
            rateSource,
            feed,
            shape.occurred,
            shape.occurred,
            ledgerNow(),
            reverses,
        )
        return id
    }

    private data class Shape(
        val owner: UUID,
        val baseAccount: UUID,
        val quoteAccount: UUID,
        val baseClearing: UUID,
        val quoteClearing: UUID,
        val ancillaryAccount: UUID,
        val occurred: OffsetDateTime,
    )
}
