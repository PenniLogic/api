package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.Optional
import java.util.concurrent.AbstractExecutorService
import java.util.concurrent.CompletableFuture
import java.util.concurrent.CountDownLatch
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import java.util.stream.Stream

/** Synthetic process acknowledgements exercise failure paths; AdmissionProcessTest retains the real OS controls. */
class AdmissionCleanupTest {
    @Test
    fun `cleanup includes the final descendant snapshot and kills in reverse observation order`() {
        val events = mutableListOf<String>()
        val exited = SyntheticChild(1, events).apply { acknowledgeExit() }
        val observed = SyntheticChild(2, events)
        val latest = SyntheticChild(3, events)
        val process =
            SyntheticProcess(events).apply {
                alive = true
                latestChildren = listOf(observed, latest)
            }
        val executor = SyntheticExecutor(events)

        cleanup(process, executor, exited, observed)

        assertEquals(
            listOf("kill:3", "kill:2", "kill:provider", "shutdown", "wait:provider", "exit:1", "exit:2", "exit:3", "wait:io"),
            events,
        )
        assertFalse(process.isAlive)
        assertTrue(listOf(exited, observed, latest).none { it.isAlive })
        assertEquals(TimeUnit.SECONDS.toNanos(2), process.waitNanos)
        assertTrue(executor.waitNanos.single() in 0..TimeUnit.SECONDS.toNanos(2))
    }

    @Test
    fun `every incomplete final acknowledgement refuses instead of reporting cleanup success`() {
        for (incomplete in listOf("provider", "io", "descendant")) {
            val process = SyntheticProcess()
            val executor = SyntheticExecutor()
            val child = SyntheticChild(1)
            when (incomplete) {
                "provider" -> {
                    process.alive = true
                    process.stopOnDestroy = false
                }

                "io" -> {
                    executor.waitForTermination = { false }
                }

                "descendant" -> {
                    child.exited.complete(child)
                    child.acknowledgeKill = false
                }
            }

            val error = assertThrows(AdmissionRefused::class.java) { cleanup(process, executor, child) }

            assertEquals("PROCESS_CLEANUP", error.code, incomplete)
            assertNull(error.cause)
            assertTrue(executor.isShutdown)
            assertEquals(1, executor.waitNanos.size)
        }
    }

    @Test
    fun `failed descendant exit observation has a static refusal without the original exception`() {
        val child =
            SyntheticChild(1).apply {
                acknowledgeKill = false
                exited.completeExceptionally(IOException("sensitive-child-observation"))
            }
        val executor = SyntheticExecutor()

        val error = assertThrows(AdmissionRefused::class.java) { cleanup(SyntheticProcess(), executor, child) }

        assertEquals("PROCESS_CLEANUP", error.code)
        assertFalse(error.toString().contains("sensitive-child-observation"))
        assertNull(error.cause)
        assertTrue(executor.isShutdown)
        assertTrue(executor.waitNanos.isEmpty())
    }

    @Test
    fun `missing descendant exit acknowledgement exhausts the existing budget and still refuses`() {
        val child = SyntheticChild(1).apply { acknowledgeKill = false }
        val executor = SyntheticExecutor()
        val started = System.nanoTime()

        val error = assertThrows(AdmissionRefused::class.java) { cleanup(SyntheticProcess(), executor, child) }

        val elapsed = System.nanoTime() - started
        assertEquals("PROCESS_CLEANUP", error.code)
        assertTrue(elapsed in TimeUnit.SECONDS.toNanos(2)..TimeUnit.SECONDS.toNanos(6))
        assertTrue(child.isAlive)
        assertFalse(child.exited.isDone)
        assertTrue(executor.isShutdown)
        assertTrue(executor.waitNanos.isEmpty())
    }

    @Test
    fun `descendant exit and IO termination consume one shared remaining deadline`() {
        val requested = CountDownLatch(1)
        val observedAt = AtomicLong()
        val child =
            SyntheticChild(1).apply {
                acknowledgeKill = false
                exitRequested = {
                    observedAt.set(System.nanoTime())
                    requested.countDown()
                }
            }
        val process = SyntheticProcess()
        val executor = SyntheticExecutor()
        val result = FutureTask { cleanup(process, executor, child) }
        val worker = Thread(result, "admission-cleanup-budget-test")
        worker.start()
        try {
            assertTrue(requested.await(5, TimeUnit.SECONDS))
            val acknowledgementDelay = System.nanoTime() - observedAt.get()
            child.acknowledgeExit()
            result.get(5, TimeUnit.SECONDS)

            assertTrue(acknowledgementDelay > 0)
            assertTrue(executor.waitNanos.single() <= TimeUnit.SECONDS.toNanos(2) - acknowledgementDelay)
            assertEquals(TimeUnit.SECONDS.toNanos(2), process.waitNanos)
            assertFalse(child.isAlive)
        } finally {
            worker.interrupt()
            worker.join(5000)
            assertFalse(worker.isAlive)
        }
    }

