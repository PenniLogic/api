package com.pennilogic.migration

import java.io.ByteArrayOutputStream
import java.io.PrintStream
import java.nio.file.Files
import java.nio.file.Path
import java.sql.Connection
import java.sql.DriverManager
import java.sql.SQLException
import java.util.Properties
import java.util.UUID

/** Writes synthetic migration files following the published convention. */
object Fixtures {
    fun header(
        phase: String = "expand",
        owner: String = "api#55",
        reversal: String = "down",
        reason: String? = null,
        expand: String? = null,
    ): String =
        buildString {
            append("-- phase: $phase\n-- owner: $owner\n-- reversal: $reversal\n")
            if (reason != null) append("-- reason: $reason\n")
            if (expand != null) append("-- expand: $expand\n")
        }

    fun write(
        directory: Path,
        id: String,
        up: String,
        down: String? = "SELECT 1;\n",
        compensating: String? = null,
    ) {
        Files.createDirectories(directory)
        Files.writeString(directory.resolve("$id.up.sql"), up)
        if (down != null) Files.writeString(directory.resolve("$id.down.sql"), down)
        if (compensating != null) Files.writeString(directory.resolve("$id.compensating.sql"), compensating)
    }

    /** A three-phase set: expand creates a table, migrate back-fills it, contract compensates instead of reversing. */
    fun threePhaseSet(directory: Path) {
        write(
            directory,
            "V001__create_widgets",
            header() + "CREATE TABLE widgets (id integer PRIMARY KEY, legacy text, modern text);\n",
            "DROP TABLE widgets;\n",
        )
        write(
            directory,
            "V002__backfill_modern",
            header(phase = "migrate", expand = "V001") + "UPDATE widgets SET modern = legacy WHERE modern IS NULL;\n",
            "UPDATE widgets SET modern = NULL;\n",
        )
        write(
            directory,
            "V003__drop_legacy",
            header(
                phase = "contract",
                reversal = "compensating",
                reason = "the dropped legacy values cannot be restored",
                expand = "V001",
            ) +
                "ALTER TABLE widgets DROP COLUMN legacy;\n",
            down = null,
            compensating = "ALTER TABLE widgets ADD COLUMN legacy text;\n",
        )
    }
}

/** Captures the runner's JSON lines so tests can assert on exact events. */
class Output {
    private val buffer = ByteArrayOutputStream()
    val stream = PrintStream(buffer, true, "UTF-8")

    fun lines(): List<String> = buffer.toString("UTF-8").lines().filter { it.isNotBlank() }

    fun event(name: String): String? = lines().firstOrNull { it.contains("\"event\":\"$name\"") }

    fun events(name: String): List<String> = lines().filter { it.contains("\"event\":\"$name\"") }

    fun require(name: String): String = event(name) ?: throw AssertionError("missing event $name in:\n$this")

    override fun toString(): String = buffer.toString("UTF-8")
}

/**
 * One disposable database per test inside the container that the Gradle integrationTest task
 * starts (or any Postgres named through MIGRATION_TEST_JDBC_URL/_DB_USER/_DB_PASSWORD).
 */
class TestDatabase(
    val url: String,
    val user: String,
    val password: String,
) {
    /** The database name, which is the last path segment of the JDBC URL. */
    val name: String get() = url.substringAfterLast('/').substringBefore('?')

    fun environment(): Map<String, String> =
        mapOf("MIGRATION_JDBC_URL" to url, "MIGRATION_DB_USER" to user, "MIGRATION_DB_PASSWORD" to password)

    fun connect(): Connection = open(url, user, password)

    fun <T> query(
        sql: String,
        read: (java.sql.ResultSet) -> T,
    ): T =
        connect().use { connection ->
            connection.createStatement().use { statement -> statement.executeQuery(sql).use { rows -> read(rows) } }
        }

    fun execute(sql: String) {
        connect().use { connection -> connection.createStatement().use { statement -> statement.execute(sql) } }
    }

    fun scalar(sql: String): Any? =
        query(sql) { rows ->
            rows.next()
            rows.getObject(1)
        }

    fun count(sql: String): Long = (scalar(sql) as Number).toLong()

    companion object {
        private val adminUrl = System.getenv("MIGRATION_TEST_JDBC_URL")
        private val adminUser = System.getenv("MIGRATION_TEST_DB_USER")
        private val adminPassword = System.getenv("MIGRATION_TEST_DB_PASSWORD")

        val skipReason: String? =
            if (adminUrl == null || adminUser == null || adminPassword == null) {
                "Postgres tests need MIGRATION_TEST_JDBC_URL, MIGRATION_TEST_DB_USER and MIGRATION_TEST_DB_PASSWORD;" +
                    " run ./gradlew integrationTest with Docker available"
            } else {
                null
            }

        fun fresh(): TestDatabase {
            val name = "t_" + UUID.randomUUID().toString().replace("-", "")
            open(requireNotNull(adminUrl), requireNotNull(adminUser), requireNotNull(adminPassword)).use { connection ->
                connection.createStatement().use { statement -> statement.execute("CREATE DATABASE $name") }
            }
            return TestDatabase(adminUrl.substringBeforeLast('/') + "/" + name, adminUser, adminPassword)
        }

        private fun open(
            url: String,
            user: String,
            password: String,
        ): Connection {
            val properties =
                Properties().apply {
                    setProperty("user", user)
                    setProperty("password", password)
                }
            val deadline = System.nanoTime() + 30_000_000_000L
            while (true) {
                try {
                    return DriverManager.getConnection(url, properties)
                } catch (error: SQLException) {
                    // Retry only while the server is still starting (connection-class SQLSTATE 08xxx).
                    if (error.sqlState?.startsWith("08") != true || System.nanoTime() > deadline) throw error
                    Thread.sleep(250)
                }
            }
        }
    }
}
