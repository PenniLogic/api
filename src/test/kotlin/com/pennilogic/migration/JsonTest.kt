package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class JsonTest {
    @Test
    fun `encodes every supported value shape deterministically`() {
        val encoded =
            Json.encode(
                linkedMapOf(
                    "null" to null,
                    "text" to "a\"b\\c\nd\re\tf\u0001g",
                    "flag" to true,
                    "int" to 42,
                    "long" to 9_000_000_000L,
                    "list" to listOf(1, "two", null),
                    "enum" to Phase.CONTRACT,
                ),
            )
        assertEquals(
            """{"null":null,"text":"a\"b\\c\nd\re\tf\u0001g","flag":true,"int":42,"long":9000000000,"list":[1,"two",null],"enum":"CONTRACT"}""",
            encoded,
        )
    }

    @Test
    fun `event puts the event name first`() {
        assertEquals("""{"event":"sample","version":3}""", Json.event("sample", "version" to 3))
    }
}
