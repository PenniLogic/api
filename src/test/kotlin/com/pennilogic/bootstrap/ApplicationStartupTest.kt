package com.pennilogic.bootstrap

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class ApplicationStartupTest {
    @Test
    fun `startup passes validated configuration to the engine`() {
        var captured: ServerConfig? = null
        val result = runApplication(mapOf("APP_ENV" to "local")) { captured = it }
        assertEquals(0, result)
        assertNotNull(captured)
        assertEquals("local", captured?.environment)
        assertEquals(8080, captured?.port)
    }

    @Test
    fun `configuration failure returns a failing exit code without starting an engine`() {
        var started = false
        assertEquals(1, runApplication(emptyMap()) { started = true })
        assertFalse(started)
    }

    @Test
    fun `engine startup failure is not disguised as a successful launch`() {
        assertThrows(IllegalStateException::class.java) {
            runApplication(mapOf("APP_ENV" to "test")) { error("Synthetic engine startup failure") }
        }
    }
}
