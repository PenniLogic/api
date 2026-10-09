package com.pennilogic.testing

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import org.junit.jupiter.api.AfterAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeAll
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Files
import java.nio.file.Path
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class CategoryTimingTest {
    @TempDir
    lateinit var directory: Path

    @Test
    fun `actual cleanup and deterministic timing controls preserve execution boundaries`() {
        val root = Path.of("").toAbsolutePath()
        val build = Files.readString(root.resolve("build.gradle.kts"))
        for (name in listOf("settings.gradle.kts", "gradle.properties", "gradle.lockfile", "gradle/verification-metadata.xml")) {
            val target = directory.resolve(name)
            Files.createDirectories(target.parent)
            Files.copy(root.resolve(name), target)
        }
        Files.writeString(directory.resolve("build.gradle.kts"), build + controls)
        val output =
            Files.createTempFile(
                Files.createDirectories(root.resolve("build/test-category-timing")),
                "fixture-",
                ".log",
            )
        val process =
            ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp",
                root.resolve("gradle/wrapper/gradle-wrapper.jar").toString(),
                "org.gradle.wrapper.GradleWrapperMain",
                "--no-daemon",
                "--offline",
                "--console=plain",
                "stopMigrationTestPostgres",
                "categoryTimingControls",
            ).directory(directory.toFile()).redirectErrorStream(true).redirectOutput(output.toFile()).start()
        try {
            assertTrue(process.waitFor(120, TimeUnit.SECONDS), "category-timing-fixture-process-timeout")
            assertTrue(Files.size(output) <= 2 * 1024 * 1024, "category-timing-fixture-output-bound")
            val text = Files.readString(output)
            println(
                buildJsonObject {
                    put("event", "category_timing_native_fixture")
                    put("exit", process.exitValue())
                    put("log", root.relativize(output).toString())
                    put("output_sha256", MessageDigest.getInstance("SHA-256").digest(text.toByteArray()).toHexString())
                    put("build_sha256", MessageDigest.getInstance("SHA-256").digest(build.toByteArray()).toHexString())
                },
            )
            assertEquals(0, process.exitValue(), "category-timing-native-fixture-failed")
            val events =
                text
                    .lineSequence()
                    .filter { it.startsWith("{") }
                    .map { Json.parseToJsonElement(it).jsonObject }
                    .toList()
            val timing = events.single { it.getValue("event").jsonPrimitive.content == "test_category_fixture" }
            assertEquals("teardown", timing.getValue("phase").jsonPrimitive.content)
            assertEquals("integration", timing.getValue("category").jsonPrimitive.content)
            assertEquals("stopMigrationTestPostgres", timing.getValue("task").jsonPrimitive.content)
            assertEquals("fixture_action", timing.getValue("boundary").jsonPrimitive.content)
            assertEquals("gradle_process_monotonic_ns", timing.getValue("clock").jsonPrimitive.content)
            assertEquals("completed", timing.getValue("outcome").jsonPrimitive.content)
            val elapsed = timing.getValue("finished_ns").jsonPrimitive.long - timing.getValue("started_ns").jsonPrimitive.long
            assertTrue(elapsed >= 0)
            assertEquals(TimeUnit.NANOSECONDS.toMillis(elapsed), timing.getValue("wall_ms").jsonPrimitive.long)
            assertFalse(Files.exists(directory.resolve("build/migration-test/container-id")), "no-docker-fixture-has-no-container")
            val completed = events.single { it.getValue("event").jsonPrimitive.content == "category_timing_controls" }
            assertEquals(
                listOf(
                    "setup",
                    "failure",
                    "recovery",
                    "overlap",
                    "zero",
                    "clock-order",
                    "reporting-failure",
                    "combined-failure",
                    "clock-failure",
                    "phase-refusal",
                ),
                completed.getValue("cases").jsonArray.map { it.jsonPrimitive.content },
            )
            println(completed)
        } finally {
            if (process.isAlive) {
                process.descendants().use { children -> children.forEach { it.destroyForcibly() } }
                process.destroyForcibly()
                check(process.waitFor(5, TimeUnit.SECONDS)) { "category-timing-fixture-process-did-not-stop" }
            }
        }
    }

    companion object {
        private var classFixtureStarted = 0L

        @JvmStatic
        @BeforeAll
        fun startClassFixture() {
            classFixtureStarted = System.currentTimeMillis()
        }

        @JvmStatic
        @AfterAll
        fun finishClassFixture() {
            println(
                """{"event":"category_timing_class_fixture","started_ms":$classFixtureStarted,"finished_ms":${System.currentTimeMillis()}}""",
            )
        }

        private val controls =
            """

            tasks.register("categoryTimingControls") {
                doLast {
                    val events = mutableListOf<Map<*, *>>()
                    val cases = mutableListOf<String>()
                    var now = 10_000_000L
                    val clock = { now }
                    val report: (String) -> Unit = { text ->
                        check(!text.contains("private-fixture-content"))
                        val event = groovy.json.JsonSlurper().parseText(text) as Map<*, *>
                        check(event.keys == setOf(
                            "event", "task", "category", "phase", "boundary", "clock",
                            "started_ns", "finished_ns", "wall_ms", "outcome"
                        ))
                        events.add(event)
                    }
                    fun number(event: Map<*, *>, key: String) = (event[key] as Number).toLong()
                    fun control(name: String, action: () -> Unit) {
                        action()
                        cases.add(name)
                    }
                    control("setup") {
                        TestCategoryFixtureTiming.measure("setup", report, clock) { now = 25_000_000L }
                        check(events.single()["outcome"] == "completed")
                        check(events.single()["task"] == "integrationTest")
                        check(number(events.single(), "started_ns") == 10_000_000L)
                        check(number(events.single(), "finished_ns") == 25_000_000L)
                        check(number(events.single(), "wall_ms") == 15L)
                    }
                    val original = AssertionError("private-fixture-content")
                    control("failure") {
                        now = 30_000_000L
                        val failure = runCatching {
                            TestCategoryFixtureTiming.measure("teardown", report, clock) {
                                now = 45_000_000L
                                throw original
                            }
                        }.exceptionOrNull()
                        check(failure === original)
                        check(events.last()["outcome"] == "failed")
                        check(number(events.last(), "wall_ms") == 15L)
                    }
                    control("recovery") {
                        now = 50_000_000L
                        TestCategoryFixtureTiming.measure("setup", report, clock) { now = 56_000_000L }
                        check(events.last()["outcome"] == "completed")
                        check(number(events.last(), "wall_ms") == 6L)
                    }
                    control("overlap") {
                        now = 100_000_000L
                        TestCategoryFixtureTiming.measure("setup", report, clock) {
                            now = 110_000_000L
                            TestCategoryFixtureTiming.measure("teardown", report, clock) { now = 130_000_000L }
                            now = 140_000_000L
                        }
                        val inner = events[events.size - 2]
                        val outer = events.last()
                        check(number(inner, "wall_ms") == 20L && number(outer, "wall_ms") == 40L)
                        check(number(outer, "started_ns") < number(inner, "started_ns"))
                        check(number(outer, "finished_ns") > number(inner, "finished_ns"))
                    }
                    control("zero") {
                        TestCategoryFixtureTiming.measure("setup", report, clock) {}
                        check(number(events.last(), "wall_ms") == 0L)
                    }
                    control("clock-order") {
                        val count = events.size
                        val failure = runCatching {
                            TestCategoryFixtureTiming.measure("setup", report, clock) { now-- }
                        }.exceptionOrNull()
                        check(failure is IllegalStateException && failure.message == "test-category-clock-order")
                        check(events.size == count)
                    }
                    val reporting = IllegalStateException("private-fixture-content")
                    val brokenReport: (String) -> Unit = { throw reporting }
                    control("reporting-failure") {
                        val failure = runCatching {
                            TestCategoryFixtureTiming.measure("setup", brokenReport, clock) {}
                        }.exceptionOrNull()
                        check(failure === reporting)
                    }
                    control("combined-failure") {
                        val failure = runCatching {
                            TestCategoryFixtureTiming.measure("teardown", brokenReport, clock) { throw original }
                        }.exceptionOrNull()
                        check(failure === original && original.suppressed.single() === reporting)
                    }
                    control("clock-failure") {
                        val brokenClock: () -> Long = { throw reporting }
                        var invoked = false
                        val failure = runCatching {
                            TestCategoryFixtureTiming.measure("setup", report, brokenClock) { invoked = true }
                        }.exceptionOrNull()
                        check(failure === reporting && !invoked)
                    }
                    control("phase-refusal") {
                        var invoked = false
                        val failure = runCatching {
                            TestCategoryFixtureTiming.measure("not-a-fixture", report, clock) { invoked = true }
                        }.exceptionOrNull()
                        check(failure is IllegalArgumentException && !invoked)
                    }
                    logger.lifecycle(groovy.json.JsonOutput.toJson(mapOf(
                        "event" to "category_timing_controls", "clock" to "synthetic", "cases" to cases
                    )))
                }
            }
            """.trimIndent()
    }
}
