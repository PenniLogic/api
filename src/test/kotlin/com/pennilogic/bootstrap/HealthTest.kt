package com.pennilogic.bootstrap

import io.ktor.client.request.get
import io.ktor.client.request.post
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationStopPreparing
import io.ktor.server.testing.testApplication
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.net.ServerSocket
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

class HealthTest {
    @Test
    fun `configured Netty engine serves health and stops without retained state`() {
        val port = ServerSocket(0).use { it.localPort }
        val config = ServerConfig.load(mapOf("APP_ENV" to "test", "PORT" to port.toString()))
        val server = createServer(config)
        try {
            server.start(wait = false)
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build().use { client ->
                val request =
                    HttpRequest
                        .newBuilder(URI("http://127.0.0.1:$port/health/ready"))
                        .timeout(Duration.ofSeconds(3))
                        .build()
                val response = client.send(request, HttpResponse.BodyHandlers.ofString())
                assertEquals(200, response.statusCode())
                assertEquals("""{"status":"ready"}""", response.body())
            }
        } finally {
            server.stop(0, 1000)
        }
    }

    @Test
    fun `readiness transitions are safe and idempotent`() {
        val readiness = Readiness()
        assertFalse(readiness.isReady())
        readiness.started()
        readiness.started()
        assertTrue(readiness.isReady())
        readiness.stopping()
        readiness.stopping()
        assertFalse(readiness.isReady())
    }

    @Test
    fun `started application exposes only fixed non-disclosing health responses`() =
        testApplication {
            application { healthModule() }
            for ((path, body) in listOf("/health/live" to """{"status":"up"}""", "/health/ready" to """{"status":"ready"}""")) {
                val response = client.get(path)
                assertEquals(HttpStatusCode.OK, response.status)
                assertEquals(ContentType.Application.Json, response.contentType()?.withoutParameters())
                assertEquals(body, response.bodyAsText())
                assertEquals("no-store", response.headers[HttpHeaders.CacheControl])
                assertEquals(null, response.headers[HttpHeaders.Server])
            }
            assertEquals(HttpStatusCode.NotFound, client.get("/").status)
            assertEquals(HttpStatusCode.NotFound, client.get("/users").status)
            assertEquals(HttpStatusCode.MethodNotAllowed, client.post("/health/ready").status)
        }

    @Test
    fun `shutdown removes readiness while liveness remains available`() =
        testApplication {
            val readiness = Readiness()
            lateinit var runningApplication: Application
            application {
                runningApplication = this
                healthModule(readiness)
            }
            startApplication()
            assertTrue(readiness.isReady())
            runningApplication.monitor.raise(ApplicationStopPreparing, runningApplication.environment)
            val response = client.get("/health/ready")
            assertEquals(HttpStatusCode.ServiceUnavailable, response.status)
            assertEquals("""{"status":"not_ready"}""", response.bodyAsText())
            assertEquals("no-store", response.headers[HttpHeaders.CacheControl])
            assertEquals(HttpStatusCode.OK, client.get("/health/live").status)
        }
}
