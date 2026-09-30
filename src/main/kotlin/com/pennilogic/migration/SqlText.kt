package com.pennilogic.migration

/**
 * Removes SQL comments so the "script has statements" rule cannot be satisfied by a comment.
 * Handles `--` line comments and nested `/* */` block comments, and leaves quoted literals
 * (including `E'…'` escape strings, where a backslash escapes the next character), quoted
 * identifiers and dollar-quoted bodies intact so a comment marker inside them is not mistaken
 * for a comment and a statement after them is not mistaken for literal text. Plain literals
 * follow `standard_conforming_strings = on`, which the runner sets for its session. The check
 * stays syntactic: it decides whether anything remains, not whether what remains is a
 * meaningful reverse.
 */
internal object SqlText {
    private val DOLLAR_TAG = Regex("""\$([A-Za-z_][A-Za-z0-9_]*)?\$""")

    fun withoutComments(script: String): String {
        val out = StringBuilder()
        var index = 0
        while (index < script.length) {
            val character = script[index]
            index =
                when {
                    script.startsWith("--", index) -> {
                        skipLineComment(script, index)
                    }

                    script.startsWith("/*", index) -> {
                        skipBlockComment(script, index)
                    }

                    character == '\'' -> {
                        copyQuoted(script, index, character, out, escapes = isEscapeString(script, index))
                    }

                    character == '"' -> {
                        copyQuoted(script, index, character, out, escapes = false)
                    }

                    character == '$' -> {
                        copyDollarQuoted(script, index, out)
                    }

                    else -> {
                        out.append(character)
                        index + 1
                    }
                }
        }
        return out.toString()
    }

    private fun skipLineComment(
        script: String,
        start: Int,
    ): Int {
        val end = script.indexOf('\n', start)
        return if (end < 0) script.length else end
    }

    private fun skipBlockComment(
        script: String,
        start: Int,
    ): Int {
        var depth = 1
        var index = start + 2
        while (index < script.length && depth > 0) {
            when {
                script.startsWith("/*", index) -> {
                    depth++
                    index += 2
                }

                script.startsWith("*/", index) -> {
                    depth--
                    index += 2
                }

                else -> {
                    index++
                }
            }
        }
        // An unterminated block comment swallows the rest of the script, as it does for Postgres.
        return index
    }

    /** `E'…'` (or `e'…'`) is an escape string when the E is not the tail of an identifier. */
    private fun isEscapeString(
        script: String,
        quoteIndex: Int,
    ): Boolean {
        if (quoteIndex == 0 || script[quoteIndex - 1] !in "Ee") return false
        return quoteIndex == 1 || !isIdentifierCharacter(script[quoteIndex - 2])
    }

    private fun isIdentifierCharacter(character: Char): Boolean = character.isLetterOrDigit() || character == '_' || character == '$'

    private fun copyQuoted(
        script: String,
        start: Int,
        quote: Char,
        out: StringBuilder,
        escapes: Boolean,
    ): Int {
        var index = start + 1
        out.append(quote)
        while (index < script.length) {
            val character = script[index]
            out.append(character)
            index++
            if (escapes && character == '\\' && index < script.length) {
                // A backslash escapes the next character in an E'…' string, so \' does not close it.
                out.append(script[index])
                index++
            } else if (character == quote) {
                if (index < script.length && script[index] == quote) {
                    out.append(quote)
                    index++
                } else {
                    return index
                }
            }
        }
        return index
    }

    private fun copyDollarQuoted(
        script: String,
        start: Int,
        out: StringBuilder,
    ): Int {
        val tag = DOLLAR_TAG.matchAt(script, start)?.value
        if (tag == null) {
            out.append('$')
            return start + 1
        }
        val close = script.indexOf(tag, start + tag.length)
        val end = if (close < 0) script.length else close + tag.length
        out.append(script, start, end)
        return end
    }
}
