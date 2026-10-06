package com.pennilogic.migration

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.nio.file.Path

/** Protocol-only test double. It does not classify SQL or constitute real policy/provider acceptance. */
internal object AdmissionFixtures {
    val policy: JsonObject = buildJsonObject { put("commit", "a".repeat(40)) }
    val inventory: JsonObject = buildJsonObject { put("source", buildJsonObject { put("commit", "b".repeat(40)) }) }

    fun create(): DatabaseAdmission = DatabaseAdmission(policy, inventory, ::accept)

    fun load(directory: Path): MigrationSet = MigrationSet.load(directory, ::create)

    fun accept(
        command: String,
        request: ByteArray,
    ): AdmissionOutput = AdmissionOutput(0, response(command, request).toString().toByteArray(Charsets.UTF_8))

    fun response(
        command: String,
        request: ByteArray,
    ): JsonObject {
        val data = AdmissionJson.parse(request, DatabaseAdmission.REQUEST_LIMIT).jsonObject
        val migrations = data.getValue("migrations").jsonArray.map { it.jsonObject }
        val scripts =
            data.getValue("selection").jsonArray.map { choice ->
                val selection = choice.jsonObject
                val id = selection.admissionString("id")
                val index = migrations.indexOfFirst { it.admissionString("id") == id }
                val direction = selection.admissionString("direction")
                val sql =
                    if (direction == "up") {
                        migrations[index].admissionString("up")
                    } else {
                        migrations[index].getValue("reverse").jsonObject.admissionString("sql")
                    }
                buildJsonObject {
                    put("migration_index", index)
                    put("direction", direction)
                    put("sha256", Checksum.of(sql))
                }
            }
        return buildJsonObject {
            put("schema", "pennilogic.database-admission.result/1")
            put("gate", "T-PLT-01-DATABASE-EXTENSION-GATE")
            put("command", command)
            put("decision", "ADMIT")
            put("reason", JsonNull)
            put("scope", "DATABASE_EXTENSIONS_AND_DECLARED_COLUMN_SEMANTICS")
            put("plan_sha256", Checksum.of(JsonObject(data - "plan_sha256").toString()))
            put("scripts", JsonArray(scripts))
            put("policy_commit", policy.getValue("commit"))
            put("inventory_commit", inventory.getValue("source").jsonObject.getValue("commit"))
            put("provider_activation", false)
            put("sql_executed", false)
        }
    }

    fun deny(
        command: String,
        request: ByteArray,
    ): AdmissionOutput {
        val result =
            response(command, request).toMutableMap().apply {
                put("decision", kotlinx.serialization.json.JsonPrimitive("DENY"))
                put("reason", kotlinx.serialization.json.JsonPrimitive("SYNTHETIC_TEST_DENIAL"))
                put("plan_sha256", JsonNull)
                put("scripts", JsonArray(emptyList()))
                put("policy_commit", JsonNull)
                put("inventory_commit", JsonNull)
            }
        return AdmissionOutput(1, JsonObject(result).toString().toByteArray(Charsets.UTF_8))
    }
}
