package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.nio.file.Files
import java.nio.file.Path
import java.time.Duration
import java.util.concurrent.TimeUnit

class AdmissionProcessTest {
    @TempDir
    lateinit var directory: Path

    private fun run(
        code: String,
        input: ByteArray = "{}".toByteArray(),
        timeout: Duration = Duration.ofSeconds(5),
    ): AdmissionOutput = AdmissionProcess(timeout).run(listOf("python", "-I", "-S", "-B", "-c", code), directory, input)

    @Test
    fun `real process receives bounded input and an environment without credentials or search-path injection`() {
        val result =
            run(
                "import os,sys; data=sys.stdin.buffer.read(); " +
                    "forbidden=['GH_TOKEN','GITHUB_TOKEN','MIGRATION_DB_PASSWORD','PYTHONPATH','PYTHONHOME','PATH']; " +
                    "assert not any(k in os.environ for k in forbidden); " +
                    "sys.stdout.buffer.write(data)",
                "{\"opaque\":\"synthetic\"}".toByteArray(),
            )
        assertEquals(0, result.exitCode)
        assertEquals("{\"opaque\":\"synthetic\"}", result.stdout.toString(Charsets.UTF_8))
    }

    @Test
    fun `nonzero exits are returned for strict protocol agreement checking`() {
        val result = run("import sys; sys.stdin.buffer.read(); sys.stdout.write('{}'); sys.exit(7)")
        assertEquals(7, result.exitCode)
        assertEquals("{}", result.stdout.toString(Charsets.UTF_8))
    }

    @Test
    fun `stderr output and oversized streams never enter diagnostics`() {
        for (code in listOf(
            "import sys; sys.stderr.write('sensitive-marker'); sys.stdout.write('{}')",
            "import sys; sys.stdout.write('x'*65537)",
            "import sys; sys.stderr.write('x'*8193)",
        )) {
            val error = assertThrows(AdmissionRefused::class.java) { run(code) }
            assertTrue(error.code in setOf("OUTPUT_INVALID", "OUTPUT_SIZE"))
            assertFalse(error.toString().contains("sensitive-marker"))
        }
    }

    @Test
    fun `a provider that never consumes input is bounded and terminated`() {
        val start = System.nanoTime()
        val error =
            assertThrows(AdmissionRefused::class.java) {
                run("import time; time.sleep(60)", ByteArray(DatabaseAdmission.REQUEST_LIMIT), Duration.ofMillis(250))
            }
        assertEquals("PROCESS_TIMEOUT", error.code)
        assertTrue(TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - start) < 6)
    }

    @Test
    fun `timeout also terminates observed invocation children without killing an unrelated process`() {
        val unrelated = ProcessBuilder("python", "-I", "-S", "-B", "-c", "import time; time.sleep(60)").start()
        try {
            val error =
                assertThrows(AdmissionRefused::class.java) {
                    run(
                        "import subprocess,sys,time,pathlib; " +
                            "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time; time.sleep(60)']); " +
                            "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(60)",
                        timeout = Duration.ofSeconds(1),
                    )
                }
            assertEquals("PROCESS_TIMEOUT", error.code)
            val child = Files.readString(directory.resolve("child.pid")).trim().toLong()
            assertFalse(ProcessHandle.of(child).map { it.isAlive }.orElse(false))
            assertTrue(unrelated.isAlive)
        } finally {
            unrelated.destroyForcibly()
            assertTrue(unrelated.waitFor(5, TimeUnit.SECONDS))
        }
    }

    @Test
    fun `provider exit does not leave an inherited output pipe or its observed child alive`() {
        assertEquals(
            "PROCESS_TIMEOUT",
            assertThrows(AdmissionRefused::class.java) {
                run(
                    "import subprocess,sys,time,pathlib; " +
                        "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time; time.sleep(60)']); " +
                        "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(0.25)",
                    timeout = Duration.ofSeconds(1),
                )
            }.code,
        )
        val child = Files.readString(directory.resolve("child.pid")).trim().toLong()
        assertFalse(ProcessHandle.of(child).map { it.isAlive }.orElse(false))
    }

    @Test
    fun `missing executable and over-limit requests fail with static codes`() {
        val error =
            assertThrows(AdmissionRefused::class.java) {
                AdmissionProcess().run(
                    listOf(directory.resolve("missing-executable-sensitive-marker").toString()),
                    directory,
                    byteArrayOf(),
                )
            }
        assertEquals("PROCESS_START", error.code)
        assertFalse(error.toString().contains("sensitive-marker"))
        assertEquals(
            "INPUT_INVALID",
            assertThrows(AdmissionRefused::class.java) {
                run("raise AssertionError('must not run')", ByteArray(DatabaseAdmission.REQUEST_LIMIT + 1))
            }.code,
        )
        assertEquals(
            "INPUT_INVALID",
            assertThrows(AdmissionRefused::class.java) {
                AdmissionProcess().run(
                    listOf(directory.resolve("must-not-execute").toString()),
                    directory,
                    byteArrayOf(),
                    ByteArray(DatabaseAdmission.REQUEST_LIMIT + 1),
                )
            }.code,
        )
    }

    @Test
    fun `closed provider input is an explicit IO refusal rather than partial request success`() {
        assertEquals(
            "PROCESS_IO",
            assertThrows(AdmissionRefused::class.java) {
                run("import os,time; os.close(0); time.sleep(0.1)", ByteArray(DatabaseAdmission.REQUEST_LIMIT))
            }.code,
        )
    }

    @Test
    fun `interruption is preserved after process cleanup`() {
        Thread.currentThread().interrupt()
        try {
            assertEquals(
                "PROCESS_INTERRUPTED",
                assertThrows(AdmissionRefused::class.java) { run("import time; time.sleep(60)") }.code,
            )
            assertTrue(Thread.currentThread().isInterrupted)
        } finally {
            Thread.interrupted()
        }
    }
}
