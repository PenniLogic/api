package com.pennilogic.money

import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneySerializer
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.opentest4j.AssertionFailedError
import java.math.BigInteger
import java.nio.file.Path
import java.util.Random
import kotlin.io.path.readText

class AcceptedMoneyPrimitiveModelTest {
    private val registry =
        Json
            .parseToJsonElement(
                Path
                    .of(requireNotNull(System.getProperty("pennilogic.money.source")) { "primitive-source-required" })
                    .resolve("spec")
                    .resolve("currency-registry.v1.json")
                    .readText(),
            ).jsonObject
            .getValue("currencies")
            .jsonArray
    private val exponents =
        registry
            .associate {
                it.jsonObject
                    .getValue("code")
                    .jsonPrimitive.content to
                    it.jsonObject
                        .getValue("exponent")
                        .jsonPrimitive.content
                        .toInt()
            }.toSortedMap()
    private val model = ExactMoneyModel(exponents)
    private val codes = exponents.keys.toList()
    private val coefficients = coefficients()
    private val pairs =
        coefficients.flatMapIndexed { index, left ->
            listOf(
                left to coefficients[(index * 17 + 3) % coefficients.size],
                left to left,
                left to left.negate(),
                left to BigInteger.ONE,
                left to BigInteger.ONE.negate(),
            )
        }

    init {
        assertTrue(registry.isNotEmpty() && registry.size == exponents.size, "primitive-registry-inventory")
        assertTrue(exponents.values.all { it >= 0 }, "primitive-registry-exponents")
    }

    private fun coefficients(): List<BigInteger> {
        val values = linkedSetOf(BigInteger.ZERO, model.limit, model.limit.negate())
        val pivots = (0..62).map { BigInteger.ONE.shiftLeft(it) } + (1..18).map { BigInteger.TEN.pow(it) }
        for (pivot in pivots) {
            for (offset in -1..1) {
                val candidate = pivot.add(BigInteger.valueOf(offset.toLong()))
                if (candidate.abs() <= model.limit) {
                    values.add(candidate)
                    values.add(candidate.negate())
                }
            }
        }
        val random = Random(72131015)
        repeat(64) {
            val bytes = ByteArray(8)
            random.nextBytes(bytes)
            val candidate = BigInteger(1, bytes).and(model.limit)
            values.add(candidate)
            values.add(candidate.negate())
        }
        return values.toList()
    }

    private fun wire(
        text: String,
        code: String,
    ): JsonObject = JsonObject(mapOf("amount" to JsonPrimitive(text), "currency" to JsonPrimitive(code)))

    private fun actualValue(value: Money): ExactValue = ExactValue(BigInteger.valueOf(value.minorUnits), value.currency)

    private fun <T> observed(operation: () -> T): ExactOutcome<T> =
        try {
            ExactOutcome.Value(operation())
        } catch (error: MoneyWireException) {
            ExactOutcome.Rejected(error.reason.wireName, error.field)
        }

    private fun <T> observedArithmetic(operation: () -> T): ExactOutcome<T> =
        try {
            observed(operation)
        } catch (_: ArithmeticException) {
            ExactOutcome.Rejected("out_of_range", "amount")
        } catch (_: IllegalArgumentException) {
            ExactOutcome.Rejected("currency_mismatch", "currency")
        }

    private fun comparisons(
        category: String,
        body: (PrimitiveComparisons) -> Unit,
    ) {
        val checks = PrimitiveComparisons(category)
        try {
            body(checks)
            assertTrue(checks.comparisons > 0, "primitive-$category-empty")
        } finally {
            checks.report()
        }
    }

    @Test
    fun constructionMatchesExactModel() =
        comparisons("construction") { checks ->
            for ((currencyIndex, code) in (codes + listOf("XYZ", "", "inr", "INR\n")).withIndex()) {
                for ((index, coefficient) in (coefficients + model.limit.negate().subtract(BigInteger.ONE)).withIndex()) {
                    checks.compare(
                        "primitive-construction-c$currencyIndex-$index",
                        { model.construct(coefficient, code) },
                        { observed { actualValue(Money.ofMinorUnits(coefficient.longValueExact(), code)) } },
                    )
                }
            }
        }

