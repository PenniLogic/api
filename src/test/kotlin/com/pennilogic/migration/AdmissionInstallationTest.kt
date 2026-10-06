package com.pennilogic.migration

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.abort
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.FileSystemException
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest

/** Synthetic installation authority is confined to these unit tests; no real provider acceptance is asserted. */
class AdmissionInstallationTest {
    @TempDir
    lateinit var directory: Path

    private val launcherPath = "scripts/prepare_database_admission.py"
    private val definitionPath = "src/main/resources/database-admission-installation.json"

    private fun hash(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun write(
        path: String,
        bytes: ByteArray,
    ) {
        val file = directory.resolve(path)
        Files.createDirectories(file.parent)
        Files.write(file, bytes)
    }

    private fun pin(
        path: String,
        bytes: ByteArray,
    ): JsonObject =
        buildJsonObject {
            put("path", path)
            put("mode", "100644")
            put("bytes", bytes.size)
            put("sha256", hash(bytes))
        }

    private fun fixture(): JsonObject {
        val launcher =
            """
            from __future__ import annotations
            import hashlib, json, pathlib, sys
            from dataclasses import dataclass, fields
            from typing import ClassVar
            assert __name__ == "__main__"
            assert pathlib.Path(__file__) == pathlib.Path.cwd() / "scripts" / "prepare_database_admission.py"
            assert sys.argv[1] == "run" and sys.argv[2] in ("plan", "admit-apply")
            @dataclass
            class SyntheticSource:
                label: str
                non_field: ClassVar[int] = 1
            assert [field.name for field in fields(SyntheticSource)] == ["label"]
            request = json.load(sys.stdin)
            pathlib.Path("synthetic-launcher-ran").write_text(sys.argv[2])
            scripts = []
            for selected in request["selection"]:
                index = next(i for i, migration in enumerate(request["migrations"]) if migration["id"] == selected["id"])
                migration = request["migrations"][index]
                sql = migration["up"] if selected["direction"] == "up" else migration["reverse"]["sql"]
                scripts.append(dict(migration_index=index, direction=selected["direction"], sha256=hashlib.sha256(sql.encode()).hexdigest()))
            print(json.dumps(dict(
                schema="pennilogic.database-admission.result/1", gate="T-PLT-01-DATABASE-EXTENSION-GATE",
                command=sys.argv[2], decision="ADMIT", reason=None,
                scope="DATABASE_EXTENSIONS_AND_DECLARED_COLUMN_SEMANTICS", plan_sha256="c"*64, scripts=scripts,
                policy_commit=request["policy"]["commit"], inventory_commit=request["inventory"]["source"]["commit"],
                provider_activation=False, sql_executed=False)))
            """.trimIndent().toByteArray()
        val payloads =
            linkedMapOf(
                "scripts/database_admission.py" to "# synthetic; not executed\n".toByteArray(),
                "scripts/database_sql.py" to "# synthetic; not executed\n".toByteArray(),
                "scripts/database_baseline.py" to "# synthetic; not executed\n".toByteArray(),
                "database/admission-trust.json" to "{}\n".toByteArray(),
                "inputs.json" to
                    buildJsonObject {
                        put("policy", AdmissionFixtures.policy)
                        put("inventory", AdmissionFixtures.inventory)
                    }.toString().toByteArray(),
            )
        write(launcherPath, launcher)
        for ((path, bytes) in payloads) write("build/database-admission/$path", bytes)
        val definition =
            buildJsonObject {
                put("schema", "pennilogic.database-admission.installation/1")
                put("gate", "T-PLT-01-DATABASE-EXTENSION-GATE")
                put("runtime", DatabaseAdmission.RUNTIME)
                put(
                    "consumer",
                    buildJsonObject {
                        put("repository", "PenniLogic/api")
                        put("repository_id", 1394134582)
                        put("organization_id", 335295566)
                    },
                )
                put("launcher", pin(launcherPath, launcher))
                put(
                    "binding",
                    buildJsonObject {
                        put(
                            "sources",
                            JsonObject(listOf("infra", "policy", "inventory", "evidence").associateWith { JsonObject(emptyMap()) }),
                        )
                        put("payloads", JsonArray(payloads.map { (path, bytes) -> pin(path, bytes) }))
                    },
                )
            }
        write(definitionPath, definition.toString().toByteArray())
        return definition
    }

    private fun installation(definition: JsonObject): AdmissionInstallation =
        AdmissionInstallation(directory, definition.toString().toByteArray())

    private fun migrations(): List<Migration> {
        val path = directory.resolve("migrations")
        Fixtures.threePhaseSet(path)
        return AdmissionFixtures.load(path).migrations
    }

    @Test
    fun `verified synthetic launcher runs only its captured bytes and receives the normal protocol`() {
        val definition = fixture()
        val admission = installation(definition).admission()
        val migrations = migrations()
        admission.validate(migrations)
        assertEquals("plan", Files.readString(directory.resolve("synthetic-launcher-ran")))
        admission.authorize(migrations, listOf(AdmissionSelection(migrations.first(), "up")))
        assertEquals("admit-apply", Files.readString(directory.resolve("synthetic-launcher-ran")))
    }

    @Test
    fun `launcher tampering after construction cannot execute or forge admission`() {
        val definition = fixture()
        val admission = installation(definition).admission()
        write(launcherPath, "import pathlib; pathlib.Path('synthetic-launcher-ran').write_text('tampered')\n".toByteArray())
        assertEquals(
            "SOURCE_CHANGED",
            assertThrows(AdmissionRefused::class.java) { admission.validate(migrations()) }.code,
        )
        assertFalse(Files.exists(directory.resolve("synthetic-launcher-ran")))
    }

    @Test
    fun `changed policy input and a rewritten disk receipt are not authority`() {
        val definition = fixture()
        val installation = installation(definition)
        write("build/database-admission/inputs.json", "{\"policy\":\"substituted\",\"inventory\":{}}".toByteArray())
        val inputs = Files.readAllBytes(directory.resolve("build/database-admission/inputs.json"))
        val binding = definition.getValue("binding").jsonObject
        val payloads =
            binding.getValue("payloads").jsonArray.map {
                if (it.jsonObject.admissionString("path") == "inputs.json") pin("inputs.json", inputs) else it
            }
        val forged = JsonObject(definition + ("binding" to JsonObject(binding + ("payloads" to JsonArray(payloads)))))
        write(definitionPath, forged.toString().toByteArray())
        assertEquals("SOURCE_CHANGED", assertThrows(AdmissionRefused::class.java) { installation.verify() }.code)
        assertFalse(Files.exists(directory.resolve("synthetic-launcher-ran")))
    }

    @Test
    fun `any payload substitution missing file or extra cached code blocks`() {
        for (path in listOf(
            "scripts/database_admission.py",
            "scripts/database_sql.py",
            "scripts/database_baseline.py",
            "database/admission-trust.json",
            "inputs.json",
        )) {
            val definition = fixture()
            write("build/database-admission/$path", "changed".toByteArray())
            assertThrows(AdmissionRefused::class.java) { installation(definition).verify() }
        }
        val definition = fixture()
        Files.delete(directory.resolve("build/database-admission/scripts/database_sql.py"))
        assertThrows(AdmissionRefused::class.java) { installation(definition).verify() }
        val restored = fixture()
        write("build/database-admission/scripts/__pycache__/injected.pyc", "not permitted".toByteArray())
        assertThrows(AdmissionRefused::class.java) { installation(restored).verify() }
        assertFalse(Files.exists(directory.resolve("synthetic-launcher-ran")))
    }

    @Test
    fun `resource consumer launcher path mode and duplicate payloads are closed`() {
        val definition = fixture()
        val launcher = definition.getValue("launcher").jsonObject
        val binding = definition.getValue("binding").jsonObject
        val payloads = binding.getValue("payloads").jsonArray
        val invalid =
            listOf(
                JsonObject(definition + ("unknown" to JsonNull)),
                JsonObject(
                    definition +
                        ("consumer" to JsonObject(definition.getValue("consumer").jsonObject + ("repository_id" to JsonPrimitive(1)))),
                ),
                JsonObject(definition + ("launcher" to JsonObject(launcher + ("path" to JsonPrimitive("../launcher.py"))))),
                JsonObject(definition + ("launcher" to JsonObject(launcher + ("mode" to JsonPrimitive("100755"))))),
                JsonObject(definition + ("launcher" to JsonObject(launcher + ("bytes" to JsonPrimitive(0))))),
                JsonObject(definition + ("launcher" to JsonObject(launcher + ("sha256" to JsonPrimitive("invalid"))))),
                JsonObject(definition + ("binding" to JsonObject(binding + ("payloads" to JsonArray(List(5) { payloads.first() }))))),
            )
        for (item in invalid) assertThrows(AdmissionRefused::class.java) { installation(item) }
        assertFalse(Files.exists(directory.resolve("synthetic-launcher-ran")))
    }

    @Test
    fun `null acceptance binding is an explicit unavailable-source refusal`() {
        val definition = JsonObject(fixture() + ("binding" to JsonNull))
        assertEquals("SOURCE_UNAVAILABLE", assertThrows(AdmissionRefused::class.java) { installation(definition) }.code)
        assertFalse(Files.exists(directory.resolve("synthetic-launcher-ran")))
    }

    @Test
    fun `compiled definition bytes are not mutable caller-owned state`() {
        val definition = fixture()
        val bytes = definition.toString().toByteArray()
        val installation = AdmissionInstallation(directory, bytes)
        bytes.fill(0)
        assertTrue(installation.verify().inputs.containsKey("policy"))
    }

    @Test
    fun `a symlinked launcher cannot redirect execution`() {
        val definition = fixture()
        val target = directory.resolve("replacement.py")
        Files.copy(directory.resolve(launcherPath), target)
        Files.delete(directory.resolve(launcherPath))
        try {
            Files.createSymbolicLink(directory.resolve(launcherPath), target)
        } catch (error: FileSystemException) {
            if (System.getProperty("os.name").startsWith("Windows") && error.reason?.contains("privilege") == true) {
                abort<Unit>("Windows account cannot create a symlink; no privilege or system configuration is changed")
            }
            throw error
        }
        assertEquals("SOURCE_INVALID", assertThrows(AdmissionRefused::class.java) { installation(definition).verify() }.code)
        assertFalse(Files.exists(directory.resolve("synthetic-launcher-ran")))
    }
}
