package com.pennilogic.bootstrap

class ConfigurationException(
    val key: String,
) : IllegalArgumentException("Invalid or missing configuration key: $key")

class ServerConfig private constructor(
    val environment: String,
    val host: String,
    val port: Int,
) {
    companion object {
        fun load(environment: Map<String, String>): ServerConfig {
            val appEnvironment = environment["APP_ENV"]
            if (appEnvironment !in setOf("local", "test", "staging", "production")) {
                throw ConfigurationException("APP_ENV")
            }
            val host = environment["HOST"] ?: "127.0.0.1"
            if (host !in setOf("127.0.0.1", "0.0.0.0")) {
                throw ConfigurationException("HOST")
            }
            val rawPort = environment["PORT"] ?: "8080"
            val port = rawPort.toIntOrNull() ?: throw ConfigurationException("PORT")
            if (port !in 1..65535 || rawPort != port.toString()) {
                throw ConfigurationException("PORT")
            }
            return ServerConfig(requireNotNull(appEnvironment), host, port)
        }
    }
}
