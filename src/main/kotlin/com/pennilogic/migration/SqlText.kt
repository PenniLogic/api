package com.pennilogic.migration

/**
 * Removes SQL comments so the "script has statements" rule cannot be satisfied by a comment.
 * Handles `--` line comments and nested `/* */` block comments, and leaves quoted literals,
 * quoted identifiers and dollar-quoted bodies intact so a comment marker inside them is not
 * mistaken for a comment. The check stays syntactic: it decides whether anything remains, not
 * whether what remains is a meaningful reverse.
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

                    character == '\'' || character == '"' -> {
                        copyQuoted(script, index, character, out)
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

    private fun copyQuoted(
        script: String,
        start: Int,
        quote: Char,
        out: StringBuilder,
    ): Int {
        var index = start + 1
        out.append(quote)
        while (index < script.length) {
            val character = script[index]
            out.append(character)
            index++
            if (character == quote) {
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
