package com.pennilogic.migration

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class SqlTextTest {
    private fun strip(script: String): String = SqlText.withoutComments(script).trim()

    @Test
    fun `line and block comments are removed including nested and multi-line blocks`() {
        assertEquals("", strip("-- only a line comment"))
        assertEquals("", strip("/* only a block comment */"))
        assertEquals("", strip("/* multi\nline\ncomment */\n"))
        assertEquals("", strip("/* outer /* inner */ still a comment */"))
        assertEquals("", strip("/* never closed\nDROP TABLE t1;"))
        assertEquals("DROP TABLE t1;", strip("/* leading */ DROP TABLE t1; -- trailing"))
        assertEquals("SELECT 1;", strip("-- /* not a block comment\nSELECT 1;"))
        assertEquals("SELECT 1;\n\nSELECT 2;", strip("SELECT 1;\n/* between */\nSELECT 2;"))
    }

    @Test
    fun `comment markers inside literals identifiers and dollar quotes are kept`() {
        assertEquals("SELECT '/* text */';", strip("SELECT '/* text */';"))
        assertEquals("SELECT 'it''s -- fine';", strip("SELECT 'it''s -- fine';"))
        assertEquals("""SELECT "odd /* name" FROM t;""", strip("""SELECT "odd /* name" FROM t; -- comment"""))
        assertEquals("SELECT \$\$ /* body */ -- text \$\$;", strip("SELECT \$\$ /* body */ -- text \$\$; -- comment"))
        assertEquals("SELECT \$fn\$ -- inside \$fn\$;", strip("SELECT \$fn\$ -- inside \$fn\$; /* outside */"))
        assertEquals("SELECT \$1;", strip("SELECT \$1; -- positional parameter"))
        assertEquals("SELECT 'unterminated", strip("SELECT 'unterminated"))
        assertEquals("SELECT 'x'", strip("SELECT 'x'"))
        assertEquals("SELECT \$\$ unterminated", strip("SELECT \$\$ unterminated"))
    }

    @Test
    fun `escape strings honour backslash escapes so a following statement is not hidden`() {
        // Postgres reads E'\'' as a one-character string; the DROP after it is a real statement and must stay visible.
        assertEquals("SELECT E'\\'';\nDROP TABLE t CASCADE;", strip("SELECT E'\\'';\nDROP TABLE t CASCADE;"))
        assertEquals("SELECT E'\\\\';", strip("SELECT E'\\\\'; -- backslash literal"))
        assertEquals("SELECT E'a\\'b -- not a comment';", strip("SELECT E'a\\'b -- not a comment';"))
        assertEquals("SELECT e'\\''", strip("SELECT e'\\'' /* lowercase marker */"))
        assertEquals("SELECT E'both''\\'';", strip("SELECT E'both''\\''; -- doubled and escaped"))
        assertEquals("SELECT E'trailing\\", strip("SELECT E'trailing\\"))
        // A quote after an identifier ending in e is a plain literal: '\' closes at the second quote.
        assertEquals("SELECT abe'\\';", strip("SELECT abe'\\'; -- tail"))
        assertEquals("SELECT x_e'\\';", strip("SELECT x_e'\\'; -- tail"))
        assertEquals("SELECT x\$e'\\';", strip("SELECT x\$e'\\'; -- tail"))
        assertEquals("E'\\''", strip("E'\\'' -- marker at the start of the script"))
        assertEquals("SELECT 'x'e'y'", strip("SELECT 'x'e'y' -- e after a quote is not an identifier tail"))
        assertEquals("'\\'", strip("'\\' -- a quote at the very start is a plain literal"))
        // In a plain literal the backslash is an ordinary character (standard_conforming_strings = on).
        assertEquals("SELECT '\\';\nDROP TABLE t CASCADE;", strip("SELECT '\\';\nDROP TABLE t CASCADE;"))
    }
}
