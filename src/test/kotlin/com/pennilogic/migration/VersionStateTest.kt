package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

class VersionStateTest {
    private val applied =
        RegistryRow(
            version = 1,
            id = "V001__status_fixture",
            state = RowState.APPLIED,
            direction = "up",
            checksum = Checksum.of("SELECT 1;\n"),
            reversalKind = ReversalKind.DOWN,
            reversalFile = "V001__status_fixture.down.sql",
            reversalReason = null,
            reversalChecksum = Checksum.of("SELECT 2;\n"),
            appliedBy = "status@junit",
            host = "fixture",
            attemptedAt = "2026-10-06T00:00:00Z",
            durationMs = 1,
            failure = null,
        )
    private val reversed = applied.copy(state = RowState.REVERSED, direction = "down")
    private val failed =
        applied.copy(
            state = RowState.FAILED,
            failure = FailureSummary("P0001", null, null, "status_fixture_refused", null, null),
        )

    @Test
    fun `a failed down attempt labels an applied version failed without undoing its transition`() {
        val state = VersionState(1, applied, failed.copy(direction = "down"))
        assertEquals(applied, state.appliedRow)
        assertEquals(applied, state.transition)
        assertEquals("failed", state.label)
    }

    @Test
    fun `a failed first up attempt has no applied transition`() {
        val state = VersionState(1, null, failed)
        assertNull(state.appliedRow)
        assertEquals("failed", state.label)
    }

    @Test
    fun `a failed up attempt after reversal keeps that version unapplied`() {
        val state = VersionState(1, reversed, failed)
        assertNull(state.appliedRow)
        assertEquals(reversed, state.transition)
        assertEquals("failed", state.label)
    }

    @Test
    fun `a successful up attempt reports the applied transition`() {
        val state = VersionState(1, applied, applied)
        assertEquals(applied, state.appliedRow)
        assertEquals("applied", state.label)
    }

    @Test
    fun `a successful down attempt reports reversal with no applied row`() {
        val state = VersionState(1, reversed, reversed)
        assertNull(state.appliedRow)
        assertEquals("reversed", state.label)
    }
}
