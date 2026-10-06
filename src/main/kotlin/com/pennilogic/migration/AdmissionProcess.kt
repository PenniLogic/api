package com.pennilogic.migration

import java.io.IOException
import java.io.InputStream
import java.nio.file.Path
import java.time.Duration
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
        val builder = ProcessBuilder(command).directory(directory.toFile())
        val systemRoot = builder.environment()["SystemRoot"]
        builder.environment().clear()
        if (systemRoot != null) builder.environment()["SystemRoot"] = systemRoot
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
            val streams = listOf(stdout, stderr, stdin)
            while (process.isAlive || streams.any { !it.isDone }) {
                process.descendants().use { children -> children.forEach { descendants[it.pid()] = it } }
                remaining(deadline)
                streams.filter { it.isDone }.forEach { await(it, deadline) }
                Thread.sleep(10)
            }
            await(stdin, deadline)
            val output = await(stdout, deadline)
            val errors = await(stderr, deadline)
            admissionRequire(process.waitFor(remaining(deadline), TimeUnit.NANOSECONDS), AdmissionReason.PROCESS_TIMEOUT)
            admissionRequire(errors.isEmpty(), AdmissionReason.OUTPUT_INVALID)
            return AdmissionOutput(process.exitValue(), output)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            throw AdmissionRefused(AdmissionReason.PROCESS_INTERRUPTED)
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
                val finished = executor.awaitTermination(2, TimeUnit.SECONDS)
                admissionRequire(stopped && finished && descendants.values.none { it.isAlive }, AdmissionReason.PROCESS_CLEANUP)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                throw AdmissionRefused(AdmissionReason.PROCESS_INTERRUPTED)
            } finally {
                if (interrupted) Thread.currentThread().interrupt()
            }
        }
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

    private fun <T> await(
        future: Future<T>,
        deadline: Long,
    ): T =
        try {
            future.get(remaining(deadline), TimeUnit.NANOSECONDS)
        } catch (_: TimeoutException) {
            throw AdmissionRefused(AdmissionReason.PROCESS_TIMEOUT)
        } catch (error: ExecutionException) {
            throw (error.cause as? AdmissionRefused ?: AdmissionRefused(AdmissionReason.PROCESS_IO))
        }

    private fun remaining(deadline: Long): Long {
        val remaining = deadline - System.nanoTime()
        admissionRequire(remaining > 0, AdmissionReason.PROCESS_TIMEOUT)
        return remaining
    }
}
