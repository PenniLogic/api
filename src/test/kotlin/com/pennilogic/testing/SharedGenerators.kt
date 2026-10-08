package com.pennilogic.testing

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import io.kotest.property.Arb
import io.kotest.property.RandomSource
import io.kotest.property.Sample
import io.kotest.property.Shrinker
import io.kotest.property.arbitrary.LongShrinker
import io.kotest.property.arbitrary.element
import io.kotest.property.arbitrary.long
import io.kotest.property.sampleOf
import java.util.UUID

internal class LedgerInput(
    val value: Money,
    val owner: UUID,
    val first: AccountInput,
    val second: AccountInput,
    val transaction: TransactionInput,
) {
    fun withValue(value: Money): LedgerInput = LedgerInput(value, owner, first, second, transaction)
}

internal object SharedGenerators {
    private val corpus get() = SharedFixtures.corpus
    private val range = -Money.MAX_MINOR_UNITS..Money.MAX_MINOR_UNITS
    private val zero = Money.ofMinorUnits(0, "INR")
    private val acceptedAccounts get() = corpus.accounts.filter { it.expectedInvariant == ExpectedInvariant.ACCOUNT_ADMITTED }
    private val acceptedTransactions get() = corpus.transactions.filter { it.expectedInvariant == ExpectedInvariant.TRANSACTION_ADMITTED }

    fun values(): Arb<FixtureCase<ValueInput>> =
        seeded(
            corpus.values,
            Shrinker { case ->
                when (val input = case.input) {
                    is ValueInput.Refused -> {
                        emptyList()
                    }

                    is ValueInput.Accepted -> {
                        LongShrinker(range).shrink(input.value.minorUnits).mapIndexed { index, units ->
                            FixtureCase(
                                "${case.id}.s$index",
                                ValueInput.Accepted(Money.ofMinorUnits(units, input.value.currency)),
                                case.expectedInvariant,
                            )
                        }
                    }
                }
            },
        ) { source, index ->
            val currency = Arb.element(CurrencyRegistry.entries.keys.sorted()).sample(source).value
            val value = drawValue(source, currency, range)
            FixtureCase("generated-value-$index", ValueInput.Accepted(value), ExpectedInvariant.ROUND_TRIP)
        }

    fun ledger(): Arb<FixtureCase<LedgerInput>> {
        // Zero and invalid identifiers have an explicit rejecting consumer, not discarded property attempts.
        val seeds =
            corpus.values.mapIndexedNotNull { index, case ->
                val input = case.input
                if (input is ValueInput.Accepted && input.value.currency == "INR" && input.value != zero) {
                    FixtureCase(
                        "ledger-${case.id}",
                        ledgerInput(input.value, acceptedTransactions[index % acceptedTransactions.size].input),
                        ExpectedInvariant.ZERO_SUM,
                    )
                } else {
                    null
                }
            }
        return seeded(seeds, ledgerShrinker) { source, index ->
            val value = drawValue(source, "INR", 1L..Money.MAX_MINOR_UNITS)
            val signed = if (source.random.nextBoolean()) value else -value
            val firstType =
                Arb
                    .element(acceptedAccounts)
                    .sample(source)
                    .value.input.type
            val secondType =
                Arb
                    .element(acceptedAccounts)
                    .sample(source)
                    .value.input.type
            val dates =
                Arb
                    .element(acceptedTransactions)
                    .sample(source)
                    .value.input
            val input =
                LedgerInput(
                    signed,
                    uuid(source),
                    AccountInput(uuid(source), firstType),
                    AccountInput(uuid(source), secondType),
                    TransactionInput(uuid(source), dates.occurredAt, dates.valueDate),
                )
            FixtureCase("generated-ledger-$index", input, ExpectedInvariant.ZERO_SUM)
        }
    }

    fun ledgerAdversarial(): Arb<FixtureCase<LedgerInput>> {
        val base = ledgerInput(corpus.acceptedValue("inr-unit"))
        val seeds =
            listOf(FixtureCase("ledger-inr-zero", base.withValue(corpus.acceptedValue("inr-zero")), ExpectedInvariant.ZERO_ENTRY)) +
                corpus.accounts.filter { it.expectedInvariant == ExpectedInvariant.ACCOUNT_UUID }.map { case ->
                    FixtureCase(
                        "ledger-${case.id}",
                        LedgerInput(base.value, base.owner, case.input, base.second, base.transaction),
                        case.expectedInvariant,
                    )
                } +
                corpus.transactions.filter { it.expectedInvariant == ExpectedInvariant.TRANSACTION_UUID }.map { case ->
                    FixtureCase(
                        "ledger-${case.id}",
                        LedgerInput(base.value, base.owner, base.first, base.second, case.input),
                        case.expectedInvariant,
                    )
                }
        return seeded(seeds, Shrinker { emptyList() }) { source, _ -> Arb.element(seeds).sample(source).value }
    }

    private val ledgerShrinker: Shrinker<FixtureCase<LedgerInput>> =
        Shrinker { case ->
            val value: Money = case.input.value
            val negative = value < zero
            val magnitude: Money = if (negative) -value else value
            LongShrinker(1L..Money.MAX_MINOR_UNITS).shrink(magnitude.minorUnits).mapIndexed { index, units ->
                val smaller: Money = Money.ofMinorUnits(units, "INR")
                FixtureCase("${case.id}.s$index", case.input.withValue(if (negative) -smaller else smaller), case.expectedInvariant)
            }
        }

    private fun ledgerInput(
        value: Money,
        transaction: TransactionInput = acceptedTransactions[0].input,
    ): LedgerInput =
        LedgerInput(
            value,
            UUID.fromString("00000000-0000-0000-0000-000000000001"),
            acceptedAccounts[0].input,
            acceptedAccounts[1].input,
            transaction,
        )

    private fun drawValue(
        source: RandomSource,
        currency: String,
        range: LongRange,
    ): Money = Money.ofMinorUnits(Arb.long(range).sample(source).value, currency)

    private fun uuid(source: RandomSource): UUID =
        UUID(
            (source.random.nextLong() and -61441L) or 0x7000L,
            (source.random.nextLong() and 0x3fffffffffffffffL) or Long.MIN_VALUE,
        )

    private fun <T> seeded(
        seeds: List<FixtureCase<T>>,
        shrinker: Shrinker<FixtureCase<T>>,
        generate: (RandomSource, Int) -> FixtureCase<T>,
    ): Arb<FixtureCase<T>> =
        object : Arb<FixtureCase<T>>() {
            private var index = 0

            override fun edgecase(rs: RandomSource): Sample<FixtureCase<T>>? = null

            override fun sample(rs: RandomSource): Sample<FixtureCase<T>> {
                val current = index++
                return sampleOf(seeds.getOrNull(current) ?: generate(rs, current), shrinker)
            }
        }
}
