package com.pennilogic.testing

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

@Tag("postgres")
class PostgresCleanupPostgresTest {
    @TempDir
    lateinit var directory: Path

    private val root = Path.of("").toAbsolutePath()
    private val marker get() = directory.resolve("build/migration-test/container-id")
    private val image: String by lazy {
        val id = Files.readString(root.resolve("build/migration-test/container-id")).trim()
        check(Regex("[0-9a-f]{64}").matches(id)) { "cleanup-fixture-container-identity" }
        docker("container", "inspect", "--format", "{{.Config.Image}}", id)
    }
    private var invocation = 0

    @Test
    fun `actual Gradle cleanup removes unshared anonymous storage but preserves named and shared resources`() {
        copyBuild()
        val named = "api-cleanup-" + UUID.randomUUID()
        val containers = mutableListOf<String>()
        val volumes = mutableSetOf(named)
        try {
            docker("volume", "create", named)
            val source =
                docker(
                    "create",
                    "--volume",
                    "/cleanup-shared",
                    "--mount",
                    "type=volume,source=$named,target=/cleanup-named",
                    image,
                    "true",
                ).also { containers.add(it) }
            val byDestination = mounts(source)
            volumes.addAll(byDestination.values)
            val data = byDestination.getValue("/var/lib/postgresql/data")
            val shared = byDestination.getValue("/cleanup-shared")
            val holder =
                docker(
                    "create",
                    "--mount",
                    "type=volume,source=$shared,target=/cleanup-shared",
                    image,
                    "true",
                ).also { containers.add(it) }
            volumes.addAll(mounts(holder).values)
            Files.createDirectories(marker.parent)
            Files.writeString(marker, source)
            cleanup(0)
            assertTrue(!Files.exists(marker), "successful-cleanup-clears-marker")
            absent("container", source)
            absent("volume", data)
            assertTrue(docker("volume", "inspect", "--format", "{{.Name}}", named) == named, "named-storage-preserved")
            assertTrue(docker("volume", "inspect", "--format", "{{.Name}}", shared) == shared, "shared-storage-preserved")
            assertTrue(docker("container", "inspect", "--format", "{{.Id}}", holder) == holder, "other-container-preserved")
            cleanup(0)
        } finally {
            for (id in containers.asReversed()) {
                if (present("container", id)) docker("rm", "-f", "-v", id)
                absent("container", id)
            }
            for (name in volumes) {
                if (present("volume", name)) docker("volume", "rm", name)
                absent("volume", name)
            }
        }
    }

    @Test
    fun `actual Gradle cleanup refuses malformed markers and retains failed Docker removal for recovery`() {
        copyBuild()
        Files.createDirectories(marker.parent)
        Files.writeString(marker, "--all")
        val invalid = cleanup(1)
        assertTrue(invalid.contains("Invalid migration test container marker"), "malformed-marker-refusal")
        assertTrue(Files.readString(marker) == "--all", "malformed-marker-retained")
        val missing = "0".repeat(64)
        absent("container", missing)
        Files.writeString(marker, missing)
        val failed = cleanup(1)
        assertTrue(failed.contains("Disposable PostgreSQL cleanup failed"), "docker-failure-surfaced")
        assertTrue(Files.readString(marker) == missing, "failed-removal-retains-marker")
    }

    @Test
    fun `wrapper launcher preserves arguments and exit codes without requiring POSIX execute permission`() {
        val windows = System.getProperty("os.name").startsWith("Windows")
        val script =
            if (windows) {
                "@echo off\r\nsetlocal DisableDelayedExpansion\r\nset \"value=%~1\"\r\n" +
                    "setlocal EnableDelayedExpansion\r\necho(!value!\r\nexit /b %~2\r\n"
            } else {
                "#!/bin/sh\nprintf '%s\\n' \"\$1\"\nexit \"\$2\"\n"
            }
        val directories =
            listOf(
                "wrapper with spaces",
                "wrapper & (data) ^ %PENNILOGIC_QUOTING_CANARY% !PENNILOGIC_QUOTING_CANARY!",
            )
        val arguments =
            listOf(
                "argument with spaces",
                "  leading and trailing spaces  ",
                "literal & | < > ( ) ^ %PENNILOGIC_QUOTING_CANARY% !PENNILOGIC_QUOTING_CANARY!",
                "literal ;,=+[]' and trailing\\",
                "",
            )
        var executions = 0
        for (name in directories) {
            val fixture = Files.createDirectory(directory.resolve(name))
            val wrapper = fixture.resolve(if (windows) "gradlew.bat" else "gradlew")
            Files.writeString(wrapper, script)
            if (!windows) {
                Files.setPosixFilePermissions(wrapper, PosixFilePermissions.fromString("rw-r--r--"))
                assertTrue(!Files.isExecutable(wrapper), "fixture-wrapper-not-executable")
            }
            for (argument in arguments) {
                for (expected in listOf(0, 7)) {
                    val launcher = wrapperLauncher(fixture, listOf(argument, expected.toString()))
                    launcher.environment()["PENNILOGIC_QUOTING_CANARY"] = "must-not-expand"
                    val (exit, output) = run(launcher, 30)
                    executions++
                    assertTrue(exit == expected, "wrapper-exit-preserved")
                    assertTrue(output == argument + System.lineSeparator(), "wrapper-argument-preserved")
                }
            }
            if (windows) {
                for (argument in listOf("embedded\"quote", "line\rbreak", "line\nbreak", "null\u0000byte")) {
                    val failure = assertThrows(IllegalArgumentException::class.java) { wrapperLauncher(fixture, listOf(argument)) }
                    assertTrue(failure.message == "wrapper-command-character", "wrapper-command-refusal")
                }
            }
            assertTrue(Files.readString(wrapper) == script, "fixture-wrapper-unchanged")
            if (!windows) assertTrue(!Files.isExecutable(wrapper), "fixture-wrapper-still-not-executable")
        }
        assertTrue(executions == 20, "wrapper-exact-control-count")
        println("""{"event":"wrapper_quoting_controls","executions":$executions,"directories":2,"arguments":5,"exit_codes":[0,7]}""")
    }

