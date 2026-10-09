import org.gradle.api.artifacts.dsl.LockMode
import org.gradle.api.tasks.testing.TestDescriptor
import org.gradle.api.tasks.testing.TestListener
import org.gradle.api.tasks.testing.TestResult
import org.gradle.language.jvm.tasks.ProcessResources
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit

plugins {
    kotlin("jvm") version "2.4.10"
    application
    jacoco
    id("com.diffplug.spotless") version "8.10.2"
}

group = "com.pennilogic"
version = "0.1.0"

kotlin {
    jvmToolchain(21)
    compilerOptions {
        allWarningsAsErrors.set(true)
    }
}

application {
    mainClass.set("com.pennilogic.bootstrap.ApplicationKt")
}

val prepareDatabaseAdmission =
    tasks.register<Exec>("prepareDatabaseAdmission") {
        description = "Explicitly fetches or verifies the exact accepted database-admission source bundle."
        group = "verification"
        workingDir(rootDir)
        commandLine("python", "-I", "-S", "-B", "scripts/prepare_database_admission.py", "prepare", "--fetch")
        mustRunAfter(tasks.clean)
    }

val verifyDatabaseAdmission =
    tasks.register<Exec>("verifyDatabaseAdmission") {
        description = "Verifies the complete pinned database-admission installation offline before migration entrypoints."
        group = "verification"
        workingDir(rootDir)
        commandLine("python", "-I", "-S", "-B", "scripts/prepare_database_admission.py", "verify")
        mustRunAfter(tasks.clean, prepareDatabaseAdmission)
    }

val verifyPreparedDatabaseAdmission =
    tasks.register<Exec>("verifyPreparedDatabaseAdmission") {
        description = "Verifies build-time admission inputs after explicit bounded source preparation."
        group = "verification"
        dependsOn(prepareDatabaseAdmission)
        workingDir(rootDir)
        commandLine("python", "-I", "-S", "-B", "scripts/prepare_database_admission.py", "verify")
    }

val prepareAcceptedSource =
    tasks.register<Exec>("prepareMoneyProvider") {
        description = "Verifies and prepares the immutable accepted Contracts Money source, not a release."
        group = "verification"
        workingDir(rootDir)
        commandLine("python", "scripts/money_provider.py")
    }

val moneyGuard =
    tasks.register<Exec>("moneyGuard") {
        description = "Rejects unsafe monetary JVM fields, numeric serializers and raw minor-unit arithmetic."
        group = "verification"
        workingDir(rootDir)
        commandLine("python", "scripts/check_money.py")
        dependsOn(prepareAcceptedSource)
    }

val acceptedSourceSet =
    sourceSets.create("contractsMoney") {
        java.setSrcDirs(emptyList<String>())
        kotlin.srcDir(layout.buildDirectory.dir("contracts-money/kotlin/src/main/kotlin"))
    }

tasks.named("compileContractsMoneyKotlin") {
    dependsOn(moneyGuard)
}

val acceptedSourceJar =
    tasks.register<Jar>("contractsMoneyJar") {
        description = "Packages the accepted immutable Money source for this local API build only."
        from(acceptedSourceSet.output)
        archiveBaseName.set("pennilogic-contracts-money-source-aa8d90c")
        manifest {
            attributes(
                "Source-Repository" to "PenniLogic/contracts",
                "Source-Commit" to "aa8d90cb98cec9b6dd08c91b3a4d869e47362662",
                "Provider-Kind" to "accepted-source-only",
            )
        }
    }

val mutationTool = configurations.create("moneyMutationTool")

tasks.named("compileKotlin") {
    dependsOn(moneyGuard)
}

tasks.named("compileTestKotlin") {
    dependsOn(moneyGuard)
}

