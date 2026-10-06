package com.pennilogic.migration

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import kotlinx.serialization.json.Json as ProtocolJson

/** Bounded JSON with duplicate-key rejection, including differently escaped spellings of a key. */
internal object AdmissionJson {
    fun parse(
        bytes: ByteArray,
        limit: Int,
    ): JsonElement {
        admissionRequire(bytes.isNotEmpty() && bytes.size <= limit, AdmissionReason.JSON_SIZE)
        val text =
            try {
                Charsets.UTF_8
                    .newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes))
                    .toString()
            } catch (_: java.nio.charset.CharacterCodingException) {
                throw AdmissionRefused(AdmissionReason.JSON_INVALID)
            }
        return Parser(text).document()
    }

    private class Parser(
        private val text: String,
    ) {
        private var at = 0

        fun document(): JsonElement {
            val result = value(0)
            whitespace()
            admissionRequire(at == text.length)
            return result
        }

        private fun value(depth: Int): JsonElement {
            admissionRequire(depth <= 16)
            whitespace()
            admissionRequire(at < text.length)
            return when (text[at]) {
                '{' -> {
                    at++
                    val fields = linkedMapOf<String, JsonElement>()
                    if (!take('}')) {
                        do {
                            whitespace()
                            val key = string().content
                            admissionRequire(key !in fields)
                            expect(':')
                            fields[key] = value(depth + 1)
                        } while (take(','))
                        expect('}')
                    }
                    JsonObject(fields)
                }

                '[' -> {
                    at++
                    val items = mutableListOf<JsonElement>()
                    if (!take(']')) {
                        do {
                            admissionRequire(items.size < 4096)
                            items += value(depth + 1)
                        } while (take(','))
                        expect(']')
                    }
                    JsonArray(items)
                }

                '"' -> {
                    string()
                }

                't' -> {
                    literal("true", JsonPrimitive(true))
                }

                'f' -> {
                    literal("false", JsonPrimitive(false))
                }

                'n' -> {
                    literal("null", JsonNull)
                }

                else -> {
                    val token = NUMBER.find(text, at)
                    admissionRequire(token != null && token.range.first == at)
                    at += requireNotNull(token).value.length
                    primitive(token.value)
                }
            }
        }

        private fun string(): JsonPrimitive {
            admissionRequire(at < text.length && text[at] == '"')
            val start = at++
            while (at < text.length) {
                val character = text[at++]
                admissionRequire(character >= ' ')
                when (character) {
                    '\\' -> at++
                    '"' -> return primitive(text.substring(start, at))
                }
            }
            throw AdmissionRefused(AdmissionReason.JSON_INVALID)
        }

        private fun primitive(token: String): JsonPrimitive =
            try {
                ProtocolJson.parseToJsonElement(token) as? JsonPrimitive
                    ?: throw AdmissionRefused(AdmissionReason.JSON_INVALID)
            } catch (_: SerializationException) {
                throw AdmissionRefused(AdmissionReason.JSON_INVALID)
            }

        private fun literal(
            token: String,
            result: JsonElement,
        ): JsonElement {
            admissionRequire(text.startsWith(token, at))
            at += token.length
            return result
        }

        private fun take(character: Char): Boolean {
            whitespace()
            if (at >= text.length || text[at] != character) return false
            at++
            return true
        }

        private fun expect(character: Char) {
            admissionRequire(take(character))
        }

        private fun whitespace() {
            while (at < text.length && text[at] in " \t\r\n") at++
        }
    }

    private val NUMBER = Regex("""-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?""")
}

internal fun JsonElement.admissionObject(fields: Set<String>): JsonObject {
    val result = this as? JsonObject ?: throw AdmissionRefused(AdmissionReason.JSON_INVALID)
    admissionRequire(result.keys == fields)
    return result
}

internal fun JsonObject.admissionString(key: String): String {
    val value = get(key) as? JsonPrimitive
    admissionRequire(value != null && value.isString)
    return requireNotNull(value).content
}
