package com.pennilogic.money

import com.pennilogic.contracts.money.CurrencyRegistry
import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneyReason
import com.pennilogic.contracts.money.MoneySerializer
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.encoding.AbstractDecoder
import kotlinx.serialization.encoding.AbstractEncoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.modules.EmptySerializersModule
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.opentest4j.AssertionFailedError
import java.lang.reflect.InvocationTargetException
import java.nio.file.Path
import java.security.MessageDigest
import kotlin.io.path.readText
import kotlin.random.Random

class AcceptedMoneySourceTest {
    private val source =
        Path.of(
            requireNotNull(System.getProperty("pennilogic.money.source")) {
                "Accepted Money source fixture location is required"
            },
        )
    private val vectors = readFixture("money-wire-fixtures.v1.json")

    private fun readFixture(name: String): JsonObject =
        Json
            .parseToJsonElement(source.resolve("spec/fixtures").resolve(name).readText())
            .jsonObject

    private fun encoded(value: Money): JsonElement = Json.parseToJsonElement(Json.encodeToString(MoneySerializer, value))

    private fun <T> same(
        expected: T,
        actual: T,
        caseId: String,
    ) {
        assertTrue(expected == actual, caseId)
    }

    @Test
    fun `failed value comparisons report a case id without rendering either value`() {
        val sensitive =
            object {
                override fun toString(): String = error("sensitive-value-rendered")
            }
        val failure = assertThrows(AssertionFailedError::class.java) { same(sensitive, Any(), "synthetic-redaction-probe") }
        assertTrue(failure.message.orEmpty().contains("synthetic-redaction-probe"))
        assertFalse(failure.message.orEmpty().contains("sensitive-value-rendered"))
    }

    @Test
    fun `all accepted wire vectors round trip through the actual serializer`() {
        val rows = vectors.getValue("valid").jsonArray
        assertTrue(rows.isNotEmpty())
        for ((index, row) in rows.withIndex()) {
            val vector = row.jsonObject
            val wire = vector.getValue("wire")
            val instance = Json.decodeFromJsonElement(MoneySerializer, wire)
            val expected =
                vector
                    .getValue("minor_units")
                    .jsonPrimitive.content
                    .toLong()
            same(expected, instance.minorUnits, "accepted-$index-units")
            same(wire, encoded(instance), "accepted-$index-wire")
            same(instance, Money.ofMinorUnits(expected, instance.currency), "accepted-$index-value")
        }
    }

    @Test
    fun `all accepted invalid wire vectors report the exact reason and field`() {
        val rows = vectors.getValue("invalid").jsonArray
        assertTrue(rows.isNotEmpty())
        for (row in rows) {
            val vector = row.jsonObject
            val error =
                assertThrows(MoneyWireException::class.java) {
                    Json.decodeFromJsonElement(MoneySerializer, vector.getValue("wire"))
                }
            assertEquals(vector.getValue("reason").jsonPrimitive.content, error.reason.wireName)
            assertEquals(vector.getValue("field").jsonPrimitive.content, error.field)
        }
    }

    @Test
    fun `accepted parsing fixtures preserve grammar currency scale range precedence`() {
        for (row in vectors.getValue("parse_invalid").jsonArray) {
            val vector = row.jsonObject
            val error =
                assertThrows(MoneyWireException::class.java) {
                    Money.parse(
                        vector.getValue("amount").jsonPrimitive.content,
                        vector.getValue("currency").jsonPrimitive.content,
                    )
                }
            assertEquals(vector.getValue("reason").jsonPrimitive.content, error.reason.wireName)
        }
    }

