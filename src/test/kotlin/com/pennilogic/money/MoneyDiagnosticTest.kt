package com.pennilogic.money

import ch.qos.logback.classic.Level
import ch.qos.logback.classic.LoggerContext
import ch.qos.logback.classic.encoder.PatternLayoutEncoder
import ch.qos.logback.classic.spi.ILoggingEvent
import ch.qos.logback.classic.util.LogbackMDCAdapter
import ch.qos.logback.core.OutputStreamAppender
import ch.qos.logback.core.read.ListAppender
import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneyReason
import com.pennilogic.contracts.money.MoneySerializer
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.io.ByteArrayOutputStream
import java.nio.charset.StandardCharsets.UTF_8

class MoneyDiagnosticTest {
    private val marker = "synthetic-account-diagnostic-canary"
    private val decimal = "87654321.09"
    private val canonical = """{"amount":"$decimal","currency":"INR"}"""
    private val sensitive = listOf(marker, decimal, "8765432109", "87654321", "92233720368547758", "9223372036854775807")

    private fun rejected(block: () -> Any): Throwable {
        try {
            block()
        } catch (error: Throwable) {
            return error
        }
        throw AssertionError("diagnostic-control-must-reject")
    }

    private fun captured(projection: MoneyDiagnostic): String {
        val output = ByteArrayOutputStream()
        val context = LoggerContext()
        context.mdcAdapter = LogbackMDCAdapter()
        val encoder = PatternLayoutEncoder()
        encoder.context = context
        encoder.pattern = "%msg%n%ex"
        encoder.start()
        val appender = OutputStreamAppender<ILoggingEvent>()
        appender.context = context
        appender.encoder = encoder
        appender.outputStream = output
        appender.start()
        val events = ListAppender<ILoggingEvent>()
        events.context = context
        events.start()
        val logger = context.getLogger("money-diagnostic-control")
        logger.level = Level.INFO
        logger.isAdditive = false
        logger.addAppender(appender)
        logger.addAppender(events)
        context.start()
        try {
            assertTrue(logger.isInfoEnabled, "diagnostic-logger-must-be-enabled")
            logger.info("{}", projection.fields())
            assertTrue(events.list.size == 1, "diagnostic-event-count")
            assertTrue(events.list.single().throwableProxy == null, "diagnostic-no-throwable")
        } finally {
            context.stop()
        }
        return output.toString(UTF_8)
    }

    private fun checkProjection(
        projection: MoneyDiagnostic,
        expected: Map<String, String>,
    ) {
        assertTrue(projection.fields() == expected, "diagnostic-static-metadata")
        assertTrue(
            projection.operation.id == expected["operation"] && projection.field.token == expected["field"],
            "diagnostic-typed-fields",
        )
        val output = captured(projection)
        assertTrue(output.trimEnd('\r', '\n') == expected.toString(), "diagnostic-enabled-output")
        assertTrue(sensitive.none { it in output }, "diagnostic-no-sensitive-rendering")
        assertTrue(output.none { it == '\u001b' || it == '\u0000' }, "diagnostic-no-input-controls")
    }

    private fun metadata(
        operation: MoneyOperation,
        outcome: String,
        field: String = "value",
        reason: String? = null,
    ): Map<String, String> =
        linkedMapOf(
            "event" to "money_diagnostic",
            "operation" to operation.id,
            "outcome" to outcome,
            "field" to field,
        ).apply {
            if (reason != null) put("reason", reason)
        }

    @Test
    fun `genuine provider rejections retain the accepted reasons and schema fields`() {
        val cases =
            listOf(
                Triple(MoneyReason.SHAPE, "value") { Money.fromWire(JsonPrimitive(marker)) },
                Triple(MoneyReason.SHAPE, "amount") { Money.fromWire(Json.parseToJsonElement("""{"currency":"INR"}""")) },
                Triple(MoneyReason.SHAPE, "currency") { Money.fromWire(Json.parseToJsonElement("""{"amount":"$decimal"}""")) },
                Triple(MoneyReason.NUMBER_NOT_STRING, "amount") {
                    Money.fromWire(Json.parseToJsonElement("""{"amount":87654321.09,"currency":"INR"}"""))
                },
                Triple(MoneyReason.GRAMMAR, "amount") { Money.parse(marker, "INR") },
                Triple(MoneyReason.CURRENCY_UNKNOWN, "currency") { Money.parse(decimal, marker) },
                Triple(MoneyReason.SCALE_MISMATCH, "amount") { Money.parse("87654321.090", "INR") },
                Triple(MoneyReason.OUT_OF_RANGE, "amount") { Money.parse("92233720368547758.08", "INR") },
            )
        assertTrue(cases.map { it.first }.toSet() == MoneyReason.entries.toSet(), "diagnostic-reason-inventory")
        for ((reason, field, operation) in cases) {
            val error = rejected(operation)
            assertTrue((error as? MoneyWireException)?.reason == reason, "diagnostic-provider-reason")
            val projection = MoneyDiagnostic.failed(MoneyOperation.DECODE, error)
            checkProjection(projection, metadata(MoneyOperation.DECODE, "validation_rejected", field, reason.wireName))
        }
    }

