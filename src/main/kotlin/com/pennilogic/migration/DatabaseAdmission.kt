package com.pennilogic.migration

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlin.contracts.ExperimentalContracts
import kotlin.contracts.contract

/** Only static codes cross the CLI boundary; no provider output, SQL, source payload or exception text. */
class AdmissionRefused internal constructor(
    reason: AdmissionReason,
) : IllegalStateException(reason.name) {
    val code: String = reason.name
}

internal enum class AdmissionReason {
    SOURCE_UNAVAILABLE,
    SOURCE_INVALID,
    SOURCE_CHANGED,
    MIGRATIONS_CHANGED,
    INPUT_INVALID,
    JSON_SIZE,
    JSON_INVALID,
    PROCESS_START,
    PROCESS_IO,
    PROCESS_TIMEOUT,
    PROCESS_INTERRUPTED,
    PROCESS_CLEANUP,
    OUTPUT_SIZE,
    OUTPUT_INVALID,
    PROVIDER_DENIED,
}

@OptIn(ExperimentalContracts::class)
internal fun admissionRequire(
    condition: Boolean,
    reason: AdmissionReason = AdmissionReason.JSON_INVALID,
) {
    contract { returns() implies condition }
    if (!condition) throw AdmissionRefused(reason)
}

internal data class AdmissionOutput(
    val exitCode: Int,
    val stdout: ByteArray,
)

internal fun interface AdmissionTransport {
    fun invoke(
        command: String,
        request: ByteArray,
    ): AdmissionOutput
}

internal data class AdmissionSelection(
    val migration: Migration,
    val direction: String,
) {
    val sql: String get() = if (direction == "up") migration.sql else migration.reversal.sql
}