    private fun copyBuild() {
        for (name in listOf(
            "build.gradle.kts",
            "settings.gradle.kts",
            "gradle.properties",
            "gradle.lockfile",
            "gradle/verification-metadata.xml",
        )) {
            val destination = directory.resolve(name)
            Files.createDirectories(destination.parent)
            Files.copy(root.resolve(name), destination)
        }
    }

    private fun mounts(id: String): Map<String, String> {
        val mounts = Json.parseToJsonElement(docker("inspect", "--format", "{{json .Mounts}}", id)).jsonArray
        val volumes =
            mounts.associate {
                val mount = it.jsonObject
                assertTrue(mount.getValue("Type").jsonPrimitive.content == "volume", "cleanup-fixture-no-host-binds")
                mount.getValue("Destination").jsonPrimitive.content to mount.getValue("Name").jsonPrimitive.content
            }
        println(
            buildJsonObject {
                put("event", "cleanup_storage_fixture")
                put("container", id)
                putJsonObject("volumes") { volumes.forEach { (destination, name) -> put(destination, name) } }
            },
        )
        return volumes
    }

    private fun cleanup(expected: Int): String {
        val launcher = wrapperLauncher(root, listOf("--no-daemon", "--offline", "--console=plain", "stopMigrationTestPostgres"))
        val (exit, output) = run(launcher, 120)
        assertTrue(exit == expected, "native-cleanup-exit")
        return output
    }

    private fun wrapperLauncher(
        directory: Path,
        arguments: List<String>,
    ): ProcessBuilder =
        if (System.getProperty("os.name").startsWith("Windows")) {
            val tokens = listOf(directory.resolve("gradlew.bat").toString()) + arguments
            require(tokens.none { token -> token.any { it in "\"\r\n\u0000" } }) { "wrapper-command-character" }
            val values = tokens.mapIndexed { index, token -> "PENNILOGIC_TEST_WRAPPER_$index" to token }.toMap()
            // Substitution is single-pass; CMD leaves empty-variable references literal.
            val command = values.entries.joinToString(" ") { (name, value) -> if (value.isEmpty()) "\"\"" else "\"%$name%\"" }
            ProcessBuilder(requireNotNull(System.getenv("ComSpec")), "/d", "/v:off", "/s", "/c", "\"$command\"")
                .apply { environment().putAll(values) }
        } else {
            ProcessBuilder(listOf("sh", directory.resolve("gradlew").toString()) + arguments)
        }

    private fun docker(vararg arguments: String): String {
        val (exit, output) = run(listOf("docker") + arguments, 30)
        assertTrue(exit == 0, "owned-cleanup-fixture-docker-command")
        return output.trim()
    }

    private fun present(
        type: String,
        id: String,
    ): Boolean {
        val (exit, output) = run(listOf("docker", type, "inspect", id), 30)
        if (exit == 0) return true
        assertTrue(exit == 1 && output.lowercase().contains("no such $type"), "owned-resource-inspection-failed")
        return false
    }

    private fun absent(
        type: String,
        id: String,
    ) {
        assertTrue(!present(type, id), "owned-$type-must-be-absent")
    }

    private fun run(
        command: List<String>,
        seconds: Long,
    ): Pair<Int, String> = run(ProcessBuilder(command), seconds)

    private fun run(
        builder: ProcessBuilder,
        seconds: Long,
    ): Pair<Int, String> {
        val started = System.nanoTime()
        val output = directory.resolve("process-${invocation++}.log").toFile()
        val command = builder.command()
        val process =
            builder
                .directory(directory.toFile())
                .redirectErrorStream(true)
                .redirectOutput(output)
                .start()
        if (!process.waitFor(seconds, TimeUnit.SECONDS)) {
            process.descendants().use { children -> children.forEach { it.destroyForcibly() } }
            process.destroyForcibly()
            assertTrue(process.waitFor(5, TimeUnit.SECONDS), "cleanup-regression-process-stopped")
            throw AssertionError("cleanup-regression-process-timeout")
        }
        val text = output.readText()
        println(
            buildJsonObject {
                put("event", "cleanup_process")
                putJsonArray("command") { command.forEach { add(it) } }
                put("exit", process.exitValue())
                put("elapsed_ms", TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started))
                put("output_sha256", MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).toHexString())
            },
        )
        return process.exitValue() to text
    }
}
