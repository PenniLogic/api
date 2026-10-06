package com.pennilogic.migration

import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path
import java.util.Collections

/**
 * The ordered, validated migrations of one directory. Loading enforces the naming, ordering,
 * header and reversal-evidence convention published in docs/migrations/CONVENTION.md, then
 * requires real complete-set admission before exposing immutable execution buffers.
 */
class MigrationSet private constructor(
    val directory: Path,
    migrations: List<Migration>,
    private val admission: DatabaseAdmission,
) {
    val migrations: List<Migration> = Collections.unmodifiableList(migrations.toList())
    val latestVersion: Int get() = migrations.lastOrNull()?.version ?: 0

    fun byVersion(version: Int): Migration? = migrations.firstOrNull { it.version == version }

    fun records(): List<Map<String, Any?>> = migrations.map { it.record() }

    internal fun validateAdmission() {
        verifyFiles()
        admission.validate(migrations)
        verifyFiles()
    }

    internal fun authorize(selection: List<AdmissionSelection>) {
        verifyFiles()
        admission.authorize(migrations, selection)
        verifyFiles()
    }

    internal fun verifyFiles() {
        try {
            val expected = migrations.flatMap { listOf(it.file, it.reversal.file) }.sorted()
            val actual = names(directory)
            admissionRequire(actual == expected, AdmissionReason.MIGRATIONS_CHANGED)
            for (migration in migrations) {
                admissionRequire(
                    readScript(directory.resolve(migration.file)) == migration.sql &&
                        readScript(directory.resolve(migration.reversal.file)) == migration.reversal.sql,
                    AdmissionReason.MIGRATIONS_CHANGED,
                )
            }
        } catch (_: IOException) {
            throw AdmissionRefused(AdmissionReason.MIGRATIONS_CHANGED)
        } catch (_: AdmissionRefused) {
            throw AdmissionRefused(AdmissionReason.MIGRATIONS_CHANGED)
        }
    }

    companion object {
        private val FILE_NAME = Regex("""^(V(\d{3,})__([a-z][a-z0-9]*(?:_[a-z0-9]+)*))\.(up|down|compensating)\.sql$""")
        private val DIRECTIVE = Regex("""^--\s*([a-z]+):\s*(.*?)\s*$""")
        private val EXPAND_REFERENCE = Regex("""^V(\d{3,})$""")
        private val REQUIRED_DIRECTIVES = listOf("phase", "owner", "reversal")
        private val KNOWN_DIRECTIVES = REQUIRED_DIRECTIVES + listOf("reason", "expand")

        // DROP ... CASCADE and TRUNCATE ... CASCADE within one statement; ON DELETE CASCADE in a constraint is not matched.
        private val DESTRUCTIVE_CASCADE = Regex("""\b(?:DROP|TRUNCATE)\b[^;]*\bCASCADE\b""", RegexOption.IGNORE_CASE)

        fun label(version: Int): String = "V" + version.toString().padStart(3, '0')

        fun load(directory: Path): MigrationSet = load(directory, AdmissionInstallation::load)

        internal fun load(
            directory: Path,
            admission: () -> DatabaseAdmission,
        ): MigrationSet {
            if (!Files.isDirectory(directory)) {
                throw ConventionViolation(directory.toString(), "migrations directory does not exist")
            }
            val names = names(directory)
            val files = sortedMapOf<Int, MutableMap<String, String>>()
            val ids = mutableMapOf<Int, String>()
            for (name in names) {
                val match =
                    FILE_NAME.matchEntire(name)
                        ?: throw ConventionViolation(name, "file name must match V###__snake_case_name.(up|down|compensating).sql")
                val (id, digits, _, kind) = match.destructured
                val version = digits.toIntOrNull() ?: throw ConventionViolation(name, "version is out of range")
                if (version == 0) {
                    throw ConventionViolation(name, "version must be at least ${label(1)}")
                }
                val knownId = ids.putIfAbsent(version, id)
                if (knownId != null && knownId != id) {
                    throw ConventionViolation(name, "files for version ${label(version)} disagree on the name: $knownId")
                }
                files.getOrPut(version) { mutableMapOf() }[kind] = name
            }
            val migrations = mutableListOf<Migration>()
            var expected = 1
            var sqlBytes = 0
            for (entry in files.entries) {
                val version = entry.key
                if (version != expected) {
                    throw ConventionViolation(
                        entry.value.values.first(),
                        "versions must be contiguous from ${label(1)}; ${label(version)} follows ${label(expected - 1)}",
                    )
                }
                val migration = build(directory, version, ids.getValue(version), entry.value, migrations)
                sqlBytes += migration.sql.toByteArray(Charsets.UTF_8).size +
                    migration.reversal.sql
                        .toByteArray(Charsets.UTF_8)
                        .size
                admissionRequire(sqlBytes <= DatabaseAdmission.REQUEST_LIMIT, AdmissionReason.INPUT_INVALID)
                migrations += migration
                expected++
            }
            return MigrationSet(directory, migrations, admission()).also { it.validateAdmission() }
        }

        private fun names(directory: Path): List<String> =
            Files.list(directory).use { stream ->
                val names =
                    stream
                        .limit(257)
                        .map { it.fileName.toString() }
                        .sorted()
                        .toList()
                admissionRequire(names.size <= 256, AdmissionReason.INPUT_INVALID)
                names
            }

        private fun readScript(path: Path): String {
            admissionRequire(Files.isRegularFile(path, NOFOLLOW_LINKS), AdmissionReason.INPUT_INVALID)
            val bytes =
                Files.newInputStream(path, NOFOLLOW_LINKS).use { it.readNBytes(DatabaseAdmission.SQL_LIMIT * 2 + 1) }
            admissionRequire(bytes.size <= DatabaseAdmission.SQL_LIMIT * 2, AdmissionReason.INPUT_INVALID)
            val script =
                try {
                    Charsets.UTF_8
                        .newDecoder()
                        .decode(java.nio.ByteBuffer.wrap(bytes))
                        .toString()
                        .replace("\r\n", "\n")
                } catch (_: java.nio.charset.CharacterCodingException) {
                    throw AdmissionRefused(AdmissionReason.INPUT_INVALID)
                }
            admissionRequire(script.toByteArray(Charsets.UTF_8).size <= DatabaseAdmission.SQL_LIMIT, AdmissionReason.INPUT_INVALID)
            return script
        }

        private fun build(
            directory: Path,
            version: Int,
            id: String,
            scripts: Map<String, String>,
            earlier: List<Migration>,
        ): Migration {
            val upName = scripts["up"] ?: throw ConventionViolation(scripts.values.first(), "version ${label(version)} has no up script")
            val downName = scripts["down"]
            val compensatingName = scripts["compensating"]
            if (downName != null && compensatingName != null) {
                throw ConventionViolation(compensatingName, "declare exactly one reversal script, a down script or a compensating script")
            }
            val reversalName =
                downName ?: compensatingName
                    ?: throw ConventionViolation(
                        upName,
                        "reversal evidence is required: add $id.down.sql or $id.compensating.sql with a reason directive",
                    )
            val upSql = readScript(directory.resolve(upName))
            val header = parseHeader(upName, upSql)
            val phase =
                Phase.entries.firstOrNull { it.directive == header["phase"] }
                    ?: throw ConventionViolation(upName, "phase must be expand, migrate or contract")
            val declaredKind =
                ReversalKind.entries.firstOrNull { it.directive == header["reversal"] }
                    ?: throw ConventionViolation(upName, "reversal must be down or compensating")
            val kind = if (downName != null) ReversalKind.DOWN else ReversalKind.COMPENSATING
            if (declaredKind != kind) {
                throw ConventionViolation(upName, "declares reversal ${declaredKind.directive} but the reversal script is $reversalName")
            }
            val reason = header["reason"]
            if (kind == ReversalKind.COMPENSATING && reason == null) {
                throw ConventionViolation(upName, "a compensating reversal must state why the exact reverse is impossible (reason: ...)")
            }
            if (kind == ReversalKind.DOWN && reason != null) {
                throw ConventionViolation(upName, "reason is only valid for a compensating reversal")
            }
            val expandVersion = expandVersion(upName, phase, header["expand"], earlier)
            if (statements(upSql).isEmpty()) {
                throw ConventionViolation(upName, "script has no SQL statements")
            }
            val reversalSql = readScript(directory.resolve(reversalName))
            val reversalStatements = statements(reversalSql)
            if (reversalStatements.isEmpty()) {
                throw ConventionViolation(reversalName, "reversal script has no SQL statements")
            }
            if (DESTRUCTIVE_CASCADE.containsMatchIn(reversalStatements.joinToString(" "))) {
                throw ConventionViolation(
                    reversalName,
                    "reversal scripts must not DROP or TRUNCATE with CASCADE: a reverse must fail rather than destroy objects or rows it does not own",
                )
            }
            return Migration(
                version = version,
                id = id,
                name = id.substringAfter("__"),
                phase = phase,
                owner = header.getValue("owner"),
                expandVersion = expandVersion,
                file = upName,
                sql = upSql,
                checksum = Checksum.of(upSql),
                reversal = Reversal(kind, reversalName, reversalSql, Checksum.of(reversalSql), reason),
            )
        }

        private fun expandVersion(
            file: String,
            phase: Phase,
            reference: String?,
            earlier: List<Migration>,
        ): Int? {
            if (reference == null) {
                if (phase == Phase.CONTRACT) {
                    throw ConventionViolation(file, "a contract migration must name the expand migration it completes (expand: V###)")
                }
                return null
            }
            if (phase == Phase.EXPAND) {
                throw ConventionViolation(file, "the expand directive is only valid for migrate and contract migrations")
            }
            val target =
                EXPAND_REFERENCE.matchEntire(reference)?.let { match -> match.groupValues[1].toIntOrNull() }
                    ?: throw ConventionViolation(file, "expand must name a version like ${label(1)}")
            val expand =
                earlier.firstOrNull { it.version == target }
                    ?: throw ConventionViolation(file, "expand names $reference, which is not an earlier migration of this set")
            if (expand.phase != Phase.EXPAND) {
                throw ConventionViolation(file, "expand names $reference, whose phase is ${expand.phase.directive} rather than expand")
            }
            return expand.version
        }

        private fun parseHeader(
            file: String,
            script: String,
        ): Map<String, String> {
            val directives = mutableMapOf<String, String>()
            for (line in script.lineSequence()) {
                val trimmed = line.trim()
                if (trimmed.isEmpty()) continue
                if (!trimmed.startsWith("--")) break
                val match = DIRECTIVE.matchEntire(trimmed) ?: continue
                val (key, value) = match.destructured
                if (key !in KNOWN_DIRECTIVES) {
                    throw ConventionViolation(file, "unknown header directive '$key'")
                }
                if (value.isEmpty()) {
                    throw ConventionViolation(file, "header directive '$key' has no value")
                }
                if (directives.putIfAbsent(key, value) != null) {
                    throw ConventionViolation(file, "duplicate header directive '$key'")
                }
            }
            for (required in REQUIRED_DIRECTIVES) {
                if (required !in directives) {
                    throw ConventionViolation(file, "missing header directive '$required'")
                }
            }
            return directives
        }

        private fun statements(script: String): List<String> =
            SqlText
                .withoutComments(script)
                .lines()
                .map { it.trim() }
                .filter { it.isNotEmpty() }
    }
}
