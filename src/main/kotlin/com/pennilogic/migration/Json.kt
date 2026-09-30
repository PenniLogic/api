package com.pennilogic.migration

/**
 * Minimal single-line JSON encoder for runner events. It has no dependency and no
 * reflection so the runner's output shape is exactly what the registry contract documents.
 */
object Json {
    fun encode(value: Any?): String =
        when (value) {
            null -> {
                "null"
            }

            is String -> {
                quote(value)
            }

            is Boolean -> {
                value.toString()
            }

            is Number -> {
                value.toString()
            }

            is Map<*, *> -> {
                value.entries.joinToString(",", "{", "}") { entry ->
                    quote(entry.key.toString()) + ":" + encode(entry.value)
                }
            }

            is Iterable<*> -> {
                value.joinToString(",", "[", "]") { encode(it) }
            }

            else -> {
                quote(value.toString())
            }
        }

    fun event(
        name: String,
        vararg fields: Pair<String, Any?>,
    ): String = encode(linkedMapOf("event" to name, *fields))

    private fun quote(text: String): String {
        val out = StringBuilder("\"")
        for (character in text) {
            when {
                character == '"' -> out.append("\\\"")
                character == '\\' -> out.append("\\\\")
                character == '\n' -> out.append("\\n")
                character == '\r' -> out.append("\\r")
                character == '\t' -> out.append("\\t")
                character < ' ' -> out.append(String.format("\\u%04x", character.code))
                else -> out.append(character)
            }
        }
        return out.append('"').toString()
    }
}
