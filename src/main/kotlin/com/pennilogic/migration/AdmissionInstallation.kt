package com.pennilogic.migration

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.intOrNull
import java.io.IOException
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path
import java.nio.file.attribute.BasicFileAttributes
import java.security.MessageDigest

/** The compiled resource is authority; the fixed build directory is only a verified materialization. */
internal class AdmissionInstallation(
    private val root: Path,
    definition: ByteArray,
) {
    private val definition = definition.copyOf()
    private val pins: InstallationPins = pins()
    private val bundle = root.resolve("build").resolve("database-admission")

    fun admission(): DatabaseAdmission {
        val inputs = verify().inputs
        return DatabaseAdmission(inputs.getValue("policy"), inputs.getValue("inventory")) { command, request ->
            val snapshot = verify()
            AdmissionProcess().run(
                listOf(
                    "python",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    LAUNCHER_BOOTSTRAP,
                    root.resolve(LAUNCHER_PATH).toString(),
                    command,
                    snapshot.launcher.size.toString(),
                ),
                root,
                request,
                snapshot.launcher,
            )
        }
    }

    internal fun verify(): Verified {
        try {
            val disk = read(root, INSTALLATION_PATH, DatabaseAdmission.REQUEST_LIMIT)
            admissionRequire(disk.contentEquals(definition), AdmissionReason.SOURCE_CHANGED)
            val launcher = read(root, LAUNCHER_PATH, pins.launcher.bytes)
            admissionRequire(
                launcher.size == pins.launcher.bytes && digest(launcher) == pins.launcher.sha256,
                AdmissionReason.SOURCE_CHANGED,
            )
            safeAttributes(root, "build").also { admissionRequire(it.isDirectory, AdmissionReason.SOURCE_INVALID) }
            safeAttributes(root, "build/database-admission").also { admissionRequire(it.isDirectory, AdmissionReason.SOURCE_INVALID) }
            val actual =
                Files.walk(bundle, 3).use { paths ->
                    paths.limit(9).map { bundle.relativize(it).joinToString("/") }.toList()
                }
            admissionRequire(actual.toSet() == PAYLOAD_PATHS + setOf("", "scripts", "database"), AdmissionReason.SOURCE_INVALID)
            val contents =
                pins.payloads.mapValues { (name, pin) ->
                    val content = read(bundle, name, pin.bytes)
                    admissionRequire(content.size == pin.bytes && digest(content) == pin.sha256, AdmissionReason.SOURCE_CHANGED)
                    content
                }
            return Verified(
                AdmissionJson
                    .parse(contents.getValue("inputs.json"), DatabaseAdmission.REQUEST_LIMIT)
                    .admissionObject(setOf("policy", "inventory")),
                launcher,
            )
        } catch (_: IOException) {
            throw AdmissionRefused(AdmissionReason.SOURCE_UNAVAILABLE)
        }
    }

    private fun pins(): InstallationPins {
        val document =
            AdmissionJson
                .parse(definition, DatabaseAdmission.REQUEST_LIMIT)
                .admissionObject(setOf("schema", "gate", "runtime", "consumer", "launcher", "binding"))
        val consumer = document.getValue("consumer").admissionObject(setOf("repository", "repository_id", "organization_id"))
        admissionRequire(
            document.admissionString("schema") == "pennilogic.database-admission.installation/1" &&
                document.admissionString("gate") == "T-PLT-01-DATABASE-EXTENSION-GATE" &&
                document.admissionString("runtime") == DatabaseAdmission.RUNTIME &&
                consumer.admissionString("repository") == "PenniLogic/api" &&
                consumer["repository_id"] == JsonPrimitive(1394134582) &&
                consumer["organization_id"] == JsonPrimitive(335295566),
            AdmissionReason.SOURCE_INVALID,
        )
        val launcher = pin(document.getValue("launcher").admissionObject(PIN_FIELDS), setOf(LAUNCHER_PATH))
        admissionRequire(document["binding"] != JsonNull, AdmissionReason.SOURCE_UNAVAILABLE)
        val binding = document.getValue("binding").admissionObject(setOf("sources", "payloads"))
        binding.getValue("sources").admissionObject(setOf("infra", "policy", "inventory", "evidence"))
        val payloads = binding["payloads"] as? JsonArray ?: throw AdmissionRefused(AdmissionReason.SOURCE_INVALID)
        admissionRequire(payloads.size == PAYLOAD_PATHS.size, AdmissionReason.SOURCE_INVALID)
        val result = linkedMapOf<String, Pin>()
        for (item in payloads) {
            val pin = pin(item.admissionObject(PIN_FIELDS), PAYLOAD_PATHS)
            admissionRequire(pin.path !in result, AdmissionReason.SOURCE_INVALID)
            result[pin.path] = pin
        }
        return InstallationPins(result, launcher)
    }

    private fun pin(
        record: JsonObject,
        paths: Set<String>,
    ): Pin {
        val name = record.admissionString("path")
        val bytes = record["bytes"] as? JsonPrimitive
        val size = bytes?.takeUnless { it.isString }?.intOrNull
        val hash = record.admissionString("sha256")
        admissionRequire(
            name in paths && record.admissionString("mode") == "100644" &&
                size != null && size in 1..DatabaseAdmission.REQUEST_LIMIT && DatabaseAdmission.SHA256.matches(hash),
            AdmissionReason.SOURCE_INVALID,
        )
        return Pin(name, size, hash)
    }

    private data class Pin(
        val path: String,
        val bytes: Int,
        val sha256: String,
    )

    private data class InstallationPins(
        val payloads: Map<String, Pin>,
        val launcher: Pin,
    )

    internal data class Verified(
        val inputs: JsonObject,
        val launcher: ByteArray,
    )

    companion object {
        private const val INSTALLATION = "database-admission-installation.json"
        private const val INSTALLATION_PATH = "src/main/resources/$INSTALLATION"
        private const val LAUNCHER_PATH = "scripts/prepare_database_admission.py"
        private val PIN_FIELDS = setOf("path", "mode", "bytes", "sha256")

        // Execute the verified snapshot, not a subsequently reopened mutable launcher path.
        private val LAUNCHER_BOOTSTRAP =
            """
            import io, sys, types
            length = int(sys.argv[3])
            source = sys.stdin.buffer.read(length)
            request = sys.stdin.buffer.read(2097153)
            if len(source) != length or len(request) > 2097152:
                sys.exit(1)
            sys.stdin = io.TextIOWrapper(io.BytesIO(request), encoding='utf-8', errors='strict')
            sys.argv = [sys.argv[1], 'run', sys.argv[2]]
            module = types.ModuleType('__main__')
            module.__file__ = sys.argv[0]
            sys.modules['__main__'] = module
            exec(compile(source, module.__file__, 'exec'), module.__dict__)
            """.trimIndent()
        private val PAYLOAD_PATHS =
            setOf(
                "scripts/database_admission.py",
                "scripts/database_sql.py",
                "scripts/database_baseline.py",
                "database/admission-trust.json",
                "inputs.json",
            )

        fun load(): DatabaseAdmission {
            val resources =
                try {
                    AdmissionInstallation::class.java.classLoader
                        .getResources(INSTALLATION)
                        .toList()
                } catch (_: IOException) {
                    throw AdmissionRefused(AdmissionReason.SOURCE_UNAVAILABLE)
                }
            admissionRequire(resources.size == 1, AdmissionReason.SOURCE_UNAVAILABLE)
            val definition =
                try {
                    resources.single().openStream().use { it.readNBytes(DatabaseAdmission.REQUEST_LIMIT + 1) }
                } catch (_: IOException) {
                    throw AdmissionRefused(AdmissionReason.SOURCE_UNAVAILABLE)
                }
            return AdmissionInstallation(Path.of("").toAbsolutePath().normalize(), definition).admission()
        }

        private fun safeAttributes(
            root: Path,
            relative: String,
        ): BasicFileAttributes {
            var current = root
            val parts = relative.split("/")
            var attributes = Files.readAttributes(current, BasicFileAttributes::class.java, NOFOLLOW_LINKS)
            admissionRequire(!attributes.isSymbolicLink && !attributes.isOther && attributes.isDirectory, AdmissionReason.SOURCE_INVALID)
            for ((index, part) in parts.withIndex()) {
                current = current.resolve(part)
                attributes = Files.readAttributes(current, BasicFileAttributes::class.java, NOFOLLOW_LINKS)
                admissionRequire(!attributes.isSymbolicLink && !attributes.isOther, AdmissionReason.SOURCE_INVALID)
                if (index < parts.lastIndex) admissionRequire(attributes.isDirectory, AdmissionReason.SOURCE_INVALID)
            }
            return attributes
        }

        private fun read(
            root: Path,
            relative: String,
            limit: Int,
        ): ByteArray {
            admissionRequire(safeAttributes(root, relative).isRegularFile, AdmissionReason.SOURCE_INVALID)
            return Files.newInputStream(root.resolve(relative), NOFOLLOW_LINKS).use { stream ->
                val bytes = stream.readNBytes(limit + 1)
                admissionRequire(bytes.size <= limit, AdmissionReason.SOURCE_CHANGED)
                bytes
            }
        }

        private fun digest(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    }
}