    @Test
    fun `ten thousand seeded and thirty boundary values retain canonical bytes and exact minor units`() {
        val fixture = readFixture("money-roundtrip-generated.v1.json")
        assertEquals("10000", fixture.getValue("generated_count").jsonPrimitive.content)
        assertEquals("30", fixture.getValue("boundary_count").jsonPrimitive.content)
        val rows = fixture.getValue("values").jsonArray
        assertEquals(10030, rows.size)
        val digest = MessageDigest.getInstance("SHA-256")
        for ((index, row) in rows.withIndex()) {
            val vector = row.jsonObject
            val wire = vector.getValue("wire")
            val instance = Json.decodeFromJsonElement(MoneySerializer, wire)
            val expected =
                vector
                    .getValue("minor_units")
                    .jsonPrimitive.content
                    .toLong()
            val output = encoded(instance)
            same(expected, instance.minorUnits, "round-trip-$index-units")
            same(wire.toString(), output.toString(), "round-trip-$index-wire")
            same(instance, Json.decodeFromJsonElement(MoneySerializer, output), "round-trip-$index-value")
            val members = output.jsonObject
            val line =
                listOf(
                    members.getValue("amount").jsonPrimitive.content,
                    members.getValue("currency").jsonPrimitive.content,
                    instance.minorUnits.toString(),
                ).joinToString("|", postfix = "\n")
            digest.update(line.toByteArray(Charsets.UTF_8))
        }
        assertEquals(fixture.getValue("round_trip_sha256").jsonPrimitive.content, digest.digest().toHexString())
        println("""{"event":"money_property_cases","property":"round_trip","count":${rows.size}}""")
    }

    @Test
    fun `shared registry entries are complete and exponents come from accepted data`() {
        val registry =
            Json
                .parseToJsonElement(source.resolve("spec/currency-registry.v1.json").readText())
                .jsonObject
                .getValue("currencies")
                .jsonArray
        val expectedCodes =
            registry
                .map {
                    it.jsonObject
                        .getValue("code")
                        .jsonPrimitive.content
                }.toSet()
        assertEquals(expectedCodes, CurrencyRegistry.entries.keys)
        for (row in registry) {
            val entry = row.jsonObject
            val code = entry.getValue("code").jsonPrimitive.content
            val exponent =
                entry
                    .getValue("exponent")
                    .jsonPrimitive.content
                    .toInt()
            val provided = requireNotNull(CurrencyRegistry.entries[code])
            assertEquals(code, provided.code)
            assertEquals(exponent, provided.exponent)
            assertEquals(entry.getValue("minor_unit_name").jsonPrimitive.content, provided.minorUnitName)
            assertEquals(exponent, CurrencyRegistry.exponentOf(code))
            val instance = Money.ofMinorUnits(1, code)
            val text =
                encoded(instance)
                    .jsonObject
                    .getValue("amount")
                    .jsonPrimitive.content
            assertEquals(exponent, text.substringAfter('.', "").length)
            same(instance, Money.parse(text, code), "registry-$code-value")
        }
        same(
            "1",
            encoded(Money.ofMinorUnits(1, "JPY"))
                .jsonObject
                .getValue("amount")
                .jsonPrimitive.content,
            "registry-integral-render",
        )
        same(
            "0.001",
            encoded(Money.ofMinorUnits(1, "KWD"))
                .jsonObject
                .getValue("amount")
                .jsonPrimitive.content,
            "registry-three-digit-render",
        )
        assertEquals(null, CurrencyRegistry.exponentOf("XYZ"))
    }

    @Test
    fun `canonical integer conversion makes the decimal tenth sum exact`() {
        val first = Money.parse("0.10", "INR")
        val second = Money.parse("0.20", "INR")
        val total = first + second
        same(30L, total.minorUnits, "decimal-tenth-units")
        same(
            "0.30",
            encoded(total)
                .jsonObject
                .getValue("amount")
                .jsonPrimitive.content,
            "decimal-tenth-render",
        )
        assertEquals(MoneyReason.SCALE_MISMATCH, assertThrows(MoneyWireException::class.java) { Money.parse("0.1", "INR") }.reason)
    }

    @Test
    fun `addition is associative and exact over bounded synthetic integer inputs`() {
        val random = Random(72131)
        val codes = CurrencyRegistry.entries.keys.sorted()
        val bound = Long.MAX_VALUE / 4
        var cases = 0
        repeat(10000) { index ->
            val code = codes[index % codes.size]
            val firstInput = random.nextLong(-bound, bound)
            val secondInput = random.nextLong(-bound, bound)
            val thirdInput = random.nextLong(-bound, bound)
            val first = Money.ofMinorUnits(firstInput, code)
            val second = Money.ofMinorUnits(secondInput, code)
            val third = Money.ofMinorUnits(thirdInput, code)
            val expected = Math.addExact(firstInput, secondInput)
            val combined = first + second
            same((first + second) + third, first + (second + third), "associativity-$index")
            same(expected, combined.minorUnits, "addition-$index")
            same(first, (first + second) - second, "subtraction-$index")
            same(first, -(-first), "negation-$index")
            cases += 1
        }
        println("""{"event":"money_property_cases","property":"associativity","count":$cases}""")
    }

