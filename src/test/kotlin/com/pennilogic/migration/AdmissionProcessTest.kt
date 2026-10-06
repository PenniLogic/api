package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import java.io.File
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermission
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
                    "forbidden=['GH_TOKEN','GITHUB_TOKEN','MIGRATION_DB_PASSWORD'," +
                    "'PYTHONPATH','PYTHONHOME','PATH','LD_LIBRARY_PATH','LD_PRELOAD']; " +
                    "assert not any(k in os.environ for k in forbidden); " +
                    "sys.stdout.buffer.write(data)",
                "{\"opaque\":\"synthetic\"}".toByteArray(),
            )
        assertEquals(0, result.exitCode)
        assertEquals("{\"opaque\":\"synthetic\"}", result.stdout.toString(Charsets.UTF_8))
    }

    @Test
    fun `isolated Python receives an absolute program name and can launch its own interpreter`() {
        val result =
            run(
                """
                import os, subprocess, sys
                environment = {key: value for key, value in os.environ.items() if key.casefold() == 'systemroot'}
                child = subprocess.run(
                    [sys.executable, '-I', '-S', '-B', '-c',
                     'import os,sys; assert os.path.isabs(sys.orig_argv[0]); '
                     'assert not any(name in os.environ for name in sys.argv[1:]); sys.stdout.buffer.write(sys.stdin.buffer.read())',
                     'PATH', 'LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME',
                     'GH_TOKEN', 'GITHUB_TOKEN', 'MIGRATION_DB_PASSWORD'],
                    input=sys.stdin.buffer.read(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment, timeout=2, check=False)
                sys.stdout.write(str(os.path.isabs(sys.orig_argv[0])) + '/' + str(os.path.isabs(sys.executable)) +
                                 '/' + str(child.returncode) + '/' + str(bool(child.stderr)) + '/')
                sys.stdout.flush()
                sys.stdout.buffer.write(child.stdout)
                """.trimIndent(),
            )
        assertEquals(0, result.exitCode)
        assertEquals("True/True/0/False/{}", result.stdout.toString(Charsets.UTF_8))
    }

    @Test
    fun `managed Python lookup uses the first executable in absolute parent search directories`() {
        val installed = Path.of(AdmissionProcess.pythonExecutable(System.getenv("PATH")))
        assertTrue(installed.isAbsolute)
        val first = Files.createDirectory(directory.resolve("first"))
        val second = Files.createDirectory(directory.resolve("second"))
        val firstFile = Files.createFile(first.resolve(installed.fileName))
        val secondFile = Files.createFile(second.resolve(installed.fileName))
        assertTrue(firstFile.toFile().setExecutable(true))
        assertTrue(secondFile.toFile().setExecutable(true))
        val searchPath =
            listOf("", ".", "relative", directory.resolve("absent").toString(), first.toString(), second.toString())
                .joinToString(File.pathSeparator)
        assertEquals(firstFile.toString(), AdmissionProcess.pythonExecutable(searchPath))
        assertEquals(
            secondFile.toString(),
            AdmissionProcess.pythonExecutable(listOf(second, first).joinToString(File.pathSeparator)),
        )
        if (Files.getFileStore(directory).supportsFileAttributeView("posix")) {
            Files.setPosixFilePermissions(firstFile, setOf(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE))
            assertEquals(secondFile.toString(), AdmissionProcess.pythonExecutable(searchPath))
        }
    }

    @Test
    fun `missing relative malformed and directory-only Python candidates refuse without path disclosure`() {
        val installed = Path.of(AdmissionProcess.pythonExecutable(System.getenv("PATH")))
        Files.createDirectory(directory.resolve(installed.fileName))
        for (searchPath in listOf(
            null,
            "",
            ".",
            "relative",
            directory.resolve("missing-sensitive-marker").toString(),
            directory.toString(),
            "sensitive-marker\u0000",
        )) {
            val error = assertThrows(AdmissionRefused::class.java) { AdmissionProcess.pythonExecutable(searchPath) }
            assertEquals("PROCESS_START", error.code)
            assertFalse(error.toString().contains("sensitive-marker"))
        }
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
        val started = System.nanoTime()
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
        assertTrue(TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - started) < 6)
    }

    @Test
    fun `cleanup also awaits observed children without inherited input or output pipes`() {
        val unrelated = ProcessBuilder("python", "-I", "-S", "-B", "-c", "import time; time.sleep(60)").start()
        val started = System.nanoTime()
        try {
            val error =
                assertThrows(AdmissionRefused::class.java) {
                    run(
                        "import subprocess,sys,time,pathlib; " +
                            "child=subprocess.Popen([sys.executable,'-I','-S','-B','-c','import time; time.sleep(60)'], " +
                            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); " +
                            "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(60)",
                        timeout = Duration.ofSeconds(1),
                    )
                }
            assertEquals("PROCESS_TIMEOUT", error.code)
            val child = Files.readString(directory.resolve("child.pid")).trim().toLong()
            assertFalse(ProcessHandle.of(child).map { it.isAlive }.orElse(false))
            assertTrue(unrelated.isAlive)
            assertTrue(TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - started) < 6)
        } finally {
            unrelated.destroyForcibly()
            assertTrue(unrelated.waitFor(5, TimeUnit.SECONDS))
        }
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
