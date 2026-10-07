package com.pennilogic.interop

import com.pennilogic.contracts.models.SyntheticEnvelope
import com.pennilogic.contracts.serialization.PennilogicJson
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.io.IOException
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import kotlin.system.exitProcess

object MoneyClientInteropGenerated {
    private val json = PennilogicJson.json

    @JvmStatic
    fun main(args: Array<String>) {
        try {
            require(args.size == 3 && args[0] in setOf("convert", "disagree", "reject"))
            val operation = args[0]
            val input = Json.parseToJsonElement(Files.readString(Path.of(args[1]))).jsonObject
            val cases = input.getValue("cases").jsonArray
            require(cases.size in 1..10030)
            val converted =
                cases.mapIndexed { index, element ->
                    val row = element.jsonObject
                    val original = row.getValue("envelope")
                    val output =
                        if (operation == "reject") {
                            var rejected = false
                            try {
                                json.decodeFromJsonElement(SyntheticEnvelope.serializer(), original)
                            } catch (_: SerializationException) {
                                rejected = true
                            } catch (_: IllegalArgumentException) {
                                rejected = true
                            }
                            check(rejected) { "invalid-accepted" }
                            buildJsonObject {
                                put("id", row.getValue("id"))
                                put("status", "rejected")
                            }
                        } else {
                            val model = json.decodeFromJsonElement(SyntheticEnvelope.serializer(), original)
                            // A test-only fault in the real generated data model, not a patched codec.
                            val result =
                                if (operation == "disagree" && index == 0) {
                                    val other = json.decodeFromJsonElement(SyntheticEnvelope.serializer(), cases[1].jsonObject.getValue("envelope"))
                                    model.copy(total = other.total)
                                } else {
                                    model
                                }
                            buildJsonObject {
                                put("id", row.getValue("id"))
                                put("envelope", json.encodeToJsonElement(SyntheticEnvelope.serializer(), result))
                            }
                        }
                    output
                }
            val result =
                JsonObject(
                    input + ("cases" to JsonArray(converted)) +
                        if (operation == "reject") mapOf("schema" to JsonPrimitive("pennilogic.api-money-client-rejections/1")) else emptyMap(),
                )
            Files.writeString(Path.of(args[2]), "$result\n", StandardOpenOption.CREATE_NEW)
            println(
                buildJsonObject {
                    put("event", "money_client_conversion")
                    put("status", "passed")
                    put("target", "kotlin")
                    put("operation", operation)
                    put("case_count", converted.size)
                    put("run_id", input.getValue("run_id"))
                },
            )
        } catch (_: IllegalArgumentException) {
            fail()
        } catch (_: IllegalStateException) {
            fail()
        } catch (_: IOException) {
            fail()
        }
    }

    private fun fail(): Nothing {
        System.err.println("""{"event":"money_client_conversion","status":"refused","code":"kotlin-conversion"}""")
        exitProcess(1)
    }
}
