package com.pennilogic.migration

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.lang.reflect.Proxy
import java.nio.file.Files
import java.nio.file.Path
import java.sql.Connection
import java.time.Duration

class DatabaseAdmissionTest {
    @TempDir
    lateinit var directory: Path

    private fun set(): MigrationSet {
        Fixtures.threePhaseSet(directory)
        return AdmissionFixtures.load(directory)
    }

    private fun adapter(transport: AdmissionTransport): DatabaseAdmission =
        DatabaseAdmission(AdmissionFixtures.policy, AdmissionFixtures.inventory, transport)

    private fun output(
        value: JsonElement,
        exit: Int = 0,
    ): AdmissionOutput = AdmissionOutput(exit, value.toString().toByteArray(Charsets.UTF_8))

    @Test
    fun `complete set and both reverse kinds go through the protocol even when unselected`() {
        Fixtures.threePhaseSet(directory)
        val requests = mutableListOf<Pair<String, JsonObject>>()
        val admission =
            adapter { command, request ->
                requests += command to AdmissionJson.parse(request, DatabaseAdmission.REQUEST_LIMIT).jsonObject
                AdmissionFixtures.accept(command, request)
            }
        val set = MigrationSet.load(directory) { admission }
        set.authorize(listOf(AdmissionSelection(set.migrations[2], "compensating"), AdmissionSelection(set.migrations[1], "down")))
        assertEquals(listOf("plan", "plan", "admit-apply"), requests.map { it.first })
        assertEquals(JsonArray(emptyList()), requests.first().second["selection"])
        for ((_, request) in requests) {
            assertEquals(3, request.getValue("migrations").jsonArray.size)
            assertEquals(
                set.migrations.map { it.reversal.sql },
                request.getValue("migrations").jsonArray.map {
                    it.jsonObject
                        .getValue("reverse")
                        .jsonObject
                        .admissionString("sql")
                },
            )
            assertEquals(JsonArray(emptyList()), request["packages"])
        }
        val plan = requests[1].second
        val apply = requests[2].second
        assertEquals(plan, JsonObject(apply - "plan_sha256"))
        assertEquals(Checksum.of(plan.toString()), apply.admissionString("plan_sha256"))
    }

    @Test
    fun `no-op selection is not replaced with fictitious SQL`() {
        val set = set()
        var count = 0
        val admission =
            adapter { command, request ->
                count++
                assertEquals(
                    JsonArray(emptyList()),
                    AdmissionJson.parse(request, DatabaseAdmission.REQUEST_LIMIT).jsonObject["selection"],
                )
                AdmissionFixtures.accept(command, request)
            }
        admission.authorize(set.migrations, emptyList())
        assertEquals(2, count)
    }

    @Test
    fun `output fields and exit decisions are closed and must agree`() {
        val set = set()
        val changes =
            mapOf(
                "schema" to JsonPrimitive("unrecognized"),
                "gate" to JsonPrimitive("unrecognized"),
                "command" to JsonPrimitive("run"),
                "decision" to JsonPrimitive("ALLOW"),
                "reason" to JsonPrimitive("contradictory"),
                "scope" to JsonPrimitive("ALL_SQL"),
                "plan_sha256" to JsonPrimitive("a".repeat(63)),
                "policy_commit" to JsonPrimitive("c".repeat(40)),
                "inventory_commit" to JsonPrimitive("c".repeat(40)),
                "provider_activation" to JsonPrimitive(true),
                "sql_executed" to JsonPrimitive(true),
                "unknown" to JsonNull,
            )
        for ((key, value) in changes) {
            val admission =
                adapter { command, request ->
                    output(JsonObject(AdmissionFixtures.response(command, request) + (key to value)))
                }
            assertThrows(AdmissionRefused::class.java, { admission.validate(set.migrations) }, key)
        }
        for (exit in listOf(-1, 1, 2, 255)) {
            val admission = adapter { command, request -> AdmissionFixtures.accept(command, request).copy(exitCode = exit) }
            assertThrows(AdmissionRefused::class.java, { admission.validate(set.migrations) }, "exit-$exit")
        }
    }

