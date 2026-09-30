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
}