/** The provider alone classifies SQL and provenance; this adapter binds its result to the actual runner buffers. */
internal class DatabaseAdmission(
    private val policy: JsonElement,
    private val inventory: JsonElement,
    private val transport: AdmissionTransport,
) {
    fun validate(migrations: List<Migration>) {
        val request = request(migrations, emptyList())
        invoke("plan", request, JsonArray(emptyList()))
    }

    fun authorize(
        migrations: List<Migration>,
        selection: List<AdmissionSelection>,
    ) {
        val request = request(migrations, selection)
        val scripts =
            JsonArray(
                selection.map { item ->
                    val index = migrations.indexOfFirst { it === item.migration }
                    admissionRequire(index >= 0, AdmissionReason.INPUT_INVALID)
                    buildJsonObject {
                        put("migration_index", index)
                        put("direction", item.direction)
                        put("sha256", Checksum.of(item.sql))
                    }
                },
            )
        val plan = invoke("plan", request, scripts)
        val apply = JsonObject(request + ("plan_sha256" to JsonPrimitive(plan)))
        admissionRequire(invoke("admit-apply", apply, scripts) == plan, AdmissionReason.OUTPUT_INVALID)
    }

    private fun request(
        migrations: List<Migration>,
        selection: List<AdmissionSelection>,
    ): JsonObject {
        admissionRequire(migrations.size <= 128 && selection.size <= 128, AdmissionReason.INPUT_INVALID)
        admissionRequire(selection.map { it.migration.id }.distinct().size == selection.size, AdmissionReason.INPUT_INVALID)
        for (item in selection) {
            admissionRequire(
                item.direction == "up" || item.direction == item.migration.reversal.kind.directive,
                AdmissionReason.INPUT_INVALID,
            )
        }
        return buildJsonObject {
            put("schema", "pennilogic.database-admission.request/1")
            put("runtime", RUNTIME)
            put("policy", policy)
            put("inventory", inventory)
            put("packages", JsonArray(emptyList()))
            put(
                "migrations",
                JsonArray(
                    migrations.map { migration ->
                        for (sql in listOf(migration.sql, migration.reversal.sql)) {
                            admissionRequire(
                                sql.isNotEmpty() && '\u0000' !in sql && sql.toByteArray(Charsets.UTF_8).size <= SQL_LIMIT &&
                                    "\r\n" !in sql,
                                AdmissionReason.INPUT_INVALID,
                            )
                        }
                        admissionRequire(
                            migration.checksum == Checksum.of(migration.sql) &&
                                migration.reversal.checksum == Checksum.of(migration.reversal.sql),
                            AdmissionReason.INPUT_INVALID,
                        )
                        buildJsonObject {
                            put("id", migration.id)
                            put("up", migration.sql)
                            put(
                                "reverse",
                                buildJsonObject {
                                    put("direction", migration.reversal.kind.directive)
                                    put("sql", migration.reversal.sql)
                                },
                            )
                        }
                    },
                ),
            )
            put(
                "selection",
                JsonArray(
                    selection.map { item ->
                        buildJsonObject {
                            put("id", item.migration.id)
                            put("direction", item.direction)
                        }
                    },
                ),
            )
        }
    }

    private fun invoke(
        command: String,
        request: JsonObject,
        scripts: JsonArray,
    ): String {
        val bytes = request.toString().toByteArray(Charsets.UTF_8)
        admissionRequire(bytes.size <= REQUEST_LIMIT, AdmissionReason.INPUT_INVALID)
        val output = transport.invoke(command, bytes)
        val result = AdmissionJson.parse(output.stdout, OUTPUT_LIMIT).admissionObject(RESULT_FIELDS)
        admissionRequire(
            result.admissionString("schema") == "pennilogic.database-admission.result/1" &&
                result.admissionString("gate") == "T-PLT-01-DATABASE-EXTENSION-GATE" &&
                result.admissionString("command") == command &&
                result.admissionString("scope") == "DATABASE_EXTENSIONS_AND_DECLARED_COLUMN_SEMANTICS" &&
                result["provider_activation"] == JsonPrimitive(false) &&
                result["sql_executed"] == JsonPrimitive(false),
            AdmissionReason.OUTPUT_INVALID,
        )
        when (result.admissionString("decision")) {
            "DENY" -> {
                admissionRequire(
                    output.exitCode == 1 && result["plan_sha256"] == JsonNull &&
                        result["scripts"] == JsonArray(emptyList()) &&
                        result["policy_commit"] == JsonNull && result["inventory_commit"] == JsonNull &&
                        REASON.matches(result.admissionString("reason")),
                    AdmissionReason.OUTPUT_INVALID,
                )
                throw AdmissionRefused(AdmissionReason.PROVIDER_DENIED)
            }

            "ADMIT" -> {
                val policySource = policy as? JsonObject
                val inventorySource = (inventory as? JsonObject)?.get("source") as? JsonObject
                admissionRequire(policySource != null && inventorySource != null, AdmissionReason.OUTPUT_INVALID)
                val policyCommit = policySource.admissionString("commit")
                val inventoryCommit = inventorySource.admissionString("commit")
                admissionRequire(
                    output.exitCode == 0 && result["reason"] == JsonNull &&
                        COMMIT.matches(policyCommit) && COMMIT.matches(inventoryCommit) &&
                        result.admissionString("policy_commit") == policyCommit &&
                        result.admissionString("inventory_commit") == inventoryCommit &&
                        result["scripts"] == scripts,
                    AdmissionReason.OUTPUT_INVALID,
                )
                val hash = result.admissionString("plan_sha256")
                admissionRequire(SHA256.matches(hash), AdmissionReason.OUTPUT_INVALID)
                return hash
            }

            else -> {
                throw AdmissionRefused(AdmissionReason.OUTPUT_INVALID)
            }
        }
    }

    companion object {
        const val REQUEST_LIMIT = 2 * 1024 * 1024
        const val SQL_LIMIT = 262144
        const val OUTPUT_LIMIT = 65536
        const val RUNTIME = "R4_LINUX_DOCKER_NFTABLES"
        val SHA256 = Regex("[0-9a-f]{64}")
        private val COMMIT = Regex("[0-9a-f]{40}")
        private val REASON = Regex("[A-Z][A-Z0-9_]{0,79}")
        private val RESULT_FIELDS =
            setOf(
                "schema",
                "gate",
                "command",
                "decision",
                "reason",
                "scope",
                "plan_sha256",
                "scripts",
                "policy_commit",
                "inventory_commit",
                "provider_activation",
                "sql_executed",
            )
    }
}
