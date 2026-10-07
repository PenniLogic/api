package com.pennilogic.money

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.math.BigInteger

internal sealed interface ExactOutcome<out T> {
    data class Value<T>(
        val result: T,
    ) : ExactOutcome<T>

    data class Rejected(
        val reason: String,
        val field: String,
    ) : ExactOutcome<Nothing>
}

internal data class ExactValue(
    val minorUnits: BigInteger,
    val currency: String,
)

// Test-only interpretation of ADR-001 and ADR-015, with no provider or fixture-answer imports.
internal class ExactMoneyModel(
    private val exponents: Map<String, Int>,
) {
    val limit: BigInteger = BigInteger.ONE.shiftLeft(63).subtract(BigInteger.ONE)

    fun construct(
        coefficient: BigInteger,
        currency: String,
    ): ExactOutcome<ExactValue> =
        when {
            currency !in exponents -> ExactOutcome.Rejected("currency_unknown", "currency")
            coefficient.abs() > limit -> ExactOutcome.Rejected("out_of_range", "amount")
            else -> ExactOutcome.Value(ExactValue(coefficient, currency))
        }

    fun parse(
        text: String,
        currency: String,
    ): ExactOutcome<ExactValue> {
        val negative = text.startsWith('-')
        val unsigned = text.removePrefix("-")
        val pieces = unsigned.split('.')
        val whole = pieces.first()
        val fraction = pieces.getOrNull(1)
        val grammar =
            pieces.size <= 2 &&
                whole.isNotEmpty() &&
                whole.all { it in '0'..'9' } &&
                (whole.length == 1 || whole.first() != '0') &&
                (fraction == null || (fraction.isNotEmpty() && fraction.all { it in '0'..'9' })) &&
                (!negative || unsigned.any { it in '1'..'9' })
        if (!grammar) return ExactOutcome.Rejected("grammar", "amount")
        val exponent = exponents[currency] ?: return ExactOutcome.Rejected("currency_unknown", "currency")
        if ((fraction?.length ?: 0) != exponent) return ExactOutcome.Rejected("scale_mismatch", "amount")
        var coefficient = BigInteger.ZERO
        for (digit in unsigned) {
            if (digit != '.') {
                coefficient = coefficient.multiply(BigInteger.TEN).add(BigInteger.valueOf((digit - '0').toLong()))
            }
        }
        return construct(if (negative) coefficient.negate() else coefficient, currency)
    }

    fun render(value: ExactValue): String {
        val exponent = exponents.getValue(value.currency)
        val parts = value.minorUnits.abs().divideAndRemainder(BigInteger.TEN.pow(exponent))
        val unsigned =
            if (exponent == 0) {
                parts[0].toString()
            } else {
                "${parts[0]}.${parts[1].toString().padStart(exponent, '0')}"
            }
        return if (value.minorUnits.signum() < 0) "-$unsigned" else unsigned
    }

    fun add(
        left: ExactValue,
        right: ExactValue,
    ): ExactOutcome<ExactValue> =
        if (left.currency != right.currency) {
            ExactOutcome.Rejected("currency_mismatch", "currency")
        } else {
            construct(left.minorUnits.add(right.minorUnits), left.currency)
        }

    fun subtract(
        left: ExactValue,
        right: ExactValue,
    ): ExactOutcome<ExactValue> =
        if (left.currency != right.currency) {
            ExactOutcome.Rejected("currency_mismatch", "currency")
        } else {
            construct(left.minorUnits.subtract(right.minorUnits), left.currency)
        }

    fun negate(value: ExactValue): ExactOutcome<ExactValue> = construct(value.minorUnits.negate(), value.currency)

    fun order(
        left: ExactValue,
        right: ExactValue,
    ): ExactOutcome<Int> =
        if (left.currency != right.currency) {
            ExactOutcome.Rejected("currency_mismatch", "currency")
        } else {
            ExactOutcome.Value(left.minorUnits.compareTo(right.minorUnits))
        }

    fun fromWire(document: JsonElement): ExactOutcome<ExactValue> {
        if (document !is JsonObject) return ExactOutcome.Rejected("shape", "")
        val fields = listOf("amount", "currency")
        fields.firstOrNull { it !in document }?.let { return ExactOutcome.Rejected("shape", it) }
        document.keys.firstOrNull { it !in fields }?.let { return ExactOutcome.Rejected("shape", it) }
        val members = fields.map { it to document.getValue(it) }
        members
            .firstOrNull { (_, node) ->
                node !is JsonPrimitive ||
                    node == JsonNull ||
                    (!node.isString && node.content in setOf("true", "false"))
            }?.let { return ExactOutcome.Rejected("shape", it.first) }
        members
            .firstOrNull { (_, node) -> node is JsonPrimitive && !node.isString }
            ?.let { return ExactOutcome.Rejected("number_not_string", it.first) }
        return parse(
            (document.getValue("amount") as JsonPrimitive).content,
            (document.getValue("currency") as JsonPrimitive).content,
        )
    }
}