    @Test
    fun `arbitrary extra keys become a fixed token without changing machine fields or check order`() {
        val value = Json.parseToJsonElement(canonical) as JsonObject
        val collisions = mapOf("amount" to "bNount", "currency" to "dVrrency")
        assertTrue(
            collisions.all { (field, collision) -> field != collision && field.hashCode() == collision.hashCode() },
            "diagnostic-field-hash-collision-controls",
        )
        for (key in listOf(marker, "$marker\n\r\t\u001b\u0000", "amount\n$marker", "", marker.repeat(128)) + collisions.values) {
            val extended = JsonObject(value + (key to JsonPrimitive(marker)))
            val error = rejected { Money.fromWire(extended) }
            val rejection = error as? MoneyWireException
            assertTrue(rejection?.reason == MoneyReason.SHAPE && rejection.field == key, "diagnostic-extra-key-contract")
            checkProjection(
                MoneyDiagnostic.failed(MoneyOperation.DECODE, error),
                metadata(MoneyOperation.DECODE, "validation_rejected", reason = "shape"),
            )
            for (missing in listOf("amount", "currency")) {
                val earlier = rejected { Money.fromWire(JsonObject(extended - missing)) }
                assertTrue((earlier as? MoneyWireException)?.field == missing, "diagnostic-missing-before-extra")
                checkProjection(
                    MoneyDiagnostic.failed(MoneyOperation.DECODE, earlier),
                    metadata(MoneyOperation.DECODE, "validation_rejected", missing, "shape"),
                )
            }
        }
    }

    @Test
    fun `actual decoder syntax errors never forward source text or invent a provider reason`() {
        val malformed = canonical.dropLast(1) + ",\"$marker\":\"synthetic\""
        val error = rejected { Json.decodeFromString(MoneySerializer, malformed) }
        assertTrue(error is SerializationException, "diagnostic-actual-decoder-error")
        assertTrue(
            error.message.orEmpty().contains(marker) && error.message.orEmpty().contains(decimal),
            "diagnostic-decoder-before-control",
        )
        checkProjection(MoneyDiagnostic.failed(MoneyOperation.DECODE, error), metadata(MoneyOperation.DECODE, "failed"))
    }

    @Test
    fun `unexpected nested failures are failures and no throwable accessor is needed`() {
        val inner = rejected { Money.parse(marker, "INR") }
        val nested = IllegalStateException(marker, inner)
        nested.addSuppressed(IllegalArgumentException(decimal))
        nested.stackTrace = arrayOf(StackTraceElement(marker, marker, marker, 1))
        val hostile =
            object : RuntimeException() {
                override val message: String get() = throw AssertionError("diagnostic-read-message")

                override val cause: Throwable get() = throw AssertionError("diagnostic-read-cause")

                override fun toString(): String = throw AssertionError("diagnostic-rendered-error")
            }
        for (error in listOf(nested, hostile, AssertionError(marker, nested))) {
            checkProjection(MoneyDiagnostic.failed(MoneyOperation.COMPARE, error), metadata(MoneyOperation.COMPARE, "failed"))
        }
        checkProjection(MoneyDiagnostic.failed(MoneyOperation.COMPARE), metadata(MoneyOperation.COMPARE, "failed"))
    }

    @Test
    fun `arithmetic failures are not relabeled as successful or fabricated codec rejections`() {
        val maximum = Money.ofMinorUnits(Long.MAX_VALUE, "INR")
        val unit = Money.ofMinorUnits(1, "INR")
        val overflow = rejected { maximum + unit }
        assertTrue(overflow is ArithmeticException, "diagnostic-overflow-control")
        checkProjection(MoneyDiagnostic.failed(MoneyOperation.ADD, overflow), metadata(MoneyOperation.ADD, "failed"))
        val mismatch = rejected { unit + Money.ofMinorUnits(1, "JPY") }
        assertTrue(mismatch is IllegalArgumentException && (mismatch as? MoneyWireException) == null, "diagnostic-mismatch-control")
        checkProjection(MoneyDiagnostic.failed(MoneyOperation.ADD, mismatch), metadata(MoneyOperation.ADD, "failed"))
    }

    @Test
    fun `successful genuine operations expose only fixed metadata and keep value bearing transport intact`() {
        val value = Money.parse(decimal, "INR")
        val zero = Money.ofMinorUnits(0, "INR")
        val operations =
            mapOf<MoneyOperation, () -> Any>(
                MoneyOperation.CONSTRUCT to { Money.ofMinorUnits(8765432109, "INR") },
                MoneyOperation.PARSE to { Money.parse(decimal, "INR") },
                MoneyOperation.DECODE to { Json.decodeFromString(MoneySerializer, canonical) },
                MoneyOperation.ENCODE to { Json.encodeToString(MoneySerializer, value) },
                MoneyOperation.ADD to { value + zero },
                MoneyOperation.SUBTRACT to { value - zero },
                MoneyOperation.NEGATE to { -value },
                MoneyOperation.COMPARE to { value.compareTo(zero) },
            )
        assertTrue(operations.keys == MoneyOperation.entries.toSet(), "diagnostic-operation-inventory")
        for ((operation, action) in operations) {
            val result = action()
            when (operation) {
                MoneyOperation.ENCODE -> assertTrue(result == canonical, "diagnostic-canonical-wire-preserved")
                MoneyOperation.NEGATE -> assertTrue(result == -value, "diagnostic-negation-preserved")
                MoneyOperation.COMPARE -> assertTrue(result == 1, "diagnostic-comparison-preserved")
                else -> assertTrue(result == value, "diagnostic-value-preserved")
            }
            val projection = MoneyDiagnostic.succeeded(operation)
            assertTrue(projection.outcome == MoneyDiagnosticOutcome.SUCCEEDED && projection.reason == null, "diagnostic-success")
            checkProjection(projection, metadata(operation, "succeeded"))
        }
        assertTrue(value.toString() == "$decimal INR", "diagnostic-value-rendering-is-not-logging")
    }
}