    @Test
    fun `mixed currencies refuse arithmetic and ordering immediately`() {
        val first = Money.ofMinorUnits(1, "INR")
        val second = Money.ofMinorUnits(1, "JPY")
        assertThrows(IllegalArgumentException::class.java) { first + second }
        assertThrows(IllegalArgumentException::class.java) { first - second }
        assertThrows(IllegalArgumentException::class.java) { first.compareTo(second) }
        assertFalse(first == second, "distinct-currency-equality")
        assertFalse(first == Money.ofMinorUnits(2, "INR"), "distinct-value-equality")
        assertFalse(first.equals(1L), "distinct-type-equality")
        same(first.hashCode(), Money.ofMinorUnits(1, "INR").hashCode(), "equal-value-hashes")
        assertTrue(first < Money.ofMinorUnits(2, "INR"))
        assertFalse(first > Money.ofMinorUnits(2, "INR"))
    }

    @Test
    fun `accepted symmetric bounds exclude Long minimum at construction and parsing`() {
        for (code in CurrencyRegistry.entries.keys) {
            val maximum = Money.ofMinorUnits(Long.MAX_VALUE, code)
            val minimum = Money.ofMinorUnits(-Long.MAX_VALUE, code)
            same(minimum, -maximum, "negative-bound-$code")
            same(maximum, -minimum, "positive-bound-$code")
            assertEquals(
                MoneyReason.OUT_OF_RANGE,
                assertThrows(MoneyWireException::class.java) { Money.ofMinorUnits(Long.MIN_VALUE, code) }.reason,
            )
        }
        assertEquals(
            MoneyReason.OUT_OF_RANGE,
            assertThrows(MoneyWireException::class.java) { Money.parse("-9223372036854775808", "JPY") }.reason,
        )
        assertThrows(ArithmeticException::class.java) {
            Money.ofMinorUnits(Long.MAX_VALUE, "INR") + Money.ofMinorUnits(1, "INR")
        }
        assertEquals(
            MoneyReason.OUT_OF_RANGE,
            assertThrows(MoneyWireException::class.java) {
                Money.ofMinorUnits(-Long.MAX_VALUE, "INR") - Money.ofMinorUnits(1, "INR")
            }.reason,
        )
        assertThrows(ArithmeticException::class.java) {
            Money.ofMinorUnits(-Long.MAX_VALUE, "INR") - Money.ofMinorUnits(2, "INR")
        }
    }

    @Test
    fun `awkward inputs are rejected according to the accepted grammar and scale`() {
        for (input in listOf(".5", "1.", "1,250.00", "+1.00", "01.00", " 1.00", "1e3", "NaN", "-0.00")) {
            assertEquals(
                MoneyReason.GRAMMAR,
                assertThrows(MoneyWireException::class.java) { Money.parse(input, "INR") }.reason,
            )
        }
        for (input in listOf("85000", "1.005")) {
            assertEquals(
                MoneyReason.SCALE_MISMATCH,
                assertThrows(MoneyWireException::class.java) { Money.parse(input, "INR") }.reason,
            )
        }
        same(85000L, Money.parse("85000", "JPY").minorUnits, "integral-unit-parse")
        same(1005L, Money.parse("1.005", "KWD").minorUnits, "three-digit-parse")
        val expected = -1005L
        same(expected, Money.parse("-1.005", "KWD").minorUnits, "negative-three-digit-parse")
        assertEquals(
            MoneyReason.OUT_OF_RANGE,
            assertThrows(MoneyWireException::class.java) { Money.parse("9".repeat(10000) + ".00", "INR") }.reason,
        )
    }

    @Test
    fun `rejections do not expose an offending amount or unknown currency`() {
        val marker = "synthetic-rejected-amount"
        val malformed = assertThrows(MoneyWireException::class.java) { Money.parse(marker, "INR") }
        assertFalse(malformed.message.orEmpty().contains(marker))
        assertEquals("amount", malformed.field)
        val unknown = assertThrows(MoneyWireException::class.java) { Money.ofMinorUnits(1, "XYZ") }
        assertFalse(unknown.message.orEmpty().contains("XYZ"))
        assertEquals("currency", unknown.field)
    }

