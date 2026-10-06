package com.pennilogic.migration

import java.io.File
import java.io.IOException
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.InvalidPathException
import java.nio.file.Path
import java.time.Duration
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/** Internal transport primitive; only the installation builds production commands from fixed, reviewed source. */
internal class AdmissionProcess(
    private val timeout: Duration = Duration.ofSeconds(10),
) {
    fun run(
        command: List<String>,
        directory: Path,
        input: ByteArray,
        prefix: ByteArray = byteArrayOf(),
    ): AdmissionOutput {
        admissionRequire(
            input.size <= DatabaseAdmission.REQUEST_LIMIT && prefix.size <= DatabaseAdmission.REQUEST_LIMIT,
            AdmissionReason.INPUT_INVALID,
        )
        val executableCommand =
            if (command.firstOrNull() == "python") {
                listOf(pythonExecutable(System.getenv("PATH"))) + command.drop(1)
            } else {
                command
            }
        val builder = ProcessBuilder(executableCommand).directory(directory.toFile())
        builder.environment().keys.retainAll(sortedSetOf(String.CASE_INSENSITIVE_ORDER, "SystemRoot"))
        val process =
            try {
                builder.start()
            } catch (_: IOException) {
                throw AdmissionRefused(AdmissionReason.PROCESS_START)
            }
        val executor =
            Executors.newFixedThreadPool(3) { runnable ->
                Thread(runnable, "migration-admission-io").apply { isDaemon = true }
            }
        val deadline = System.nanoTime() + timeout.toNanos()
        val descendants = linkedMapOf<Long, ProcessHandle>()
        try {
            val stdout = executor.submit<ByteArray> { boundedRead(process.inputStream, DatabaseAdmission.OUTPUT_LIMIT) }
            val stderr = executor.submit<ByteArray> { boundedRead(process.errorStream, 8192) }
            val stdin =
                executor.submit<Unit> {
                    process.outputStream.use {
                        it.write(prefix)
                        it.write(input)
                    }
                }
            val pending = linkedSetOf(stdout, stderr, stdin)
            while (process.isAlive || pending.isNotEmpty()) {
                process.descendants().use { children -> children.forEach { descendants[it.pid()] = it } }
                remaining(deadline)
                pending.removeIf {
                    if (it.isDone) {
                        completed(it)
                        true
                    } else {
                        false
                    }
                }
                Thread.sleep(10)
            }
            completed(stdin)
            val output = completed(stdout)
            val errors = completed(stderr)
            remaining(deadline)
            admissionRequire(errors.isEmpty(), AdmissionReason.OUTPUT_INVALID)
            return AdmissionOutput(process.exitValue(), output)
        } catch (_: InterruptedException) {
            throw interruptedRefusal()
        } finally {
            // Kill only this invocation's descendants and process, never other interpreters on the host.
            process.descendants().use { children -> children.forEach { descendants[it.pid()] = it } }
            descendants.values
                .toList()
                .asReversed()
                .forEach { if (it.isAlive) it.destroyForcibly() }
            if (process.isAlive) process.destroyForcibly()
            executor.shutdownNow()
            val interrupted = Thread.interrupted()
            try {
                val stopped = process.waitFor(2, TimeUnit.SECONDS)
                val cleanupDeadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2)
                // A successful kill request is not an acknowledgement that the descendant has exited.
                CompletableFuture
                    .allOf(*descendants.values.map { it.onExit() }.toTypedArray())
                    .get((cleanupDeadline - System.nanoTime()).coerceAtLeast(0), TimeUnit.NANOSECONDS)
                val finished =
                    executor.awaitTermination(
                        (cleanupDeadline - System.nanoTime()).coerceAtLeast(0),
                        TimeUnit.NANOSECONDS,
                    )
                admissionRequire(stopped && finished && descendants.values.none { it.isAlive }, AdmissionReason.PROCESS_CLEANUP)
            } catch (_: InterruptedException) {
                throw interruptedRefusal()
            } catch (_: ExecutionException) {
                throw AdmissionRefused(AdmissionReason.PROCESS_CLEANUP)
            } catch (_: TimeoutException) {
                throw AdmissionRefused(AdmissionReason.PROCESS_CLEANUP)
            } finally {
                if (interrupted) Thread.currentThread().interrupt()
            }
        }
    }

    private fun interruptedRefusal(): AdmissionRefused {
        Thread.currentThread().interrupt()
        return AdmissionRefused(AdmissionReason.PROCESS_INTERRUPTED)
    }

    private fun boundedRead(
        stream: InputStream,
        limit: Int,
    ): ByteArray =
        stream.use {
            val bytes = it.readNBytes(limit + 1)
            admissionRequire(bytes.size <= limit, AdmissionReason.OUTPUT_SIZE)
            bytes
        }

    // The polling loop checks completion before every call and owns the single process deadline.
    private fun <T> completed(future: Future<T>): T =
        try {
            future.get()
        } catch (error: ExecutionException) {
            throw (error.cause as? AdmissionRefused ?: AdmissionRefused(AdmissionReason.PROCESS_IO))
        }

    private fun remaining(deadline: Long): Long {
        val remaining = deadline - System.nanoTime()
        admissionRequire(remaining > 0, AdmissionReason.PROCESS_TIMEOUT)
        return remaining
    }

    companion object {
        // POSIX CPython needs an absolute argv[0] to retain sys.executable when its PATH is cleared.
        internal fun pythonExecutable(searchPath: String?): String {
            val name = if (File.separatorChar == '\\') "python.exe" else "python"
            try {
                for (directory in searchPath.orEmpty().split(File.pathSeparator)) {
                    val parent = Path.of(directory)
                    if (!parent.isAbsolute) continue
                    val candidate = parent.resolve(name)
                    if (Files.isRegularFile(candidate) && Files.isExecutable(candidate)) return candidate.toString()
                }
            } catch (_: InvalidPathException) {
                throw AdmissionRefused(AdmissionReason.PROCESS_START)
            }
            throw AdmissionRefused(AdmissionReason.PROCESS_START)
        }
    }
}
