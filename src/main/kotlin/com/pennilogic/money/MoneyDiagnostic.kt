package com.pennilogic.money

import com.pennilogic.contracts.money.MoneyReason
import com.pennilogic.contracts.money.MoneyWireException

enum class MoneyOperation(
    val id: String,
) {
    CONSTRUCT("construct"),
    PARSE("parse"),
    DECODE("decode"),
    ENCODE("encode"),
    ADD("add"),
    SUBTRACT("subtract"),
    NEGATE("negate"),
    COMPARE("compare"),
}

enum class MoneyDiagnosticOutcome(
    val id: String,
) {
    SUCCEEDED("succeeded"),
    VALIDATION_REJECTED("validation_rejected"),
    FAILED("failed"),
}

enum class MoneyDiagnosticField(
    val token: String,
) {
    AMOUNT("amount"),
    CURRENCY("currency"),
    VALUE("value"),
}

/** Log metadata only: never retain a value, input-derived field, or throwable. */
class MoneyDiagnostic private constructor(
    val operation: MoneyOperation,
    val outcome: MoneyDiagnosticOutcome,
    val reason: MoneyReason?,
    val field: MoneyDiagnosticField,
) {
    fun fields(): Map<String, String> {
        val result =
            linkedMapOf(
                "event" to "money_diagnostic",
                "operation" to operation.id,
                "outcome" to outcome.id,
                "field" to field.token,
            )
        reason?.let { result["reason"] = it.wireName }
        return result
    }

    companion object {
        fun succeeded(operation: MoneyOperation): MoneyDiagnostic =
            MoneyDiagnostic(operation, MoneyDiagnosticOutcome.SUCCEEDED, null, MoneyDiagnosticField.VALUE)

        fun failed(
            operation: MoneyOperation,
            error: Throwable? = null,
        ): MoneyDiagnostic {
            val rejection = error as? MoneyWireException
            return if (rejection != null) {
                MoneyDiagnostic(
                    operation,
                    MoneyDiagnosticOutcome.VALIDATION_REJECTED,
                    rejection.reason,
                    when (rejection.field) {
                        "amount" -> MoneyDiagnosticField.AMOUNT
                        "currency" -> MoneyDiagnosticField.CURRENCY
                        else -> MoneyDiagnosticField.VALUE
                    },
                )
            } else {
                // Decoder and unexpected failures have no provider reason; never infer one from their text or causes.
                MoneyDiagnostic(operation, MoneyDiagnosticOutcome.FAILED, null, MoneyDiagnosticField.VALUE)
            }
        }
    }
}