    private fun parsingInputs(code: String): List<String> {
        val exact = coefficients.map { model.render(ExactValue(it, code)) }
        val grammar =
            listOf(
                "",
                "-",
                "+1",
                ".5",
                "1.",
                "01",
                "00.01",
                "1..0",
                "1,000",
                "1e2",
                "NaN",
                "Infinity",
                " 1",
                "1 ",
                "1\n",
                "\u0967",
                "\u22121",
                "\u00a01",
                "1\u0000",
            )
        val scales =
            (0..exponents.values.max() + 1).flatMap { scale ->
                val suffix = if (scale == 0) "" else "." + "0".repeat(scale)
                listOf("1$suffix", "-1$suffix", "-0$suffix")
            }
        val outside =
            listOf(
                model.limit.add(BigInteger.ONE),
                model.limit.add(BigInteger.TWO),
                BigInteger.ONE.shiftLeft(64),
                BigInteger.TEN.pow(128),
            ).flatMap { listOf(it, it.negate()) }
                .map { model.render(ExactValue(it, code)) }
        return exact + grammar + scales + outside
    }

    @Test
    fun parsingMatchesExactModel() =
        comparisons("parsing") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                for ((index, text) in parsingInputs(code).withIndex()) {
                    checks.compare(
                        "primitive-parsing-c$currencyIndex-$index",
                        { model.parse(text, code) },
                        { observed { actualValue(Money.parse(text, code)) } },
                    )
                }
                for ((index, text) in parsingInputs(code).takeLast(40).withIndex()) {
                    checks.compare(
                        "primitive-parsing-unknown-c$currencyIndex-$index",
                        { model.parse(text, "XYZ") },
                        { observed { actualValue(Money.parse(text, "XYZ")) } },
                    )
                }
            }
        }

    @Test
    fun renderingMatchesExactModel() =
        comparisons("rendering") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                for ((index, coefficient) in coefficients.withIndex()) {
                    val reference = ExactValue(coefficient, code)
                    checks.compare(
                        "primitive-rendering-c$currencyIndex-$index",
                        { wire(model.render(reference), code) },
                        {
                            val value = Money.ofMinorUnits(coefficient.longValueExact(), code)
                            Json.parseToJsonElement(Json.encodeToString(MoneySerializer, value))
                        },
                    )
                    checks.compare(
                        "primitive-diagnostic-c$currencyIndex-$index",
                        { "${model.render(reference)} $code" },
                        { Money.ofMinorUnits(coefficient.longValueExact(), code).toString() },
                    )
                }
            }
        }

    @Test
    fun additionMatchesExactModel() =
        comparisons("addition") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                val left = BigInteger.ONE.shiftLeft(53)
                val right = BigInteger.ONE
                checks.compare(
                    "primitive-addition-precision-c$currencyIndex",
                    { model.add(ExactValue(left, code), ExactValue(right, code)) },
                    {
                        observedArithmetic {
                            val first = Money.ofMinorUnits(left.longValueExact(), code)
                            val second = Money.ofMinorUnits(right.longValueExact(), code)
                            actualValue(first + second)
                        }
                    },
                )
                for ((index, pair) in pairs.withIndex()) {
                    checks.compare(
                        "primitive-addition-c$currencyIndex-$index",
                        { model.add(ExactValue(pair.first, code), ExactValue(pair.second, code)) },
                        {
                            observedArithmetic {
                                val first = Money.ofMinorUnits(pair.first.longValueExact(), code)
                                val second = Money.ofMinorUnits(pair.second.longValueExact(), code)
                                actualValue(first + second)
                            }
                        },
                    )
                }
            }
        }

    @Test
    fun subtractionMatchesExactModel() =
        comparisons("subtraction") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                for ((index, pair) in pairs.withIndex()) {
                    checks.compare(
                        "primitive-subtraction-c$currencyIndex-$index",
                        { model.subtract(ExactValue(pair.first, code), ExactValue(pair.second, code)) },
                        {
                            observedArithmetic {
                                val first = Money.ofMinorUnits(pair.first.longValueExact(), code)
                                val second = Money.ofMinorUnits(pair.second.longValueExact(), code)
                                actualValue(first - second)
                            }
                        },
                    )
                }
            }
        }

    @Test
    fun negationMatchesExactModel() =
        comparisons("negation") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                for ((index, coefficient) in coefficients.withIndex()) {
                    checks.compare(
                        "primitive-negation-c$currencyIndex-$index",
                        { model.negate(ExactValue(coefficient, code)) },
                        { observedArithmetic { actualValue(-Money.ofMinorUnits(coefficient.longValueExact(), code)) } },
                    )
                }
            }
        }

    @Test
    fun orderingMatchesExactModel() =
        comparisons("ordering") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                for ((index, pair) in pairs.withIndex()) {
                    checks.compare(
                        "primitive-ordering-c$currencyIndex-$index",
                        { model.order(ExactValue(pair.first, code), ExactValue(pair.second, code)) },
                        {
                            observedArithmetic {
                                val first = Money.ofMinorUnits(pair.first.longValueExact(), code)
                                val second = Money.ofMinorUnits(pair.second.longValueExact(), code)
                                first.compareTo(second).compareTo(0)
                            }
                        },
                    )
                    checks.compare(
                        "primitive-equality-c$currencyIndex-$index",
                        { pair.first == pair.second },
                        {
                            Money.ofMinorUnits(pair.first.longValueExact(), code) ==
                                Money.ofMinorUnits(pair.second.longValueExact(), code)
                        },
                    )
                }
            }
        }

    @Test
    fun mixedCurrenciesMatchExactModel() =
        comparisons("mixed_currency") { checks ->
            for ((leftIndex, leftCode) in codes.withIndex()) {
                for ((rightIndex, rightCode) in codes.withIndex()) {
                    if (leftCode == rightCode) continue
                    for ((index, coefficient) in coefficients.withIndex()) {
                        val left = ExactValue(coefficient, leftCode)
                        val right = ExactValue(coefficient, rightCode)
                        val id = "primitive-mixed-c$leftIndex-c$rightIndex-$index"
                        checks.compare("$id-add", { model.add(left, right) }, {
                            observedArithmetic {
                                val first = Money.ofMinorUnits(coefficient.longValueExact(), leftCode)
                                val second = Money.ofMinorUnits(coefficient.longValueExact(), rightCode)
                                actualValue(first + second)
                            }
                        })
                        checks.compare("$id-subtract", { model.subtract(left, right) }, {
                            observedArithmetic {
                                val first = Money.ofMinorUnits(coefficient.longValueExact(), leftCode)
                                val second = Money.ofMinorUnits(coefficient.longValueExact(), rightCode)
                                actualValue(first - second)
                            }
                        })
                        checks.compare("$id-order", { model.order(left, right) }, {
                            observedArithmetic {
                                val first = Money.ofMinorUnits(coefficient.longValueExact(), leftCode)
                                val second = Money.ofMinorUnits(coefficient.longValueExact(), rightCode)
                                first.compareTo(second).compareTo(0)
                            }
                        })
                        checks.compare("$id-equality", { left == right }, {
                            Money.ofMinorUnits(coefficient.longValueExact(), leftCode) ==
                                Money.ofMinorUnits(coefficient.longValueExact(), rightCode)
                        })
                    }
                }
            }
        }

    @Test
    fun wireMatchesExactModel() =
        comparisons("wire") { checks ->
            for ((currencyIndex, code) in codes.withIndex()) {
                val canonical = wire(model.render(ExactValue(BigInteger.ONE, code)), code)
                val nodes = listOf("null", "true", "false", "[]", "{}", "1", "1.5", "1e99").map(Json::parseToJsonElement)
                val shapes =
                    nodes + JsonPrimitive("synthetic-wire") +
                        listOf(
                            JsonObject(canonical - "amount"),
                            JsonObject(canonical - "currency"),
                            JsonObject(canonical + ("extra" to JsonPrimitive("synthetic-extra"))),
                            JsonObject((canonical - "currency") + ("extra" to JsonPrimitive("synthetic-extra"))),
                        ) +
                        listOf("amount", "currency").flatMap { field ->
                            nodes.map { JsonObject(canonical + (field to it)) }
                        } +
                        listOf(
                            JsonObject(
                                canonical + ("amount" to Json.parseToJsonElement("1")) + ("currency" to Json.parseToJsonElement("null")),
                            ),
                        )
                val documents = parsingInputs(code).map { wire(it, code) } + shapes
                for ((index, document) in documents.withIndex()) {
                    checks.compare(
                        "primitive-wire-c$currencyIndex-$index",
                        { model.fromWire(document) },
                        { observed { actualValue(Money.fromWire(document)) } },
                    )
                    checks.compare(
                        "primitive-serializer-c$currencyIndex-$index",
                        { model.fromWire(document) },
                        { observed { actualValue(Json.decodeFromJsonElement(MoneySerializer, document)) } },
                    )
                }
            }
        }

    @Test
    fun comparisonFailuresRedactValuesAndThrowables() {
        val marker = "synthetic-private-marker"
        val sensitive =
            object {
                override fun toString(): String = throw AssertionError(marker)
            }
        val checks = PrimitiveComparisons("diagnostic")
        val failure =
            assertThrows(AssertionFailedError::class.java) {
                checks.compare("primitive-redaction-values", { sensitive }, { Any() })
            }
        assertTrue(failure.message == "primitive-redaction-values")
        assertFalse(failure.isExpectedDefined)
        assertFalse(failure.isActualDefined)
        assertFalse(failure.stackTraceToString().contains(marker))
        for (side in 0..1) {
            val id = "primitive-redaction-throwable-$side"
            val thrown =
                assertThrows(AssertionFailedError::class.java) {
                    checks.compare(
                        id,
                        { if (side == 0) throw Error(marker, IllegalStateException(marker)) else sensitive },
                        { if (side == 1) throw Error(marker, IllegalStateException(marker)) else sensitive },
                    )
                }
            assertTrue(thrown.message == id)
            assertTrue(thrown.cause == null)
            assertFalse(thrown.stackTraceToString().contains(marker))
        }
        val throwingEquality =
            object {
                override fun equals(other: Any?): Boolean = throw Error(marker)

                override fun hashCode(): Int = 0

                override fun toString(): String = throw Error(marker)
            }
        val equalityFailure =
            assertThrows(AssertionFailedError::class.java) {
                checks.compare("primitive-redaction-equality", { throwingEquality }, { Any() })
            }
        assertTrue(equalityFailure.message == "primitive-redaction-equality")
        assertFalse(equalityFailure.stackTraceToString().contains(marker))
        val duplicate =
            assertThrows(AssertionFailedError::class.java) {
                checks.compare("primitive-redaction-values", { sensitive }, { sensitive })
            }
        assertTrue(duplicate.message == "primitive-duplicate-case-id")
        assertTrue(checks.comparisons == 4 && checks.disagreements == 4, "primitive-redaction-counts")
    }

    private class PrimitiveComparisons(
        private val category: String,
    ) {
        private val caseIdPattern = Regex("primitive-[a-z0-9-]+")
        private val identities = mutableSetOf<String>()
        var comparisons = 0
            private set
        var disagreements = 0
            private set

        fun <T> compare(
            caseId: String,
            expected: () -> T,
            actual: () -> T,
        ) {
            assertTrue(caseId.matches(caseIdPattern), "primitive-static-case-id")
            if (!identities.add(caseId)) throw AssertionFailedError("primitive-duplicate-case-id")
            // Both calculations and equality are inside the redaction boundary; no throwable is retained.
            val agrees =
                try {
                    expected() == actual()
                } catch (_: Throwable) {
                    false
                }
            comparisons += 1
            if (!agrees) {
                disagreements += 1
                throw AssertionFailedError(caseId)
            }
        }

        fun report() {
            println(
                """{"event":"money_primitive_model","category":"$category","comparisons":$comparisons,"disagreements":$disagreements}""",
            )
        }
    }
}
