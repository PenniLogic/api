import org.gradle.api.artifacts.dsl.LockMode
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
        archiveBaseName.set("pennilogic-contracts-money-source-ea56c63")
        manifest {
            attributes(
                "Source-Repository" to "PenniLogic/contracts",
                "Source-Commit" to "ea56c63d5c9b679537bd9205b04626049c20c572",
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

val stopMigrationTestPostgres =
    tasks.register("stopMigrationTestPostgres") {
        description = "Removes the disposable Postgres container started for integrationTest."
        group = "verification"
        val idFile = migrationTestContainerId
        doLast {
            val file = idFile.get().asFile
            if (file.isFile) {
                ProcessBuilder("docker", "rm", "-f", file.readText().trim())
                    .redirectErrorStream(true)
                    .start()
                    .apply { inputStream.readAllBytes() }
                    .waitFor()
                file.delete()
            }
        }
    }

val integrationTest =
    tasks.register<Test>("integrationTest") {
        description = "Runs the Postgres-backed migration tests against a disposable, digest-pinned postgres:17 container."
        group = "verification"
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
            sourceSets.main
                .get()
                .runtimeClasspath.asPath,
        )
        systemProperty("user.timezone", "UTC")
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
            if (file.isFile) {
                ProcessBuilder("docker", "rm", "-f", file.readText().trim())
                    .redirectErrorStream(true)
                    .start()
                    .apply { inputStream.readAllBytes() }
                    .waitFor()
                file.delete()
            }
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
        classpath = sourceSets.main.get().runtimeClasspath
        mainClass.set("com.pennilogic.migration.MigrationCliKt")
        inputs.dir(migrationsDirectory)
        args("validate", "--migrations", migrationsDirectory.asFile.path)
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
    // Merge every test task's execution data that exists; a skipped integrationTest leaves none behind.
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
