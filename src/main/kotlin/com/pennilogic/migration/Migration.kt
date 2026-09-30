package com.pennilogic.migration

import java.nio.charset.StandardCharsets
import java.security.MessageDigest

/** Expand-migrate-contract phase declared by every migration. */
enum class Phase {
    EXPAND,
    MIGRATE,
    CONTRACT,
    ;

    val directive: String get() = name.lowercase()
}

/** How a migration is reversed: a rehearsed down script or a named compensating script with a reason. */
enum class ReversalKind {
    DOWN,
    COMPENSATING,
    ;

    val directive: String get() = name.lowercase()
}

/** A migration set that violates the published convention; the message always names the file. */
class ConventionViolation(
    val file: String,
    val rule: String,
) : IllegalArgumentException("$file: $rule")

data class Reversal(
    val kind: ReversalKind,
    val file: String,
    val sql: String,
    val checksum: String,
    val reason: String?,
)

data class Migration(
    val version: Int,
    val id: String,
    val name: String,
    val phase: Phase,
    val owner: String,
    val expandVersion: Int?,
    val file: String,
    val sql: String,
    val checksum: String,
    val reversal: Reversal,
) {
    /** The machine-readable registry record shared with the pull-request check. */
    fun record(): Map<String, Any?> =
        linkedMapOf(
            "version" to version,
            "id" to id,
            "name" to name,
            "phase" to phase.directive,
            "owner" to owner,
            "expand" to expandVersion,
            "file" to file,
            "checksum" to checksum,
            "reversal" to
                linkedMapOf(
                    "kind" to reversal.kind.directive,
                    "file" to reversal.file,
                    "checksum" to reversal.checksum,
                    "reason" to reversal.reason,
                ),
        )
}

object Checksum {
    /** SHA-256 of the script with line endings normalised to LF, so the value is platform independent. */
    fun of(script: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val bytes = digest.digest(script.replace("\r\n", "\n").toByteArray(StandardCharsets.UTF_8))
        return bytes.joinToString("") { "%02x".format(it) }
    }
}
