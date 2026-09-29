package com.pennilogic.bootstrap

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.io.IOException
import java.net.ServerSocket
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.concurrent.TimeUnit

class ApplicationProcessTest {
    @TempDir
    lateinit var temporary: Path

    private fun launch(
        environment: String?,
        port: Int,
    ): Process {
        val java = Path.of(System.getProperty("java.home"), "bin", "java").toString()
        val process =
            ProcessBuilder(
                java,
                "-cp",
                System.getProperty("app.test.classpath"),
                "com.pennilogic.bootstrap.ApplicationKt",
            )
        process.environment().apply {
            remove("JAVA_TOOL_OPTIONS")
            remove("_JAVA_OPTIONS")
            remove("JDK_JAVA_OPTIONS")
            remove("APP_ENV")
            if (environment != null) put("APP_ENV", environment)
            put("HOST", "127.0.0.1")
            put("PORT", port.toString())
        }
        return process.redirectErrorStream(true).redirectOutput(temporary.resolve("process.log").toFile()).start()
    }

    @Test
    fun `real entrypoint rejects missing required configuration before binding`() {
        val process = launch(null, 8080)
        try {
            assertTrue(process.waitFor(20, TimeUnit.SECONDS))
            assertEquals(1, process.exitValue())
            assertEquals(
                """{"event":"configuration_invalid","key":"APP_ENV"}""",
                Files.readString(temporary.resolve("process.log")).trim(),
            )
        } finally {
            process.destroyForcibly()
            process.waitFor(10, TimeUnit.SECONDS)
        }
    }

    @Test
    fun `real entrypoint binds loopback and serves the documented health contract`() {
        val port = ServerSocket(0).use { it.localPort }
        val process = launch("test", port)
        try {
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build().use { client ->
                val request =
                    HttpRequest
                        .newBuilder(URI("http://127.0.0.1:$port/health/ready"))
                        .timeout(Duration.ofSeconds(1))
                        .build()
                val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(25)
                var ready = false
                while (System.nanoTime() < deadline && process.isAlive) {
                    try {
                        val response = client.send(request, HttpResponse.BodyHandlers.ofString())
                        if (response.statusCode() == 200) {
                            assertEquals("""{"status":"ready"}""", response.body())
                            ready = true
                            break
                        }
                    } catch (_: IOException) {
                        // The socket is not bound yet; a bounded retry is expected during startup.
                    }
                    Thread.sleep(100)
                }
                assertTrue(ready, Files.readString(temporary.resolve("process.log")))
                val live =
                    client.send(
                        HttpRequest.newBuilder(URI("http://127.0.0.1:$port/health/live")).build(),
                        HttpResponse.BodyHandlers.ofString(),
                    )
                assertEquals(200, live.statusCode())
                assertEquals("""{"status":"up"}""", live.body())
                assertFalse(live.headers().firstValue("Server").isPresent)
            }
            val log = Files.readString(temporary.resolve("process.log"))
            assertTrue(log.contains("""{"event":"startup"}"""))
            assertTrue(log.contains("""{"event":"readiness","status":"ready"}"""))
        } finally {
            process.destroy()
            if (!process.waitFor(15, TimeUnit.SECONDS)) {
                process.destroyForcibly()
                assertTrue(process.waitFor(10, TimeUnit.SECONDS))
            }
        }
    }
}