    @Test
    fun `selected hashes order indices and direction must match every exact execution buffer`() {
        val set = set()
        val selection = set.migrations.take(2).map { AdmissionSelection(it, "up") }
        val substitutions: List<(JsonArray) -> JsonArray> =
            listOf(
                { JsonArray(it.reversed()) },
                { JsonArray(it.dropLast(1)) },
                { JsonArray(it + it.first()) },
                { JsonArray(listOf(JsonObject(it.first().jsonObject + ("sha256" to JsonPrimitive("f".repeat(64)))), it[1])) },
                { JsonArray(listOf(JsonObject(it.first().jsonObject + ("direction" to JsonPrimitive("down"))), it[1])) },
                { JsonArray(listOf(JsonObject(it.first().jsonObject + ("migration_index" to JsonPrimitive(1))), it[1])) },
                { JsonArray(listOf(JsonObject(it.first().jsonObject + ("migration_index" to JsonPrimitive("0"))), it[1])) },
                { JsonArray(listOf(JsonObject(it.first().jsonObject + ("unexpected" to JsonNull)), it[1])) },
            )
        for (substitute in substitutions) {
            val admission =
                adapter { command, request ->
                    val result = AdmissionFixtures.response(command, request)
                    output(JsonObject(result + ("scripts" to substitute(result.getValue("scripts").jsonArray))))
                }
            assertThrows(AdmissionRefused::class.java) { admission.authorize(set.migrations, selection) }
        }
    }

    @Test
    fun `pre-apply cannot substitute a new plan digest`() {
        val set = set()
        val admission =
            adapter { command, request ->
                val result = AdmissionFixtures.response(command, request)
                output(if (command == "admit-apply") JsonObject(result + ("plan_sha256" to JsonPrimitive("f".repeat(64)))) else result)
            }
        assertThrows(AdmissionRefused::class.java) { admission.authorize(set.migrations, emptyList()) }
    }

    @Test
    fun `denial is static and never reflects provider reason contents`() {
        val set = set()
        val refusal =
            assertThrows(AdmissionRefused::class.java) {
                adapter(AdmissionFixtures::deny).validate(set.migrations)
            }
        assertEquals("PROVIDER_DENIED", refusal.code)
        assertEquals("PROVIDER_DENIED", refusal.message)
        val contradiction = adapter { command, request -> AdmissionFixtures.deny(command, request).copy(exitCode = 0) }
        assertThrows(AdmissionRefused::class.java) { contradiction.validate(set.migrations) }
    }

    @Test
    fun `denials cannot carry execution authority or sensitive free-text reasons`() {
        val migrations = set().migrations
        for ((key, value) in mapOf(
            "plan_sha256" to JsonPrimitive("a".repeat(64)),
            "scripts" to JsonArray(listOf(JsonNull)),
            "policy_commit" to JsonPrimitive("a".repeat(40)),
            "inventory_commit" to JsonPrimitive("b".repeat(40)),
            "reason" to JsonPrimitive("sensitive-marker with provider details"),
        )) {
            val admission =
                adapter { command, request ->
                    val denied =
                        AdmissionJson
                            .parse(
                                AdmissionFixtures.deny(command, request).stdout,
                                DatabaseAdmission.OUTPUT_LIMIT,
                            ).jsonObject
                    output(JsonObject(denied + (key to value)), exit = 1)
                }
            val refused = assertThrows(AdmissionRefused::class.java) { admission.validate(migrations) }
            assertEquals("OUTPUT_INVALID", refused.code)
            assertFalse(refused.toString().contains("sensitive-marker"))
        }
    }

    @Test
    fun `admit cannot validate absent malformed or nonimmutable source identities`() {
        val migrations = set().migrations
        val invalidSources =
            listOf(
                JsonNull to AdmissionFixtures.inventory,
                AdmissionFixtures.policy to JsonNull,
                AdmissionFixtures.policy to JsonObject(emptyMap()),
                AdmissionFixtures.policy to JsonObject(mapOf("source" to JsonArray(emptyList()))),
                JsonObject(mapOf("commit" to JsonPrimitive("a".repeat(39)))) to AdmissionFixtures.inventory,
                AdmissionFixtures.policy to
                    JsonObject(mapOf("source" to JsonObject(mapOf("commit" to JsonPrimitive("main"))))),
            )
        for ((policy, inventory) in invalidSources) {
            val admission = DatabaseAdmission(policy, inventory, AdmissionFixtures::accept)
            assertEquals("OUTPUT_INVALID", assertThrows(AdmissionRefused::class.java) { admission.validate(migrations) }.code)
        }
    }