    @Test
    fun `the wrapper exposes no implicit division or default rounding operation`() {
        val names =
            Money::class.java.declaredMethods
                .map { it.name }
                .toSet()
        assertFalse(names.any { it in setOf("div", "divide", "times", "round") })
    }

    @Test
    fun `the existing descriptor exposes exactly the two canonical string fields`() {
        val descriptor = MoneySerializer.descriptor
        assertEquals("com.pennilogic.contracts.money.Money", descriptor.serialName)
        assertEquals(2, descriptor.elementsCount)
        assertEquals("amount", descriptor.getElementName(0))
        assertEquals("currency", descriptor.getElementName(1))
        assertEquals(PrimitiveKind.STRING, descriptor.getElementDescriptor(0).kind)
        assertEquals(PrimitiveKind.STRING, descriptor.getElementDescriptor(1).kind)
    }

    @OptIn(ExperimentalSerializationApi::class)
    @Test
    fun `the existing serializer refuses a non JSON encoder before emitting any value`() {
        val encoder =
            object : AbstractEncoder() {
                override val serializersModule = EmptySerializersModule()

                override fun encodeValue(value: Any): Unit = throw AssertionError("The JSON-only serializer must refuse before encoding")
            }
        val instance = Money.ofMinorUnits(1, "INR")
        val error = assertThrows(IllegalStateException::class.java) { MoneySerializer.serialize(encoder, instance) }
        assertEquals("Money is a JSON-only wire type", error.message)
    }

    @OptIn(ExperimentalSerializationApi::class)
    @Test
    fun `the existing serializer refuses a non JSON decoder before reading any value`() {
        val decoder =
            object : AbstractDecoder() {
                override val serializersModule = EmptySerializersModule()

                override fun decodeElementIndex(descriptor: SerialDescriptor): Int =
                    throw AssertionError("The JSON-only serializer must refuse before decoding")

                override fun decodeValue(): Any = throw AssertionError("The JSON-only serializer must refuse before decoding")
            }
        val error = assertThrows(IllegalStateException::class.java) { MoneySerializer.deserialize(decoder) }
        assertEquals("Money is a JSON-only wire type", error.message)
    }

    @Test
    fun `existing diagnostic rendering uses accepted scale without locale or floating conversion`() {
        for (code in CurrencyRegistry.entries.keys) {
            val instance = Money.ofMinorUnits(-1, code)
            val canonical =
                encoded(instance)
                    .jsonObject
                    .getValue("amount")
                    .jsonPrimitive.content
            same("$canonical $code", instance.toString(), "diagnostic-render-$code")
        }
    }

    @Test
    fun `otherwise valid wire objects never admit additional properties`() {
        val canonical = encoded(Money.ofMinorUnits(1, "INR")).jsonObject
        for (key in listOf("extra", "metadata")) {
            val extended = JsonObject(canonical + (key to JsonPrimitive("synthetic-extra")))
            val error = assertThrows(MoneyWireException::class.java) { Money.fromWire(extended) }
            assertEquals(MoneyReason.SHAPE, error.reason)
            assertEquals(key, error.field)
        }
    }

    @Test
    fun `missing members take precedence over competing extra member failures`() {
        val canonical = encoded(Money.ofMinorUnits(1, "INR")).jsonObject
        for (missing in listOf("amount", "currency")) {
            val extended = JsonObject((canonical - missing) + ("extra" to JsonPrimitive("synthetic-extra")))
            val error = assertThrows(MoneyWireException::class.java) { Money.fromWire(extended) }
            assertEquals(MoneyReason.SHAPE, error.reason)
            assertEquals(missing, error.field)
        }
    }

    @OptIn(ExperimentalSerializationApi::class)
    @Test
    fun `JVM serializer null values are rejected before inspecting an encoder`() {
        val encoder =
            object : AbstractEncoder() {
                override val serializersModule = EmptySerializersModule()
            }
        val method = MoneySerializer::class.java.getMethod("serialize", Encoder::class.java, Money::class.java)
        val error =
            assertThrows(InvocationTargetException::class.java) {
                method.invoke(MoneySerializer, encoder, null)
            }
        assertTrue(error.targetException is NullPointerException, "non-null-serializer-boundary")
    }
}
