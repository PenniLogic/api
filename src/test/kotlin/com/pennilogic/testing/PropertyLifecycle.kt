package com.pennilogic.testing

import io.kotest.property.Arb
import org.junit.jupiter.api.extension.AfterAllCallback
import org.junit.jupiter.api.extension.ExtensionContext
import org.junit.jupiter.api.extension.ParameterContext
import org.junit.jupiter.api.extension.ParameterResolutionException
import org.junit.jupiter.api.extension.ParameterResolver
import java.time.Clock
import java.util.UUID

class PropertyChecks internal constructor(
    private val clock: Clock = Clock.systemUTC(),
) : AutoCloseable {
    private data class Registered(
        val history: FlakeHistory,
        val generator: Class<*>,
        val predicate: Class<*>,
    )

    private val context = UUID.randomUUID()
    private val histories = linkedMapOf<String, Registered>()
    private var environment: FlakeHistory? = null
    private var active = 0
    private var closed = false

    internal val retainedHistories: Int
        @Synchronized get() = histories.size

    internal val isClosed: Boolean
        @Synchronized get() = closed

    internal fun <T> checkProperty(
        id: String,
        cases: Int,
        generator: () -> Arb<FixtureCase<T>>,
        changeClasses: Set<String>,
        requestedRetries: Int? = null,
        onResult: (PropertyExecution) -> Unit = {},
        onObservation: (FlakeObservation) -> Unit = {},
        check: (FixtureCase<T>) -> Unit,
    ) {
        val runner = propertyCheck(id, cases, generator, changeClasses, requestedRetries, clock, onResult, onObservation, check)
        val history = acquire(runner.history, generator.javaClass, check.javaClass)
        try {
            runner.run(history)
        } finally {
            release()
        }
    }

    @Synchronized
    private fun acquire(
        definition: FlakeHistory,
        generator: Class<*>,
        predicate: Class<*>,
    ): FlakeHistory {
        check(!closed) { "property-history-closed" }
        require(environment?.sameEnvironment(definition) != false) { "property-history-environment" }
        val id = definition.identity.test
        val existing = histories[id]
        val registered =
            if (existing == null) {
                require(histories.size < MAX_PROPERTIES) { "property-history-capacity" }
                Registered(definition.forTestClass(context), generator, predicate).also { histories[id] = it }
            } else {
                require(existing.history.accepts(definition) && existing.generator == generator && existing.predicate == predicate) {
                    "property-history-binding"
                }
                existing
            }
        if (environment == null) environment = registered.history
        active++
        return registered.history
    }

    @Synchronized
    private fun release() {
        check(active > 0) { "property-history-not-active" }
        active--
    }

    @Synchronized
    override fun close() {
        if (closed) return
        check(active == 0) { "property-history-active-close" }
        histories.values.forEach { it.history.close() }
        histories.clear()
        environment = null
        closed = true
    }

    internal companion object {
        const val MAX_PROPERTIES = 64
    }
}

internal class PropertyChecksExtension :
    ParameterResolver,
    AfterAllCallback {
    override fun supportsParameter(
        parameterContext: ParameterContext,
        extensionContext: ExtensionContext,
    ): Boolean = parameterContext.parameter.type == PropertyChecks::class.java

    override fun resolveParameter(
        parameterContext: ParameterContext,
        extensionContext: ExtensionContext,
    ): PropertyChecks = scope(extensionContext)

    override fun afterAll(context: ExtensionContext) {
        val owner = owner(context)
        owner.getStore(namespace).get(owner.uniqueId, PropertyChecks::class.java)?.close()
    }

    companion object {
        private val namespace = ExtensionContext.Namespace.create(PropertyChecksExtension::class.java)

        internal fun scope(context: ExtensionContext): PropertyChecks {
            val owner = owner(context)
            return owner.getStore(namespace).computeIfAbsent(owner.uniqueId, { PropertyChecks() }, PropertyChecks::class.java)
        }

        private fun owner(context: ExtensionContext): ExtensionContext =
            generateSequence(context) { it.parent.orElse(null) }
                .firstOrNull { it.element.orElse(null) is Class<*> }
                ?: throw ParameterResolutionException("property-history-class-context-missing")
    }
}
