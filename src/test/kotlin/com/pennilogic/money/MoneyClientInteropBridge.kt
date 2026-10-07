package com.pennilogic.money

import com.pennilogic.contracts.money.Money
import com.pennilogic.contracts.money.MoneySerializer
import com.pennilogic.contracts.money.MoneyWireException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.io.IOException
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.security.MessageDigest
import kotlin.system.exitProcess

/** Test transport only: the authoritative Money implementation is the API's existing dependency. */
object MoneyClientInteropBridge {
    private const val SOURCE = "aa8d90cb98cec9b6dd08c91b3a4d869e47362662"
    private const val CORPUS = "22ed8a8ff3204a8962c10c8fcfada7d20dd6ed147fc2a07f98edef827ea28efe"
    private const val INVALID = "3c95a95c290fd1aad5d58300c301ab3c8c6b3b2883a95fbb586b58336ab1cf1d"
    private const val CANONICAL = "6d3172f611bc69f7d529912f68539a1e66101a1c188710ff2400ceab4fbb1ef6"
    private const val SCHEMA = "pennilogic.api-money-client-wire/1"
    private const val COUNT = 10030
    private const val RECORDED = "2026-09-30T04:52:08.439Z"
    private const val BOOKED = "2026-09-30"

    private class Refusal(
        val code: String,
        val caseId: String? = null,
    ) : RuntimeException(code)

    private fun ensure(
        condition: Boolean,
        code: String,
        caseId: String? = null,
    ) {
        if (!condition) throw Refusal(code, caseId)
    }

