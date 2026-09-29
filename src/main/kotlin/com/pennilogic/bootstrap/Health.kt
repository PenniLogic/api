package com.pennilogic.bootstrap

import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationStarted
import io.ktor.server.application.ApplicationStopPreparing
import io.ktor.server.response.respondText
import io.ktor.server.routing.get
import io.ktor.server.routing.routing
import org.slf4j.LoggerFactory
import java.util.concurrent.atomic.AtomicBoolean

class Readiness {
    private val accepting = AtomicBoolean(false)

    fun isReady(): Boolean = accepting.get()

    fun started() {
        accepting.set(true)
    }

    fun stopping() {
        accepting.set(false)
    }
}

fun Application.healthModule(readiness: Readiness = Readiness()) {
    val logger = LoggerFactory.getLogger("pennilogic.lifecycle")
    logger.info("""{"event":"startup"}""")
    monitor.subscribe(ApplicationStarted) {
        readiness.started()
        logger.info("""{"event":"readiness","status":"ready"}""")
    }
    monitor.subscribe(ApplicationStopPreparing) {
        readiness.stopping()
        logger.info("""{"event":"readiness","status":"not_ready"}""")
    }
    routing {
        get("/health/live") {
            call.response.headers.append(HttpHeaders.CacheControl, "no-store")
            call.respondText("""{"status":"up"}""", ContentType.Application.Json)
        }
        get("/health/ready") {
            call.response.headers.append(HttpHeaders.CacheControl, "no-store")
            if (readiness.isReady()) {
                call.respondText("""{"status":"ready"}""", ContentType.Application.Json)
            } else {
                call.respondText(
                    """{"status":"not_ready"}""",
                    ContentType.Application.Json,
                    HttpStatusCode.ServiceUnavailable,
                )
            }
        }
    }
}