    @Test
    fun `interruption during each cleanup wait refuses and preserves the worker interrupt`() {
        for (stage in listOf("provider", "descendant", "io")) {
            val waiting = CountDownLatch(1)
            val neverReleased = CountDownLatch(1)
            val process = SyntheticProcess()
            val child = SyntheticChild(1)
            val executor = SyntheticExecutor()
            val interruptibleWait = {
                waiting.countDown()
                neverReleased.await()
                true
            }
            when (stage) {
                "provider" -> {
                    process.waitForExit = interruptibleWait
                }

                "descendant" -> {
                    child.acknowledgeKill = false
                    child.exitRequested = { waiting.countDown() }
                }

                "io" -> {
                    executor.waitForTermination = interruptibleWait
                }
            }
            val result =
                FutureTask {
                    val error = assertThrows(AdmissionRefused::class.java) { cleanup(process, executor, child) }
                    assertEquals("PROCESS_INTERRUPTED", error.code, stage)
                    assertNull(error.cause)
                    Thread.currentThread().isInterrupted
                }
            val worker = Thread(result, "admission-cleanup-interrupt-test")
            worker.start()
            try {
                assertTrue(waiting.await(5, TimeUnit.SECONDS), stage)
                worker.interrupt()
                assertTrue(result.get(5, TimeUnit.SECONDS), stage)
                assertTrue(executor.isShutdown)
            } finally {
                worker.interrupt()
                worker.join(5000)
                assertFalse(worker.isAlive, stage)
            }
        }
    }

    @Test
    fun `an existing interrupt is restored even when an exit observation fails`() {
        val child =
            SyntheticChild(1).apply {
                acknowledgeKill = false
                exited.completeExceptionally(IOException("sensitive-observation"))
            }
        Thread.currentThread().interrupt()
        try {
            val error = assertThrows(AdmissionRefused::class.java) { cleanup(SyntheticProcess(), SyntheticExecutor(), child) }
            assertEquals("PROCESS_CLEANUP", error.code)
            assertTrue(Thread.currentThread().isInterrupted)
            assertNull(error.cause)
        } finally {
            Thread.interrupted()
        }
    }

    private fun cleanup(
        process: SyntheticProcess,
        executor: SyntheticExecutor,
        vararg observed: SyntheticChild,
    ) {
        val descendants = observed.associateByTo(linkedMapOf<Long, ProcessHandle>()) { it.pid() }
        AdmissionProcess().cleanup(process, descendants, executor)
    }

    private class SyntheticProcess(
        private val events: MutableList<String> = mutableListOf(),
    ) : Process() {
        var alive = false
        var stopOnDestroy = true
        var latestChildren = emptyList<ProcessHandle>()
        var waitForExit: () -> Boolean = { !alive }
        var waitNanos: Long? = null

        override fun descendants(): Stream<ProcessHandle> = latestChildren.stream()

        override fun isAlive(): Boolean = alive

        override fun destroyForcibly(): Process {
            events += "kill:provider"
            if (stopOnDestroy) alive = false
            return this
        }

        override fun waitFor(
            timeout: Long,
            unit: TimeUnit,
        ): Boolean {
            events += "wait:provider"
            waitNanos = unit.toNanos(timeout)
            return waitForExit()
        }

        override fun waitFor(): Int = throw AssertionError("Unbounded waits are not allowed")

        override fun getOutputStream(): OutputStream = throw AssertionError("No OS process or streams exist")

        override fun getInputStream(): InputStream = throw AssertionError("No OS process or streams exist")

        override fun getErrorStream(): InputStream = throw AssertionError("No OS process or streams exist")

        override fun exitValue(): Int = throw AssertionError("Use the bounded exit acknowledgement")

        override fun destroy() = throw AssertionError("Cleanup must request forcible termination")
    }

    private class SyntheticChild(
        private val id: Long,
        private val events: MutableList<String> = mutableListOf(),
    ) : ProcessHandle {
        @Volatile
        private var alive = true
        var acknowledgeKill = true
        var exitRequested: () -> Unit = {}
        val exited = CompletableFuture<ProcessHandle>()

        fun acknowledgeExit() {
            alive = false
            exited.complete(this)
        }

        override fun pid(): Long = id

        override fun isAlive(): Boolean = alive

        override fun onExit(): CompletableFuture<ProcessHandle> {
            events += "exit:$id"
            exitRequested()
            return exited
        }

        override fun destroyForcibly(): Boolean {
            events += "kill:$id"
            if (acknowledgeKill) acknowledgeExit()
            return true
        }

        override fun compareTo(other: ProcessHandle): Int = id.compareTo(other.pid())

        override fun parent(): Optional<ProcessHandle> = Optional.empty()

        override fun children(): Stream<ProcessHandle> = Stream.empty()

        override fun descendants(): Stream<ProcessHandle> = Stream.empty()

        override fun supportsNormalTermination(): Boolean = false

        override fun info(): ProcessHandle.Info = throw AssertionError("No OS process exists")

        override fun destroy(): Boolean = throw AssertionError("Cleanup must request forcible termination")
    }

    private class SyntheticExecutor(
        private val events: MutableList<String> = mutableListOf(),
    ) : AbstractExecutorService() {
        private var stopped = false
        var waitForTermination: () -> Boolean = { true }
        val waitNanos = mutableListOf<Long>()

        override fun shutdownNow(): MutableList<Runnable> {
            events += "shutdown"
            stopped = true
            return mutableListOf()
        }

        override fun awaitTermination(
            timeout: Long,
            unit: TimeUnit,
        ): Boolean {
            events += "wait:io"
            waitNanos += unit.toNanos(timeout)
            return waitForTermination()
        }

        override fun isShutdown(): Boolean = stopped

        override fun isTerminated(): Boolean = stopped

        override fun shutdown() = throw AssertionError("Cleanup must interrupt pending IO")

        override fun execute(command: Runnable) = throw AssertionError("No IO workers exist")
    }
}