    private fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).toHexString()

    private fun document(
        path: Path,
        expected: String? = null,
    ): JsonObject {
        ensure(Files.size(path) in 1..4194304, "document-size")
        val bytes = Files.readAllBytes(path)
        if (expected != null) ensure(digest(bytes) == expected, "fixture-binding")
        return Json.parseToJsonElement(bytes.toString(Charsets.UTF_8)).jsonObject
    }

    private fun id(index: Int): String = "case-" + index.toString().padStart(5, '0')

    private fun envelope(wire: JsonElement): JsonObject =
        buildJsonObject {
            put("total", wire)
            put("recorded_at", RECORDED)
            put("booked_on", BOOKED)
        }

    private fun rows(
        document: JsonObject,
        runId: String,
        fixture: String,
        count: Int,
    ): JsonArray {
        ensure(document.keys == setOf("schema", "run_id", "source_ref", "fixture_sha256", "case_count", "cases"), "transport-shape")
        ensure(document.getValue("schema") == JsonPrimitive(SCHEMA), "transport-version")
        ensure(document.getValue("source_ref") == JsonPrimitive(SOURCE), "transport-source")
        ensure(document.getValue("fixture_sha256") == JsonPrimitive(fixture), "transport-fixture")
        ensure(document.getValue("run_id") == JsonPrimitive(runId), "transport-stale")
        ensure(document.getValue("case_count") == JsonPrimitive(count), "transport-count")
        val cases = document.getValue("cases").jsonArray
        ensure(cases.size == count && count > 0, "transport-count")
        cases.forEachIndexed { index, element ->
            val row = element.jsonObject
            ensure(row.keys == setOf("id", "envelope") && row.getValue("id") == JsonPrimitive(id(index)), "case-order", id(index))
        }
        return cases
    }

    private fun verifyValue(
        vector: JsonObject,
        wire: JsonElement,
        index: Int,
        fold: MessageDigest,
    ) {
        val instance = Json.decodeFromJsonElement(MoneySerializer, wire)
        val expected =
            Money.ofMinorUnits(
                vector
                    .getValue("minor_units")
                    .jsonPrimitive.content
                    .toLong(),
                vector
                    .getValue("wire")
                    .jsonObject
                    .getValue("currency")
                    .jsonPrimitive.content,
            )
        ensure(instance == expected, "wire-disagreement", id(index))
        ensure(wire == vector.getValue("wire"), "wire-disagreement", id(index))
        ensure(Json.encodeToJsonElement(MoneySerializer, instance) == wire, "wire-disagreement", id(index))
        val fields = wire.jsonObject
        val line =
            listOf(
                fields.getValue("amount").jsonPrimitive.content,
                instance.currency,
                instance.minorUnits.toString(),
            ).joinToString("|", postfix = "\n")
        fold.update(line.toByteArray(Charsets.UTF_8))
    }

    private fun roundTrip(
        operation: String,
        fixture: Path,
        wireFile: Path,
        runId: String,
    ): Int {
        val corpus = document(fixture, CORPUS)
        ensure(corpus.getValue("schema_version") == JsonPrimitive(1), "fixture-version")
        ensure(corpus.getValue("generated_count") == JsonPrimitive(10000), "fixture-count")
        ensure(corpus.getValue("boundary_count") == JsonPrimitive(30), "fixture-count")
        ensure(corpus.getValue("round_trip_sha256") == JsonPrimitive(CANONICAL), "fixture-digest")
        val values = corpus.getValue("values").jsonArray
        ensure(values.size == COUNT, "fixture-count")
        val fold = MessageDigest.getInstance("SHA-256")
        if (operation == "emit") {
            val cases =
                values.mapIndexed { index, element ->
                    val vector = element.jsonObject
                    val instance =
                        Money.ofMinorUnits(
                            vector
                                .getValue("minor_units")
                                .jsonPrimitive.content
                                .toLong(),
                            vector
                                .getValue("wire")
                                .jsonObject
                                .getValue("currency")
                                .jsonPrimitive.content,
                        )
                    val wire = Json.encodeToJsonElement(MoneySerializer, instance)
                    verifyValue(vector, wire, index, fold)
                    buildJsonObject {
                        put("id", id(index))
                        put("envelope", envelope(wire))
                    }
                }
            val output =
                buildJsonObject {
                    put("schema", SCHEMA)
                    put("run_id", runId)
                    put("source_ref", SOURCE)
                    put("fixture_sha256", CORPUS)
                    put("case_count", COUNT)
                    put("cases", JsonArray(cases))
                }
            Files.writeString(wireFile, "$output\n", StandardOpenOption.CREATE_NEW)
        } else {
            val cases = rows(document(wireFile), runId, CORPUS, COUNT)
            cases.forEachIndexed { index, element ->
                val actual = element.jsonObject.getValue("envelope").jsonObject
                val wire = actual.getValue("total")
                ensure(actual == envelope(wire), "envelope-disagreement", id(index))
                verifyValue(values[index].jsonObject, wire, index, fold)
            }
        }
        ensure(fold.digest().toHexString() == CANONICAL, "canonical-digest")
        return COUNT
    }

    private fun reject(
        fixture: Path,
        wireFile: Path,
        runId: String,
    ): Int {
        val vectors = document(fixture, INVALID).getValue("invalid").jsonArray
        ensure(vectors.size == 41, "fixture-count")
        val cases = rows(document(wireFile), runId, INVALID, vectors.size)
        cases.forEachIndexed { index, element ->
            val vector = vectors[index].jsonObject
            val actual = element.jsonObject.getValue("envelope").jsonObject
            ensure(actual == envelope(vector.getValue("wire")), "invalid-vector-binding", id(index))
            try {
                Json.decodeFromJsonElement(MoneySerializer, actual.getValue("total"))
                throw Refusal("invalid-accepted", id(index))
            } catch (error: MoneyWireException) {
                ensure(error.reason.wireName == vector.getValue("reason").jsonPrimitive.content, "invalid-reason", id(index))
                ensure(error.field == vector.getValue("field").jsonPrimitive.content, "invalid-field", id(index))
            }
        }
        return cases.size
    }

    private fun refused(
        code: String,
        caseId: String? = null,
    ): Nothing {
        System.err.println(
            buildJsonObject {
                put("event", "money_client_backend")
                put("status", "refused")
                put("code", code)
                put("case_id", caseId?.let(::JsonPrimitive) ?: JsonNull)
            },
        )
        exitProcess(1)
    }

    @JvmStatic
    fun main(args: Array<String>) {
        try {
            ensure(args.size == 4 && args[0] in setOf("emit", "consume", "reject"), "arguments")
            ensure(args[3].matches(Regex("[0-9a-f]{32}")), "run-identity")
            val operation = args[0]
            val count =
                if (operation == "reject") {
                    reject(Path.of(args[1]), Path.of(args[2]), args[3])
                } else {
                    roundTrip(operation, Path.of(args[1]), Path.of(args[2]), args[3])
                }
            val canonical: JsonElement = if (operation == "reject") JsonNull else JsonPrimitive(CANONICAL)
            println(
                buildJsonObject {
                    put("event", "money_client_backend")
                    put("status", "passed")
                    put("operation", operation)
                    put("case_count", count)
                    put("run_id", args[3])
                    put("canonical_sha256", canonical)
                },
            )
        } catch (error: Refusal) {
            refused(error.code, error.caseId)
        } catch (_: IllegalArgumentException) {
            refused("backend-input")
        } catch (_: IllegalStateException) {
            refused("backend-shape")
        } catch (_: IOException) {
            refused("backend-io")
        }
    }
}
