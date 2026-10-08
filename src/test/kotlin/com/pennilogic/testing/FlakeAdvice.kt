package com.pennilogic.testing

import com.pennilogic.migration.AdmissionJson
import com.pennilogic.migration.AdmissionRefused
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import org.opentest4j.TestAbortedException
import java.security.MessageDigest
import java.time.Clock
import java.time.DateTimeException
import java.time.Duration
import java.time.Instant
import java.util.Collections
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

internal class FlakePolicy private constructor(
    val retryLimit: Int,
    val restrictedRetryLimit: Int,
    val quarantineAfterFlakes: Int,
    val windowDays: Int,
    val quarantineCeilingPercent: Int,
    val quarantineMaxAgeDays: Int,
    val changeClasses: Set<String>,
    val digest: String,
) {
    fun retries(
        classes: Set<String>,
        requested: Int?,
    ): Int {
        require(classes.isNotEmpty() && changeClasses.containsAll(classes)) { "flake-change-classes" }
        val limit = if ("money_path" in classes) restrictedRetryLimit else retryLimit
        val retries = requested ?: limit
        require(retries in 0..limit) { "flake-retry-configuration" }
        return retries
    }

    companion object {
        const val RESOURCE = "/testing/policy/test-strategy.json"
        const val MAX_BYTES = 131072

        fun accepted(): FlakePolicy {
            val bytes =
                requireNotNull(FlakePolicy::class.java.getResourceAsStream(RESOURCE)) { "flake-policy-missing" }
                    .use { it.readNBytes(MAX_BYTES + 1) }
            return parse(bytes)
        }

        fun parse(bytes: ByteArray): FlakePolicy {
            val root =
                try {
                    obj(AdmissionJson.parse(bytes, MAX_BYTES))
                } catch (_: AdmissionRefused) {
                    throw IllegalArgumentException("flake-policy-json")
                }
            require(number(root["schema_version"]) == 1) { "flake-policy-version" }
            val policy = obj(root["flake_policy"])
            val numbers =
                setOf(
                    "retry_limit",
                    "money_path_retry_limit",
                    "quarantine_after_flakes",
                    "flake_window_days",
                    "quarantine_rate_ceiling_percent",
                    "quarantine_max_age_days",
                )
            val semantics =
                setOf("retry_semantics", "on_quarantine_ceiling_exceeded", "on_retry_limit_exceeded", "on_quarantine_expired")
            require(policy.keys == numbers + semantics) { "flake-policy-fields" }
            for (key in semantics) {
                val value = policy[key]
                require(value is JsonPrimitive && value.isString && value.content.length in 1..1024) { "flake-policy-semantics" }
            }
            val retry = number(policy["retry_limit"])
            val restrictedRetry = number(policy["money_path_retry_limit"])
            val threshold = number(policy["quarantine_after_flakes"])
            val days = number(policy["flake_window_days"])
            val ceiling = number(policy["quarantine_rate_ceiling_percent"])
            val age = number(policy["quarantine_max_age_days"])
            require(retry in 0..1 && restrictedRetry == 0) { "flake-policy-retry-support" }
            require(threshold in 1..FlakeHistory.MAX_OBSERVATIONS && days > 0 && age > 0 && ceiling in 0..100) {
                "flake-policy-range"
            }
            val classes = root["change_classes"]
            require(classes is JsonArray && classes.size in 1..64) { "flake-policy-change-classes" }
            val names =
                classes.map {
                    val id = obj(it)["id"]
                    require(id is JsonPrimitive && id.isString && Regex("[a-z][a-z0-9_]{0,63}").matches(id.content)) {
                        "flake-policy-change-classes"
                    }
                    id.content
                }
            require(names.toSet().size == names.size && "money_path" in names) { "flake-policy-change-classes" }
            return FlakePolicy(
                retry,
                restrictedRetry,
                threshold,
                days,
                ceiling,
                age,
                Collections.unmodifiableSet(names.toSet()),
                flakeDigest(bytes),
            )
        }

        private fun obj(value: JsonElement?): JsonObject {
            require(value is JsonObject) { "flake-policy-fields" }
            return value
        }

        private fun number(value: JsonElement?): Int {
            require(value is JsonPrimitive && !value.isString && Regex("0|[1-9][0-9]*").matches(value.content)) {
                "flake-policy-number"
            }
            return requireNotNull(value.content.toIntOrNull()) { "flake-policy-number" }
        }
    }
}

