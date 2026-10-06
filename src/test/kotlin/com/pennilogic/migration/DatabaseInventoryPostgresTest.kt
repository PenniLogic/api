package com.pennilogic.migration

import com.pennilogic.ledger.LedgerFixture
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Tag
import org.junit.jupiter.api.Test
import java.nio.file.Files
import java.nio.file.Path

@Tag("postgres")
class DatabaseInventoryPostgresTest {
    @Test
    fun `inventory covers every actual ledger column and type across reverse and reapply`() {
        val inventory =
            Json
                .parseToJsonElement(Files.readString(Path.of("database", "admission-inventory.json")))
                .jsonObject
        val columns =
            inventory.getValue("scripts").jsonArray.flatMap { script ->
                script.jsonObject.getValue("columns").jsonArray.map { column ->
                    val record = column.jsonObject
                    record.getValue("name").jsonPrimitive.content to record.getValue("sql_type").jsonPrimitive.content
                }
            }
        assertEquals(70, columns.size)
        assertEquals(columns.size, columns.toMap().size)
        val expected = columns.toMap()
        val fixture = LedgerFixture()
        fixture.connect().use { connection ->
            fun actual(): Map<String, String> =
                connection.createStatement().use { statement ->
                    statement
                        .executeQuery(
                            "SELECT n.nspname || '.' || c.relname || '.' || a.attname AS name," +
                                " format_type(a.atttypid, a.atttypmod) AS type" +
                                " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid" +
                                " JOIN pg_namespace n ON n.oid = c.relnamespace" +
                                " WHERE n.nspname = 'pennilogic' AND c.relkind IN ('r', 'p')" +
                                " AND a.attnum > 0 AND NOT a.attisdropped",
                        ).use { rows ->
                            buildMap {
                                while (rows.next()) {
                                    val type =
                                        when (val databaseType = rows.getString("type")) {
                                            "character(3)" -> "char(3)"
                                            "timestamp(3) with time zone" -> "timestamptz(3)"
                                            else -> databaseType
                                        }
                                    put(rows.getString("name"), type)
                                }
                            }
                        }
                }
            assertEquals(expected, actual())
            val runner = fixture.runner(connection)
            assertEquals(0, runner.migrateDown(0, dryRun = false))
            assertEquals(emptyMap<String, String>(), actual())
            assertEquals(0, runner.migrate(null, includeContract = false, dryRun = false))
            assertEquals(expected, actual())
            assertEquals(2, runner.snapshot().currentVersion)
        }
    }
}
