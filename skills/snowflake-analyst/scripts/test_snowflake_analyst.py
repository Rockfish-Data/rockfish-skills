#!/usr/bin/env python3
"""Unit tests for the pure logic in snowflake_analyst.py.

These cover the safety-critical pieces — identifier parsing, read-only
enforcement, auto-limit wrapping, and the export size guard — with no live
Snowflake warehouse. A tiny fake cursor exercises the metadata-lookup SQL.

Run:  python -m unittest   (from this directory)
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snowflake_analyst as sa  # noqa: E402


class TestIdentifierParsing(unittest.TestCase):
    def test_bare_name_folds_to_upper(self):
        ref = sa.parse_table_ref("orders")
        self.assertEqual((ref.db, ref.schema, ref.name), (None, None, "ORDERS"))
        self.assertEqual(ref.sql, '"ORDERS"')

    def test_bare_name_with_defaults(self):
        ref = sa.parse_table_ref("orders", "DB", "SCH")
        self.assertEqual(ref.sql, '"DB"."SCH"."ORDERS"')

    def test_fully_qualified(self):
        ref = sa.parse_table_ref("SALES.PUBLIC.ORDERS")
        self.assertEqual((ref.db, ref.schema, ref.name), ("SALES", "PUBLIC", "ORDERS"))
        self.assertEqual(ref.sql, '"SALES"."PUBLIC"."ORDERS"')

    def test_two_part_takes_default_db(self):
        ref = sa.parse_table_ref("public.orders", "DB", None)
        self.assertEqual((ref.db, ref.schema, ref.name), ("DB", "PUBLIC", "ORDERS"))
        self.assertEqual(ref.sql, '"DB"."PUBLIC"."ORDERS"')

    def test_db_without_schema_drops_db_in_sql(self):
        # "DB"."ORDERS" would be read as schema.table; render bare and rely on
        # the session database instead.
        ref = sa.parse_table_ref("orders", "DB", None)
        self.assertEqual((ref.db, ref.schema, ref.name), ("DB", None, "ORDERS"))
        self.assertEqual(ref.sql, '"ORDERS"')

    def test_schema_only_renders_two_part(self):
        ref = sa.parse_table_ref("orders", None, "SCH")
        self.assertEqual(ref.sql, '"SCH"."ORDERS"')

    def test_quoted_preserves_case(self):
        ref = sa.parse_table_ref('"myTable"')
        self.assertEqual(ref.name, "myTable")
        self.assertEqual(ref.sql, '"myTable"')

    def test_quoted_qualified_with_dot_in_name(self):
        # The dot inside quotes must not split into another part.
        ref = sa.parse_table_ref('"DB"."SCH"."My.Tbl"')
        self.assertEqual((ref.db, ref.schema, ref.name), ("DB", "SCH", "My.Tbl"))
        self.assertEqual(ref.sql, '"DB"."SCH"."My.Tbl"')

    def test_escaped_quote_roundtrips(self):
        ref = sa.parse_table_ref('"a""b"')
        self.assertEqual(ref.name, 'a"b')
        self.assertEqual(ref.sql, '"a""b"')

    def test_resolve_default(self):
        self.assertEqual(sa.resolve_default("mydb"), "MYDB")
        self.assertEqual(sa.resolve_default('"lower"'), "lower")
        self.assertIsNone(sa.resolve_default(None))

    def test_rejects_too_many_parts(self):
        with self.assertRaises(SystemExit):
            sa.parse_table_ref("ORG.SALES.PUBLIC.ORDERS")

    def test_rejects_empty_part(self):
        with self.assertRaises(SystemExit):
            sa.parse_table_ref("db..table")

    def test_rejects_unbalanced_quote(self):
        with self.assertRaises(SystemExit):
            sa.parse_table_ref('"unterminated')


class TestReadOnly(unittest.TestCase):
    def test_accepts_select(self):
        self.assertEqual(sa._assert_read_only("SELECT 1"), ("SELECT 1", "SELECT"))

    def test_strips_leading_line_comment(self):
        body, verb = sa._assert_read_only("-- note\nSELECT 1")
        self.assertEqual(verb, "SELECT")
        self.assertEqual(body, "SELECT 1")

    def test_strips_leading_block_comment(self):
        _, verb = sa._assert_read_only("/* note */ WITH x AS (SELECT 1) SELECT * FROM x")
        self.assertEqual(verb, "WITH")

    def test_accepts_show_and_describe(self):
        self.assertEqual(sa._assert_read_only("SHOW TABLES")[1], "SHOW")
        self.assertEqual(sa._assert_read_only("DESCRIBE TABLE t")[1], "DESCRIBE")

    def test_rejects_write(self):
        for sql in ("DROP TABLE t", "INSERT INTO t VALUES (1)", "UPDATE t SET x=1"):
            with self.assertRaises(SystemExit):
                sa._assert_read_only(sql)

    def test_rejects_comment_hidden_write(self):
        with self.assertRaises(SystemExit):
            sa._assert_read_only("/* harmless */ DELETE FROM t")

    def test_rejects_multiple_statements(self):
        with self.assertRaises(SystemExit):
            sa._assert_read_only("SELECT 1; SELECT 2")


class TestAutoLimit(unittest.TestCase):
    def test_wraps_select(self):
        out = sa.apply_auto_limit("SELECT * FROM t", "SELECT", 100, no_limit=False)
        self.assertTrue(out.startswith("SELECT * FROM ("))
        self.assertTrue(out.rstrip().endswith("LIMIT 100"))

    def test_literal_limit_word_cannot_defeat_cap(self):
        # Substring detection would have wrongly skipped the cap here.
        sql = "SELECT * FROM big WHERE note = 'unlimited'"
        out = sa.apply_auto_limit(sql, "SELECT", 50, no_limit=False)
        self.assertTrue(out.rstrip().endswith("LIMIT 50"))
        self.assertIn(sql, out)

    def test_no_limit_flag_passes_through(self):
        self.assertEqual(sa.apply_auto_limit("SELECT 1", "SELECT", 100, no_limit=True), "SELECT 1")

    def test_show_is_not_wrapped(self):
        self.assertEqual(sa.apply_auto_limit("SHOW TABLES", "SHOW", 100, no_limit=False), "SHOW TABLES")


class TestExportGuard(unittest.TestCase):
    def _reason(self, **kw):
        base = dict(byte_size=10, row_count=10, max_mb=1000, max_rows=1_000_000,
                    limit=None, sample=None, force=False, stream=False)
        base.update(kw)
        return sa.export_guard_reason(**base)

    def test_allows_small(self):
        self.assertIsNone(self._reason())

    def test_blocks_big_bytes(self):
        self.assertIn("--max-mb", self._reason(byte_size=2 * 1024 ** 3))

    def test_blocks_big_rows(self):
        self.assertIn("--max-rows", self._reason(row_count=2_000_000))

    def test_blocks_unknown_size(self):
        self.assertIn("unknown size", self._reason(byte_size=None, row_count=None))

    def test_small_limit_bypasses(self):
        self.assertIsNone(self._reason(byte_size=2 * 1024 ** 3, row_count=6_000_000, limit=1000))

    def test_huge_limit_does_not_bypass(self):
        # The whole point: an enormous --limit must not defeat the guard.
        self.assertIn("--max-rows", self._reason(row_count=6_000_000, limit=1_000_000_000))

    def test_bounded_pull_from_unknown_size_allowed(self):
        # A view (unknown size) with a small bound is safe.
        self.assertIsNone(self._reason(byte_size=None, row_count=None, limit=1000))

    def test_limit_scales_estimated_bytes(self):
        # 2 GB / 1e6 rows, pulling 1000 rows ≈ 2 MB → allowed.
        self.assertIsNone(self._reason(byte_size=2 * 1024 ** 3, row_count=1_000_000, limit=1000))

    def test_force_and_stream_bypass(self):
        self.assertIsNone(self._reason(byte_size=None, row_count=None, force=True))
        self.assertIsNone(self._reason(byte_size=None, row_count=None, stream=True))


class TestColumnFolding(unittest.TestCase):
    def test_unquoted_folds_upper(self):
        self.assertEqual(sa.resolve_column("amount"), "AMOUNT")
        self.assertEqual(sa.resolve_column("  Total  "), "TOTAL")

    def test_quoted_preserves_case(self):
        self.assertEqual(sa.resolve_column('"amount"'), "amount")

    def test_matches_table_name_folding(self):
        # Columns and table names must fold identically.
        self.assertEqual(sa.resolve_column("amount"), sa.parse_table_ref("amount").name)


class TestProfileFlags(unittest.TestCase):
    def test_numeric_is_orderable_and_numeric(self):
        self.assertEqual(sa.profile_flags("NUMBER"), (True, True))
        self.assertEqual(sa.profile_flags("float"), (True, True))

    def test_text_and_date_orderable_not_numeric(self):
        self.assertEqual(sa.profile_flags("TEXT"), (True, False))
        self.assertEqual(sa.profile_flags("TIMESTAMP_NTZ"), (True, False))

    def test_semi_structured_and_geo_unorderable(self):
        for dt in ("VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY", "BINARY"):
            self.assertEqual(sa.profile_flags(dt), (False, False), dt)


class TestSqlStr(unittest.TestCase):
    def test_escapes_single_quote(self):
        self.assertEqual(sa._sql_str("O'Brien"), "O''Brien")


class _FakeCursor:
    """Records executed SQL; answers CURRENT_DATABASE/SCHEMA() and column lookups."""

    def __init__(self, current_db="MYDB", current_schema="PUBLIC"):
        self.executed = []
        self._current_db = current_db
        self._current_schema = current_schema
        self._one = None

    def execute(self, sql):
        self.executed.append(sql)
        up = sql.upper()
        if "CURRENT_DATABASE" in up:
            self._one = (self._current_db,)
        elif "CURRENT_SCHEMA" in up:
            self._one = (self._current_schema,)
        else:
            self._one = None
        return self

    def fetchone(self):
        return self._one

    def fetch_pandas_all(self):
        import pandas as pd
        return pd.DataFrame(columns=[
            "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE",
            "COLUMN_DEFAULT", "COMMENT", "ORDINAL_POSITION",
        ])


class TestColumnsLookupSQL(unittest.TestCase):
    def test_qualified_scopes_to_ref_db_and_schema(self):
        cur = _FakeCursor("MYDB")
        args = SimpleNamespace(database=None, schema=None)
        sa._columns_df(cur, "SALES.PUBLIC.ORDERS", args)
        sql = cur.executed[-1]
        self.assertIn('"SALES".INFORMATION_SCHEMA.COLUMNS', sql)
        self.assertIn("TABLE_NAME = 'ORDERS'", sql)
        self.assertIn("TABLE_SCHEMA = 'PUBLIC'", sql)

    def test_bare_name_uses_current_database_and_folds(self):
        cur = _FakeCursor("MYDB", "PUBLIC")
        args = SimpleNamespace(database=None, schema=None)
        sa._columns_df(cur, "orders", args)
        sql = cur.executed[-1]
        self.assertIn('"MYDB".INFORMATION_SCHEMA.COLUMNS', sql)
        self.assertIn("TABLE_NAME = 'ORDERS'", sql)
        # Scoped to the session's current schema, not left name-only.
        self.assertIn("TABLE_SCHEMA = 'PUBLIC'", sql)


if __name__ == "__main__":
    unittest.main()