internal fun flakeDigest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).toHexString()

internal enum class TestCategory(
    val id: String,
) {
    UNIT("unit"),
    PROPERTY("property"),
    CONTRACT("contract"),
    INTEGRATION("integration"),
}

internal data class FlakeIdentity(
    val test: String,
    val seed: Long,
    val category: TestCategory,
    val task: String,
    val source: String,
    val configuration: String,
) {
    init {
        require(Regex("[a-z][a-z0-9-]{0,63}").matches(test)) { "flake-test-identifier" }
        require(task in setOf("test", "integrationTest", "moneyTest")) { "flake-task" }
        require(listOf(source, configuration).all { Regex("[0-9a-f]{64}").matches(it) }) { "flake-execution-binding" }
    }
}

internal enum class AttemptOutcome(
    val id: String,
) {
    PASSED("passed"),
    FAILED("failed"),
    INCOMPLETE("incomplete"),
}

internal data class FlakeAttempt(
    val before: FlakeIdentity,
    val after: FlakeIdentity,
    val outcome: AttemptOutcome,
    val elapsedMs: Long,
) {
    init {
        require(elapsedMs >= 0) { "flake-attempt-duration" }
    }
}

internal enum class FlakeClassification(
    val id: String,
) {
    STABLE_SUCCESS("stable_success"),
    STABLE_FAILURE("stable_failure"),
    NON_DETERMINISTIC("non_deterministic"),
    INCOMPLETE("incomplete"),
    NOT_COMPARABLE("not_comparable"),
}

internal class FlakeObservation(
    val execution: Int,
    val at: Instant,
    val identity: FlakeIdentity,
    attempts: List<FlakeAttempt>,
    val classification: FlakeClassification,
    val flakesInWindow: Int,
    val quarantineAdvisory: Boolean,
    private val policy: FlakePolicy,
) {
    val attempts: List<FlakeAttempt> = Collections.unmodifiableList(attempts.toList())
    val blocked: Boolean get() = classification != FlakeClassification.STABLE_SUCCESS

    fun json(): String =
        buildJsonObject {
            put("event", "flaky_test_observation")
            put("test", identity.test)
            put("seed", identity.seed)
            put("category", identity.category.id)
            put("task", identity.task)
            put("source_id", identity.source)
            put("configuration_id", identity.configuration)
            put("policy_sha256", policy.digest)
            put("execution", execution)
            put("observed_at", at.toString())
            put("classification", classification.id)
            put("status", if (blocked) "blocked" else "passed")
            putJsonArray("attempts") {
                attempts.forEachIndexed { index, attempt ->
                    add(
                        buildJsonObject {
                            put("attempt", index + 1)
                            put("outcome", attempt.outcome.id)
                            put("elapsed_ms", attempt.elapsedMs)
                            put("comparable", attempt.before == identity && attempt.after == identity)
                        },
                    )
                }
            }
            put("retries", attempts.size - 1)
            put("flakes_in_window", flakesInWindow)
            put("flake_window_days", policy.windowDays)
            put("quarantine_after_flakes", policy.quarantineAfterFlakes)
            put("quarantine_advisory", quarantineAdvisory)
            put("quarantined", false)
            put("skipped", false)
            put("history_scope", "runner_instance")
            put("quarantine_inventory_gates", "not_implemented")
            put("quarantine_rate_ceiling_percent", policy.quarantineCeilingPercent)
            put("quarantine_max_age_days", policy.quarantineMaxAgeDays)
        }.toString()
}