    @Test
    fun `malformed duplicate multiple oversized and invalid UTF8 results all block`() {
        val set = set()
        val invalid: List<(String) -> ByteArray> =
            listOf(
                { ByteArray(0) },
                { (it + "\n" + it).toByteArray() },
                { (it + " trailing").toByteArray() },
                { it.replace("\"gate\":", "\"gate\":\"duplicate\",\"gate\":").toByteArray() },
                { it.replace("\"gate\":", "\"\\u0067ate\":\"duplicate\",\"gate\":").toByteArray() },
                { it.replace("\"provider_activation\":false", "\"provider_activation\":\"false\"").toByteArray() },
                { it.replace("\"reason\":null", "\"reason\":NaN").toByteArray() },
                { ("[".repeat(18) + "0" + "]".repeat(18)).toByteArray() },
                { "x".repeat(DatabaseAdmission.OUTPUT_LIMIT + 1).toByteArray() },
                { byteArrayOf(0xc3.toByte(), 0x28) },
                { byteArrayOf(0xef.toByte(), 0xbb.toByte(), 0xbf.toByte()) + it.toByteArray() },
                { "[]".toByteArray() },
                { "{\"incomplete\":".toByteArray() },
            )
        for (alter in invalid) {
            val admission =
                adapter { command, request -> AdmissionOutput(0, alter(AdmissionFixtures.response(command, request).toString())) }
            assertThrows(AdmissionRefused::class.java) { admission.validate(set.migrations) }
        }
        val duplicateScript =
            adapter { command, request ->
                val content =
                    AdmissionFixtures
                        .response(command, request)
                        .toString()
                        .replace("\"migration_index\":0", "\"migration_index\":0,\"migration_index\":0")
                AdmissionOutput(0, content.toByteArray())
            }
        assertThrows(AdmissionRefused::class.java) {
            duplicateScript.authorize(set.migrations, listOf(AdmissionSelection(set.migrations.first(), "up")))
        }
    }

    @Test
    fun `foreign migration objects duplicate choices and unsupported directions never reach the provider`() {
        val set = set()
        val admission = adapter { _, _ -> throw AssertionError("invalid input reached provider") }
        val original = set.migrations.first()
        for (choices in listOf(
            listOf(AdmissionSelection(original.copy(sql = "SELECT 99;"), "up")),
            listOf(AdmissionSelection(original, "up"), AdmissionSelection(original, "down")),
            listOf(AdmissionSelection(original, "compensating")),
        )) {
            assertThrows(AdmissionRefused::class.java) { admission.authorize(set.migrations, choices) }
        }
        val oversized = original.copy(sql = "x".repeat(DatabaseAdmission.SQL_LIMIT + 1))
        assertThrows(AdmissionRefused::class.java) { admission.validate(listOf(oversized)) }
        assertThrows(AdmissionRefused::class.java) { admission.validate(List(129) { original }) }
        assertThrows(AdmissionRefused::class.java) { admission.authorize(set.migrations, List(129) { AdmissionSelection(original, "up") }) }
        for (invalid in listOf(
            original.copy(sql = ""),
            original.copy(reversal = original.reversal.copy(sql = "")),
            original.copy(checksum = "f".repeat(64)),
            original.copy(reversal = original.reversal.copy(checksum = "f".repeat(64))),
        )) {
            assertEquals("INPUT_INVALID", assertThrows(AdmissionRefused::class.java) { admission.validate(listOf(invalid)) }.code)
        }
    }

    @Test
    fun `aggregate serialized input and invalid text block before process invocation`() {
        val original = set().migrations.first()
        val admission = adapter { _, _ -> throw AssertionError("over-limit input reached provider") }
        val sql = "x".repeat(DatabaseAdmission.SQL_LIMIT)
        val large =
            original.copy(
                sql = sql,
                checksum = Checksum.of(sql),
                reversal = original.reversal.copy(sql = sql, checksum = Checksum.of(sql)),
            )
        assertEquals(
            "INPUT_INVALID",
            assertThrows(AdmissionRefused::class.java) { admission.validate(List(5) { large }) }.code,
        )
        for (invalid in listOf("SELECT 1;\r\n", "SELECT '\u0000';")) {
            assertThrows(AdmissionRefused::class.java) {
                admission.validate(listOf(original.copy(sql = invalid, checksum = Checksum.of(invalid))))
            }
        }
    }

