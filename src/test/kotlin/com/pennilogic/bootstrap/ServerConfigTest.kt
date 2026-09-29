package com.pennilogic.bootstrap

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.ValueSource

class ServerConfigTest {
    @Test
    fun `missing required environment fails without exposing values`() {
        val error = assertThrows(ConfigurationException::class.java) { ServerConfig.load(emptyMap()) }
        assertEquals("APP_ENV", error.key)
        assertEquals("Invalid or missing configuration key: APP_ENV", error.message)
    }

    @ParameterizedTest
    @ValueSource(strings = ["local", "test", "staging", "production"])
    fun `accepted environments use safe local defaults`(environment: String) {
        val config = ServerConfig.load(mapOf("APP_ENV" to environment))
        assertEquals(environment, config.environment)
        assertEquals("127.0.0.1", config.host)
        assertEquals(8080, config.port)
    }

    @ParameterizedTest
    @ValueSource(strings = ["", " ", "LOCAL", "local ", "invalid-config-value"])
    fun `invalid environment is rejected without echoing its value`(value: String) {
        val error =
            assertThrows(ConfigurationException::class.java) {
                ServerConfig.load(mapOf("APP_ENV" to value))
            }
        assertEquals("APP_ENV", error.key)
        assertFalse(error.message.orEmpty().contains("invalid-config-value"))
    }

    @ParameterizedTest
    @ValueSource(strings = ["1", "8080", "65535"])
    fun `valid port boundaries and container host are accepted`(port: String) {
        val config = ServerConfig.load(mapOf("APP_ENV" to "test", "HOST" to "0.0.0.0", "PORT" to port))
        assertEquals("0.0.0.0", config.host)
        assertEquals(port.toInt(), config.port)
    }

    @ParameterizedTest
    @ValueSource(strings = ["", " ", "0", "-1", "65536", "2147483648", "eight", "80.0", " 80", "+80", "080"])
    fun `invalid ports fail closed`(port: String) {
        val error =
            assertThrows(ConfigurationException::class.java) {
                ServerConfig.load(mapOf("APP_ENV" to "test", "PORT" to port))
            }
        assertEquals("PORT", error.key)
    }

    @ParameterizedTest
    @ValueSource(strings = ["", "localhost", "example.invalid", "::", "0.0.0.0 "])
    fun `unsupported bind hosts are rejected`(host: String) {
        val error =
            assertThrows(ConfigurationException::class.java) {
                ServerConfig.load(mapOf("APP_ENV" to "test", "HOST" to host))
            }
        assertEquals("HOST", error.key)
    }
}