dependencies {
    implementation(files(acceptedSourceJar))
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    add(acceptedSourceSet.implementationConfigurationName, "org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    implementation(platform("io.ktor:ktor-bom:3.5.2"))
    implementation("io.ktor:ktor-server-core")
    implementation("io.ktor:ktor-server-netty")
    implementation("ch.qos.logback:logback-classic:1.6.3")
    implementation("org.postgresql:postgresql:42.7.13")
    testImplementation(platform("org.junit:junit-bom:6.1.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testImplementation("io.kotest:kotest-property-jvm:6.2.5")
    testImplementation("io.ktor:ktor-server-test-host")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
    mutationTool("org.pitest:pitest-command-line:1.30.0")
    mutationTool("org.pitest:pitest-junit5-plugin:1.2.3")
    mutationTool(platform("org.junit:junit-bom:6.1.3"))
    mutationTool("org.junit.platform:junit-platform-launcher")
}

dependencyLocking {
    lockAllConfigurations()
    lockMode.set(LockMode.STRICT)
}

spotless {
    lineEndings = com.diffplug.spotless.LineEnding.UNIX
    kotlin {
        target("src/**/*.kt")
        ktlint("1.8.0")
    }
    kotlinGradle {
        target("*.gradle.kts")
        ktlint("1.8.0")
    }
}

jacoco {
    toolVersion = "0.8.15"
}

tasks.named<ProcessResources>("processTestResources") {
    dependsOn(prepareAcceptedSource)
    from(layout.buildDirectory.file("contracts-money/strategy/governance/test-strategy.json")) {
        into("testing/policy")
    }
}

val sharedTestSources =
    files(
        sourceSets.main.get().allSource,
        sourceSets.test.get().allSource,
        acceptedSourceSet.allSource,
        "build.gradle.kts",
        "settings.gradle.kts",
        "gradle.properties",
        "gradle.lockfile",
        "gradle/verification-metadata.xml",
    )

tasks.withType<Test>().configureEach {
    systemProperty("kotest.proptest.seed.write-failed", false)
    systemProperty("kotest.proptest.output.shrink-steps", true)
    systemProperty("pennilogic.testing.task", name)
    systemProperty(
        "pennilogic.testing.flakeNegativeControl",
        providers
            .gradleProperty("flakeAdviceNegativeControl")
            .map(String::toBooleanStrict)
            .orElse(false)
            .get(),
    )
    inputs.files(sharedTestSources)
    doFirst {
        val digest = MessageDigest.getInstance("SHA-256")
        for (source in sharedTestSources.files.sortedBy { it.relativeTo(rootDir).invariantSeparatorsPath }) {
            digest.update(source.relativeTo(rootDir).invariantSeparatorsPath.toByteArray(Charsets.UTF_8))
            digest.update(0.toByte())
            val content = source.readBytes()
            digest.update(content.size.toString().toByteArray(Charsets.US_ASCII))
            digest.update(0.toByte())
            digest.update(content)
        }
        systemProperty("pennilogic.testing.source", digest.digest().joinToString("") { "%02x".format(it) })
    }
    val taskName = name
    addTestListener(
        object : TestListener {
            override fun beforeSuite(suite: TestDescriptor) = Unit

            override fun beforeTest(testDescriptor: TestDescriptor) = Unit

            override fun afterTest(
                testDescriptor: TestDescriptor,
                result: TestResult,
            ) = Unit

            override fun afterSuite(
                suite: TestDescriptor,
                result: TestResult,
            ) {
                val className = suite.className ?: return
                if (suite.parent?.className != null) return
                val category =
                    when {
                        className.contains("Property") -> "property"
                        className.contains("Contract") -> "contract"
                        taskName == "integrationTest" -> "integration"
                        else -> "unit"
                    }
                logger.lifecycle(
                    """{"event":"test_category_suite","task":"$taskName","category":"$category","suite":"$className","boundary":"top_level_class","clock":"junit_epoch_ms","started_ms":${result.startTime},"finished_ms":${result.endTime},"wall_ms":${result.endTime - result.startTime},"tests":${result.testCount},"failed":${result.failedTestCount},"skipped":${result.skippedTestCount}}""",
                )
            }
        },
    )
}

tasks.test {
    useJUnitPlatform {
        excludeTags("postgres")
    }
    systemProperty(
        "app.test.classpath",
        sourceSets.main
            .get()
            .runtimeClasspath.asPath,
    )
    // Pinned so migration timestamps and the Postgres session never depend on the machine's zone database.
    systemProperty("user.timezone", "UTC")
    systemProperty(
        "pennilogic.money.source",
        layout.buildDirectory
            .dir("contracts-money/source")
            .get()
            .asFile.path,
    )
    inputs.dir(layout.buildDirectory.dir("contracts-money/source/spec"))
    testLogging {
        events("failed", "skipped")
    }
    finalizedBy(tasks.jacocoTestReport)
}

val targetedMoneyTest =
    tasks.register<Test>("moneyTest") {
        description = "Runs the actual immutable Money dependency's source-seam and arithmetic tests."
        group = "verification"
        testClassesDirs =
            sourceSets.test
                .get()
                .output.classesDirs
        classpath = sourceSets.test.get().runtimeClasspath
        outputs.upToDateWhen { false }
        outputs.cacheIf { false }
        useJUnitPlatform()
        include("com/pennilogic/money/**")
        systemProperty(
            "pennilogic.money.source",
            layout.buildDirectory
                .dir("contracts-money/source")
                .get()
                .asFile.path,
        )
        inputs.dir(layout.buildDirectory.dir("contracts-money/source/spec"))
        testLogging {
            events("failed", "skipped")
        }
    }

val acceptedCoverageReport =
    tasks.register<JacocoReport>("moneyCoverageReport") {
        description = "Measures every compiled class of the immutable Contracts Money source dependency."
        group = "verification"
        dependsOn(targetedMoneyTest)
        executionData.setFrom(layout.buildDirectory.file("jacoco/moneyTest.exec"))
        classDirectories.setFrom(acceptedSourceSet.output.classesDirs)
        sourceDirectories.setFrom(acceptedSourceSet.allSource.sourceDirectories)
        reports {
            xml.required.set(true)
            html.required.set(true)
        }
    }

val acceptedCoverageCheck =
    tasks.register<Exec>("moneyCoverageCheck") {
        description = "Qualifies the full Money package against the exact accepted Docs line and branch floors."
        group = "verification"
        dependsOn(acceptedCoverageReport)
        workingDir(rootDir)
        commandLine("python", "scripts/quality.py", "money-coverage-report")
    }

val acceptedMutationCheck =
    tasks.register<Exec>("moneyMutation") {
        description = "Runs the complete PIT Money catalogue and enforces the exact accepted package floor and budget."
        group = "verification"
        dependsOn(acceptedCoverageCheck)
        workingDir(rootDir)
        val inputFile = layout.buildDirectory.file("money-mutation-input.json")
        val launcher = javaToolchains.launcherFor { languageVersion.set(JavaLanguageVersion.of(21)) }
        val toolClasspath = mutationTool.incoming.files
        val targetClasspath = acceptedSourceSet.output.classesDirs + sourceSets.test.get().runtimeClasspath - files(acceptedSourceJar)
        doFirst {
            val inputs =
                mapOf(
                    "java" to
                        launcher
                            .get()
                            .executablePath.asFile.absolutePath,
                    "tool_classpath" to toolClasspath.files.map { it.absolutePath }.sorted(),
                    "test_classpath" to
                        targetClasspath.files
                            .filter { it.exists() }
                            .map { it.absolutePath },
                )
            inputFile.get().asFile.writeText(groovy.json.JsonOutput.toJson(inputs) + "\n")
        }
        commandLine("python", "scripts/money_mutation.py", "run", "--input-file", inputFile.get().asFile.path)
    }

val generatedClientInterop =
    tasks.register<Exec>("moneyClientInterop") {
        description = "Round-trips the API Money serializer through actually emitted Kotlin, TypeScript and Python client models."
        group = "verification"
        dependsOn(tasks.testClasses)
        workingDir(rootDir)
        outputs.upToDateWhen { false }
        outputs.cacheIf { false }
        val launcher = javaToolchains.launcherFor { languageVersion.set(JavaLanguageVersion.of(21)) }
        val backendClasspath = sourceSets.test.get().runtimeClasspath
        val configuredPython = providers.gradleProperty("moneyClientInteropPython")
        val configuredNode = providers.gradleProperty("moneyClientInteropNode")
        doFirst {
            val windows = System.getProperty("os.name").startsWith("Windows")
            val pythonLocation = System.getenv("pythonLocation")
            val python =
                configuredPython.orNull
                    ?: System.getenv("PENNILOGIC_PYTHON")
                    ?: pythonLocation?.let { File(it).resolve(if (windows) "python.exe" else "bin/python").path }
                    ?: error("Set moneyClientInteropPython to the approved absolute Python executable.")
            val pythonFile = File(python)
            require(pythonFile.isAbsolute && pythonFile.isFile) { "Approved Python executable is missing." }
            val javaFile = launcher.get().executablePath.asFile
            val approvedEnvironment =
                System
                    .getenv()
                    .filterKeys {
                        it in
                            setOf(
                                "HOME",
                                "USERPROFILE",
                                "TEMP",
                                "TMP",
                                "TMPDIR",
                                "LANG",
                                "LC_ALL",
                                "JAVA_HOME",
                                "GRADLE_USER_HOME",
                                "CI",
                                "GITHUB_ACTIONS",
                                "SYSTEMROOT",
                                "SystemRoot",
                                "WINDIR",
                            )
                    }.toMutableMap()
            val systemPaths =
                if (windows) {
                    val system = File(System.getenv("SystemRoot") ?: error("Windows system directory is missing.")).resolve("System32")
                    listOf(system, system.resolve("WindowsPowerShell/v1.0"), system.resolve("Wbem"))
                } else {
                    listOf(File("/usr/local/bin"), File("/usr/bin"), File("/bin"))
                }
            approvedEnvironment["PATH"] =
                (listOf(pythonFile.parentFile, javaFile.parentFile) + systemPaths)
                    .joinToString(File.pathSeparator) { it.absolutePath }
            approvedEnvironment["JAVA_HOME"] = javaFile.parentFile.parentFile.absolutePath
            approvedEnvironment["PENNILOGIC_PYTHON"] = pythonFile.absolutePath
            setEnvironment(approvedEnvironment)
            commandLine(
                pythonFile.absolutePath,
                "-I",
                "-S",
                "-B",
                "scripts/money_client_interop.py",
                "run",
                "--java",
                javaFile.absolutePath,
                "--classpath",
                backendClasspath.filter { it.exists() }.asPath,
            )
            configuredNode.orNull?.let { args("--node", it) }
        }
    }

// Digest-pinned image for the disposable migration test database (PostgreSQL 17.11).
val postgresImage = "postgres:17@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"

val dockerAvailable: Provider<Boolean> =
    providers
        .exec {
            commandLine("docker", "version", "--format", "{{.Server.Version}}")
            isIgnoreExitValue = true
        }.result
        .map { it.exitValue == 0 }

val migrationTestContainerId = layout.buildDirectory.file("migration-test/container-id")

object TestCategoryFixtureTiming {
    fun measure(
        phase: String,
        report: (String) -> Unit,
        clock: () -> Long = System::nanoTime,
        action: () -> Unit,
    ) {
        require(phase in setOf("setup", "teardown")) { "test-category-fixture-phase" }
        val task = if (phase == "setup") "integrationTest" else "stopMigrationTestPostgres"
        val started = clock()
        var failure: Throwable? = null
        try {
            action()
        } catch (error: Throwable) {
            failure = error
            throw error
        } finally {
            try {
                val finished = clock()
                val elapsed = finished - started
                check(elapsed >= 0) { "test-category-clock-order" }
                val outcome = if (failure == null) "completed" else "failed"
                report(
                    """{"event":"test_category_fixture","task":"$task","category":"integration","phase":"$phase","boundary":"fixture_action","clock":"gradle_process_monotonic_ns","started_ns":$started,"finished_ns":$finished,"wall_ms":${TimeUnit.NANOSECONDS.toMillis(
                        elapsed,
                    )},"outcome":"$outcome"}""",
                )
            } catch (reportingError: Throwable) {
                val original = failure
                if (original == null) throw reportingError
                if (reportingError !== original) original.addSuppressed(reportingError)
            }
        }
    }
}

object MigrationTestPostgresCleanup {
    fun remove(file: File) {
        if (!Files.exists(file.toPath(), LinkOption.NOFOLLOW_LINKS)) return
        check(Files.isRegularFile(file.toPath(), LinkOption.NOFOLLOW_LINKS)) { "Invalid migration test container marker" }
        val id = file.readText().trim()
        check(Regex("[0-9a-f]{64}").matches(id)) { "Invalid migration test container marker" }
        val diagnostics = file.resolveSibling("docker-cleanup.log")
        val process =
            ProcessBuilder("docker", "rm", "-f", "-v", id)
                .redirectErrorStream(true)
                .redirectOutput(diagnostics)
                .start()
        if (!process.waitFor(30, TimeUnit.SECONDS)) {
            process.destroyForcibly()
            check(process.waitFor(5, TimeUnit.SECONDS)) { "Disposable PostgreSQL cleanup process did not stop; marker retained" }
            error("Disposable PostgreSQL cleanup timed out; marker retained")
        }
        val acknowledged = diagnostics.readText().trim() == id
        check(process.exitValue() == 0 && acknowledged) {
            "Disposable PostgreSQL cleanup failed (exit=${process.exitValue()}, acknowledged=$acknowledged); marker retained"
        }
        check(file.delete()) { "Could not delete the disposable PostgreSQL container marker after cleanup" }
    }
}

val stopMigrationTestPostgres =
    tasks.register("stopMigrationTestPostgres") {
        description = "Removes the owned disposable Postgres container and its unshared anonymous volumes."
        group = "verification"
        val idFile = migrationTestContainerId
        doLast {
            TestCategoryFixtureTiming.measure("teardown", logger::lifecycle) {
                MigrationTestPostgresCleanup.remove(idFile.get().asFile)
            }
        }
    }

val integrationTest =
    tasks.register<Test>("integrationTest") {
        description = "Runs the Postgres-backed migration tests against a disposable, digest-pinned postgres:17 container."
        group = "verification"
        dependsOn(tasks.jar, verifyPreparedDatabaseAdmission)
        testClassesDirs =
            sourceSets.test
                .get()
                .output.classesDirs
        classpath = sourceSets.test.get().runtimeClasspath
        useJUnitPlatform {
            includeTags("postgres")
        }
        shouldRunAfter(tasks.test)
        systemProperty(
            "app.test.classpath",
            (files(tasks.jar) + configurations.runtimeClasspath.get()).asPath,
        )
        systemProperty("user.timezone", "UTC")
        systemProperty(
            "pennilogic.testing.ledgerNegativeControl",
            providers
                .gradleProperty("ledgerPropertyNegativeControl")
                .map(String::toBooleanStrict)
                .orElse(false)
                .get(),
        )
        testLogging {
            events("failed", "skipped")
        }
        val docker = dockerAvailable
        val ci = providers.environmentVariable("CI")
        onlyIf("Docker is required for the Postgres migration tests") { task ->
            // A machine without the docker binary makes the probe throw; treat that as unavailable.
            val available = runCatching { docker.get() }.getOrDefault(false)
            if (!available) {
                if (ci.isPresent) {
                    throw GradleException(
                        "integrationTest requires Docker in CI: the Postgres migration tests run on every pull request and are never skipped there.",
                    )
                }
                task.logger.warn(
                    "integrationTest SKIPPED: Docker is not available on this machine, so the Postgres migration tests" +
                        " did not run. CI (ubuntu-24.04 with Docker) runs them on every pull request.",
                )
            }
            available
        }
        val idFile = migrationTestContainerId
        val image = postgresImage
        doFirst {
            TestCategoryFixtureTiming.measure("setup", logger::lifecycle) {
                val file = idFile.get().asFile
                file.parentFile.mkdirs()
                val diagnostics = file.resolveSibling("docker-stderr.log")

                // Stdout carries the answer; stderr carries pull progress and errors, so the two are kept apart.
                // The wait is bounded so a hung image pull fails here instead of at the 30-minute CI job timeout.
                fun dockerCommand(vararg arguments: String): String {
                    val stdout = file.resolveSibling("docker-stdout.log")
                    val process = ProcessBuilder("docker", *arguments).redirectOutput(stdout).redirectError(diagnostics).start()
                    if (!process.waitFor(10, TimeUnit.MINUTES)) {
                        process.destroyForcibly()
                        error("docker ${arguments.first()} did not finish within 10 minutes: ${diagnostics.readText()}")
                    }
                    val output = stdout.readText()
                    check(process.exitValue() == 0) { "docker ${arguments.first()} failed: $output ${diagnostics.readText()}" }
                    return output
                        .trim()
                        .lines()
                        .last()
                        .trim()
                }
                // A previous run whose JVM died before its finalizer may have left this container behind; remove exactly that one.
                MigrationTestPostgresCleanup.remove(file)
                val password = UUID.randomUUID().toString()
                val id =
                    dockerCommand(
                        "run",
                        "-d",
                        "--rm",
                        "--label",
                        "pennilogic-api-migration-test",
                        "-e",
                        "POSTGRES_PASSWORD=$password",
                        "-e",
                        "POSTGRES_USER=migration",
                        "-e",
                        "POSTGRES_DB=pennilogic",
                        "-p",
                        "127.0.0.1:0:5432",
                        image,
                    )
                file.writeText(id)
                val port = dockerCommand("port", id, "5432/tcp").lines().first().substringAfterLast(':')
                val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(120)
                while (ProcessBuilder("docker", "exec", id, "pg_isready", "-h", "127.0.0.1", "-U", "migration", "-d", "pennilogic")
                        .redirectErrorStream(true)
                        .start()
                        .apply { inputStream.readAllBytes() }
                        .waitFor() != 0
                ) {
                    check(System.nanoTime() < deadline) { "the migration test Postgres container did not become ready" }
                    Thread.sleep(500)
                }
                environment("MIGRATION_TEST_JDBC_URL", "jdbc:postgresql://127.0.0.1:$port/pennilogic")
                environment("MIGRATION_TEST_DB_USER", "migration")
                environment("MIGRATION_TEST_DB_PASSWORD", password)
            }
        }
        // Direct assertion that the Postgres tests executed: a run with none discovered or any skipped is a failure.
        val junitXml = reports.junitXml.outputLocation
        doLast {
            val parser =
                javax.xml.parsers.DocumentBuilderFactory
                    .newInstance()
                    .newDocumentBuilder()
            val suites =
                junitXml
                    .get()
                    .asFile
                    .listFiles { file -> file.name.endsWith(".xml") }
                    .orEmpty()
                    .map { parser.parse(it).documentElement }
            val count = suites.sumOf { it.getAttribute("tests").toInt() }
            val skipped = suites.sumOf { it.getAttribute("skipped").toInt() }
            check(count > 0 && skipped == 0) { "integrationTest must execute the Postgres migration tests: tests=$count skipped=$skipped" }
            logger.lifecycle("""{"event":"integration_tests","count":$count,"skipped":$skipped}""")
        }
        finalizedBy(stopMigrationTestPostgres)
    }

val migrationsDirectory = layout.projectDirectory.dir("src/main/resources/db/migrations")

val migrationConventionCheck =
    tasks.register<JavaExec>("migrationConventionCheck") {
        description = "Fails on any migration file that breaks the naming, ordering, header or reversal-evidence convention."
        group = "verification"
        dependsOn(verifyPreparedDatabaseAdmission)
        classpath = sourceSets.main.get().runtimeClasspath
        mainClass.set("com.pennilogic.migration.MigrationCliKt")
        outputs.upToDateWhen { false }
        outputs.cacheIf { false }
        inputs.dir(migrationsDirectory)
        args("validate", "--migrations", migrationsDirectory.asFile.path)
    }

jacoco {
    applyTo(migrationConventionCheck.get())
}

val migrationHolder: Provider<String> =
    providers.gradleProperty("migrationHolder").orElse(providers.systemProperty("user.name").map { "$it@gradle" })

fun registerMigrationTask(
    name: String,
    taskDescription: String,
    vararg command: String,
) = tasks.register<JavaExec>(name) {
    description = taskDescription
    group = "migration"
    dependsOn(verifyDatabaseAdmission)
    classpath = sourceSets.main.get().runtimeClasspath
    mainClass.set("com.pennilogic.migration.MigrationCliKt")
    jvmArgs("-Duser.timezone=UTC")
    args(*command, "--migrations", migrationsDirectory.asFile.path, "--holder", migrationHolder.get())
    providers.gradleProperty("target").orNull?.let { args("--target", it) }
    if (providers.gradleProperty("includeContract").isPresent) {
        args("--include-contract")
    }
}

registerMigrationTask(
    "migrate",
    "Applies pending migrations; -Ptarget=N stops at a version, -PincludeContract admits contract phases.",
    "migrate",
)
registerMigrationTask("migrateDown", "Reverses the latest applied migration, or down to -Ptarget=N.", "migrate-down")
registerMigrationTask("migrateDryRun", "Prints the migrate plan and writes nothing.", "migrate", "--dry-run")
registerMigrationTask("migrateDownDryRun", "Prints the migrate-down plan and writes nothing.", "migrate-down", "--dry-run")
registerMigrationTask("migrateStatus", "Prints the registry state: current version, last applied migration and lock holder.", "status")

tasks.jacocoTestReport {
    dependsOn(tasks.test)
    mustRunAfter(integrationTest)
    mustRunAfter(targetedMoneyTest)
    mustRunAfter(migrationConventionCheck)
    // Include the real convention-check CLI alongside test JVMs; a skipped integrationTest leaves no new data.
    executionData.setFrom(fileTree(layout.buildDirectory.dir("jacoco")) { include("*.exec") })
    reports {
        xml.required.set(true)
        html.required.set(true)
    }
}

tasks.jacocoTestCoverageVerification {
    dependsOn(tasks.jacocoTestReport)
    mustRunAfter(targetedMoneyTest)
    executionData.setFrom(fileTree(layout.buildDirectory.dir("jacoco")) { include("*.exec") })
    val docker = dockerAvailable
    val ci = providers.environmentVariable("CI")
    onlyIf("the coverage gate needs the Postgres tests, which need Docker") { task ->
        val available = runCatching { docker.get() }.getOrDefault(false)
        if (!available) {
            if (ci.isPresent) {
                throw GradleException("jacocoTestCoverageVerification requires the Postgres tests in CI, which require Docker.")
            }
            task.logger.warn("jacocoTestCoverageVerification SKIPPED: without Docker the Postgres tests did not run; CI enforces the gate.")
        }
        available
    }
    violationRules {
        rule {
            limit {
                counter = "LINE"
                minimum = "0.80".toBigDecimal()
            }
            limit {
                counter = "BRANCH"
                minimum = "0.80".toBigDecimal()
            }
        }
    }
}

tasks.check {
    dependsOn(
        moneyGuard,
        acceptedCoverageCheck,
        acceptedMutationCheck,
        generatedClientInterop,
        tasks.spotlessCheck,
        tasks.jacocoTestCoverageVerification,
        integrationTest,
        migrationConventionCheck,
    )
}

tasks.withType<AbstractArchiveTask>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
}

tasks.register("resolveDependencies") {
    description = "Resolve every resolvable configuration under strict locking and checksum verification."
    inputs.files(configurations.filter { it.isCanBeResolved })
    doLast {
        logger.lifecycle("Resolved ${inputs.files.files.size} locked dependency artifacts.")
    }
}

tasks.wrapper {
    gradleVersion = "9.7.1"
    distributionType = Wrapper.DistributionType.BIN
    distributionSha256Sum = "acd53f1edaf02f1a8ff99879f8a34b302661a057d9b063ae9e35b552f804d20a"
}