    @Test
    fun `JSON strings reject unescaped control characters and malformed escapes`() {
        for (character in charArrayOf('\u0000', '\u0001', '\t', '\n', '\r', '\u001f')) {
            val json = "{\"field\":\"before${character}after\"}".toByteArray()
            assertThrows(AdmissionRefused::class.java) { AdmissionJson.parse(json, DatabaseAdmission.OUTPUT_LIMIT) }
        }
        for (json in listOf("""{"field":"\x"}""", """{"field":"\uQQQQ"}""", """{"field":"trailing\"}""")) {
            assertThrows(AdmissionRefused::class.java) { AdmissionJson.parse(json.toByteArray(), DatabaseAdmission.OUTPUT_LIMIT) }
        }
        assertEquals(
            "before\u0000\t\n\rafter",
            AdmissionJson
                .parse("""{"field":"before\u0000\t\n\rafter"}""".toByteArray(), DatabaseAdmission.OUTPUT_LIMIT)
                .jsonObject
                .admissionString("field"),
        )
    }

    @Test
    fun `JSON container limits and token syntax reject malformed transport without rejecting exact bounds`() {
        val atLimit = "[" + List(4096) { "0" }.joinToString(",") + "]"
        assertEquals(4096, AdmissionJson.parse(atLimit.toByteArray(), DatabaseAdmission.OUTPUT_LIMIT).jsonArray.size)
        val tooMany = "[" + List(4097) { "0" }.joinToString(",") + "]"
        for (json in listOf(tooMany, "{", "{true:1}", """{"key":tru}""", """{"key":fals}""", """{"key":nul}""", "@0", "@", "\"")) {
            assertThrows(AdmissionRefused::class.java) { AdmissionJson.parse(json.toByteArray(), DatabaseAdmission.OUTPUT_LIMIT) }
        }
        val valid = """ [ {}, [], true, false, null, -1.25e+2, "quote:\" slash:\\ tab:\t" ] """
        assertEquals(7, AdmissionJson.parse(valid.toByteArray(), DatabaseAdmission.OUTPUT_LIMIT).jsonArray.size)
        for (value in listOf(JsonArray(emptyList()), JsonObject(emptyMap()), JsonNull, JsonPrimitive(1))) {
            val objectWithField = JsonObject(mapOf("field" to value))
            assertThrows(AdmissionRefused::class.java) { objectWithField.admissionString("field") }
        }
        assertThrows(AdmissionRefused::class.java) { JsonObject(emptyMap()).admissionString("absent") }
    }

    @Test
    fun `normalization happens once before immutable buffers are admitted`() {
        Fixtures.write(directory, "V001__line_endings", Fixtures.header().replace("\n", "\r\n") + "SELECT 1;\r\n", "SELECT 2;\r\n")
        val set = AdmissionFixtures.load(directory)
        val migration = set.migrations.single()
        assertFalse(migration.sql.contains("\r\n"))
        assertFalse(migration.reversal.sql.contains("\r\n"))
        assertEquals(Checksum.of(Files.readString(directory.resolve(migration.file))), migration.checksum)
        assertThrows(UnsupportedOperationException::class.java) { (set.migrations as MutableList<Migration>).clear() }
        Files.writeString(directory.resolve(migration.file), migration.sql)
        set.authorize(listOf(AdmissionSelection(migration, "up")))
        Files.writeString(directory.resolve(migration.reversal.file), "SELECT 99;\n")
        assertEquals(
            "MIGRATIONS_CHANGED",
            assertThrows(AdmissionRefused::class.java) { set.authorize(listOf(AdmissionSelection(migration, "up"))) }.code,
        )
    }