internal class FlakeHistory(
    private val policy: FlakePolicy,
    val identity: FlakeIdentity,
    classes: Set<String>,
    requestedRetries: Int? = null,
) {
    val retries: Int = policy.retries(classes, requestedRetries)
    private val observations = mutableListOf<FlakeObservation>()

    @Synchronized
    fun nextExecution(
        current: FlakeIdentity,
        at: Instant,
    ): Int {
        require(current == identity) { "flake-history-binding" }
        require(observations.size < MAX_OBSERVATIONS) { "flake-history-capacity" }
        require(observations.lastOrNull()?.at?.let { at >= it } != false) { "flake-history-time" }
        return observations.size + 1
    }

    @Synchronized
    fun record(
        execution: Int,
        at: Instant,
        attempts: List<FlakeAttempt>,
    ): FlakeObservation {
        val recorded = attempts.toList()
        require(execution == nextExecution(identity, at)) { "flake-history-sequence" }
        require(recorded.size in 1..retries + 1) { "flake-attempt-count" }
        require(recorded.dropLast(1).all { it.outcome == AttemptOutcome.FAILED && it.before == identity && it.after == identity }) {
            "flake-reexecution-without-failure"
        }
        val classification =
            when {
                recorded.any { it.before != identity || it.after != identity } -> FlakeClassification.NOT_COMPARABLE
                recorded.last().outcome == AttemptOutcome.INCOMPLETE -> FlakeClassification.INCOMPLETE
                recorded.first().outcome == AttemptOutcome.PASSED -> FlakeClassification.STABLE_SUCCESS
                recorded.size != retries + 1 -> FlakeClassification.INCOMPLETE
                recorded.last().outcome == AttemptOutcome.PASSED -> FlakeClassification.NON_DETERMINISTIC
                else -> FlakeClassification.STABLE_FAILURE
            }
        val lower =
            try {
                at.minus(Duration.ofDays(policy.windowDays.toLong()))
            } catch (_: DateTimeException) {
                throw IllegalArgumentException("flake-history-time")
            }
        val flakes =
            observations.count { it.at >= lower && it.classification == FlakeClassification.NON_DETERMINISTIC } +
                if (classification == FlakeClassification.NON_DETERMINISTIC) 1 else 0
        val comparable = classification != FlakeClassification.NOT_COMPARABLE && classification != FlakeClassification.INCOMPLETE
        val result =
            FlakeObservation(
                execution,
                at,
                identity,
                recorded,
                classification,
                flakes,
                comparable && flakes >= policy.quarantineAfterFlakes,
                policy,
            )
        observations.add(result)
        return result
    }

    companion object {
        const val MAX_OBSERVATIONS = 256
    }
}

internal class AdvisoryCheck(
    policy: FlakePolicy,
    private val currentIdentity: () -> FlakeIdentity,
    classes: Set<String>,
    requestedRetries: Int? = null,
    private val clock: Clock = Clock.systemUTC(),
    private val onObservation: (FlakeObservation) -> Unit = {},
    private val action: () -> Unit,
) {
    private val history = FlakeHistory(policy, currentIdentity(), classes, requestedRetries)
    private val active = AtomicBoolean(false)

    fun run() {
        check(active.compareAndSet(false, true)) { "flake-concurrent-execution" }
        try {
            val began = clock.instant()
            val execution = history.nextExecution(currentIdentity(), began)
            val attempts = mutableListOf<FlakeAttempt>()
            var firstFailure: AssertionError? = null
            var result: FlakeObservation
            try {
                while (attempts.size <= history.retries) {
                    val before = currentIdentity()
                    require(before == history.identity) { "flake-history-binding" }
                    var outcome = AttemptOutcome.INCOMPLETE
                    val started = System.nanoTime()
                    try {
                        action()
                        outcome = AttemptOutcome.PASSED
                    } catch (_: TestAbortedException) {
                        throw IllegalStateException("flake-execution-aborted")
                    } catch (error: AssertionError) {
                        outcome = AttemptOutcome.FAILED
                        if (firstFailure == null) {
                            firstFailure = error
                        } else if (firstFailure !== error) {
                            firstFailure.addSuppressed(error)
                        }
                    } finally {
                        attempts.add(
                            FlakeAttempt(
                                before,
                                currentIdentity(),
                                outcome,
                                TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started),
                            ),
                        )
                    }
                    if (outcome != AttemptOutcome.FAILED || attempts.last().after != history.identity) break
                }
            } finally {
                val ended = clock.instant()
                require(ended >= began) { "flake-history-time" }
                result = history.record(execution, ended, attempts)
                println(result.json())
                onObservation(result)
            }
            firstFailure?.let { throw it }
            check(!result.blocked) { "flake-execution-incomplete" }
        } finally {
            active.set(false)
        }
    }
}
