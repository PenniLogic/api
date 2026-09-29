package com.pennilogic.bootstrap

import io.ktor.server.engine.connector
import io.ktor.server.engine.embeddedServer
import io.ktor.server.netty.Netty
import org.slf4j.LoggerFactory
import kotlin.system.exitProcess

fun main() {
    exitProcess(runApplication(System.getenv()) { createServer(it).start(wait = true) })
}

fun runApplication(
    environment: Map<String, String>,
    start: (ServerConfig) -> Unit,
): Int {
    val config =
        try {
            ServerConfig.load(environment)
        } catch (error: ConfigurationException) {
            LoggerFactory.getLogger("pennilogic.lifecycle").error(
                """{"event":"configuration_invalid","key":"${error.key}"}""",
            )
            return 1
        }
    start(config)
    return 0
}

fun createServer(config: ServerConfig) =
    embeddedServer(
        Netty,
        configure = {
            connector {
                host = config.host
                port = config.port
            }
            shutdownGracePeriod = 1000
            shutdownTimeout = 5000
        },
    ) {
        healthModule()
    }