    @Test
    fun `removed migration directory and invalid UTF8 cannot change previously admitted buffers`() {
        val set = set()
        val forward = directory.resolve(set.migrations.first().file)
        Files.write(forward, byteArrayOf(0xc3.toByte(), 0x28))
        assertEquals("INPUT_INVALID", assertThrows(AdmissionRefused::class.java) { AdmissionFixtures.load(directory) }.code)
        assertEquals("MIGRATIONS_CHANGED", assertThrows(AdmissionRefused::class.java) { set.authorize(emptyList()) }.code)
        Files.list(directory).use { files -> files.forEach { Files.delete(it) } }
        Files.delete(directory)
        assertEquals("MIGRATIONS_CHANGED", assertThrows(AdmissionRefused::class.java) { set.authorize(emptyList()) }.code)
    }

    @Test
    fun `file count byte count and complete-set size caps apply before provider invocation`() {
        for (length in listOf(DatabaseAdmission.SQL_LIMIT + 1, DatabaseAdmission.SQL_LIMIT * 2 + 1)) {
            val path = directory.resolve("bytes-$length")
            Fixtures.write(path, "V001__bounded", Fixtures.header() + "SELECT 1;", "SELECT 1;")
            Files.write(path.resolve("V001__bounded.up.sql"), ByteArray(length) { ' '.code.toByte() })
            assertEquals("INPUT_INVALID", assertThrows(AdmissionRefused::class.java) { AdmissionFixtures.load(path) }.code)
        }
        val nonfile = directory.resolve("nonfile")
        Fixtures.write(nonfile, "V001__bounded", Fixtures.header() + "SELECT 1;", "SELECT 1;")
        Files.delete(nonfile.resolve("V001__bounded.up.sql"))
        Files.createDirectory(nonfile.resolve("V001__bounded.up.sql"))
        assertEquals("INPUT_INVALID", assertThrows(AdmissionRefused::class.java) { AdmissionFixtures.load(nonfile) }.code)
        val many = Files.createDirectory(directory.resolve("many"))
        repeat(257) { Files.writeString(many.resolve("entry-$it"), "") }
        assertEquals("INPUT_INVALID", assertThrows(AdmissionRefused::class.java) { AdmissionFixtures.load(many) }.code)
        val aggregate = directory.resolve("aggregate")
        for (version in 1..5) {
            val sql = "SELECT 1; --" + " ".repeat(235000)
            Fixtures.write(aggregate, "${MigrationSet.label(version)}__bounded", Fixtures.header() + sql, sql)
        }
        val failure =
            assertThrows(AdmissionRefused::class.java) {
                MigrationSet.load(aggregate) { throw AssertionError("over-limit set reached provider construction") }
            }
        assertEquals("INPUT_INVALID", failure.code)
    }

    @Test
    fun `denied complete-set revalidation touches no connection API`() {
        Fixtures.threePhaseSet(directory)
        var denied = false
        val admission =
            adapter { command, request ->
                if (denied) AdmissionFixtures.deny(command, request) else AdmissionFixtures.accept(command, request)
            }
        val set = MigrationSet.load(directory) { admission }
        val connection =
            Connection::class.java.cast(
                Proxy.newProxyInstance(javaClass.classLoader, arrayOf(Connection::class.java)) { _, _, _ ->
                    throw AssertionError("denied admission touched connection")
                },
            )
        denied = true
        val runner = MigrationRunner(connection, set, Identity("test", "test", 1), Duration.ofSeconds(60)) {}
        assertThrows(AdmissionRefused::class.java) { runner.migrate(null, includeContract = false, dryRun = false) }
        assertThrows(AdmissionRefused::class.java) { runner.migrateDown(null, dryRun = false) }
        assertThrows(AdmissionRefused::class.java) { runner.releaseLock(Identity("test", "test", 1)) }
    }

    @Test
    fun `public CLI cannot use caller flags as authority for an unreviewed set`() {
        Fixtures.threePhaseSet(directory)
        val output = Output()
        val environment =
            mapOf(
                "MIGRATION_ADMISSION_ACCEPTED" to "true",
                "MIGRATION_ADMISSION_COMMAND" to "unused",
                "MIGRATION_DB_PASSWORD" to "sensitive-marker",
            )
        assertEquals(1, MigrationCli(output.stream, environment).run(listOf("validate", "--migrations", directory.toString())))
        assertTrue(output.require("migration_admission_refused").contains("\"code\":"))
        assertFalse(output.toString().contains("sensitive-marker"))
    }
}
