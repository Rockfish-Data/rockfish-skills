#!/usr/bin/env python3
"""snowflake_analyst.py — inspect and analyze Snowflake data like local CSVs.

The guiding principle is **push work to the warehouse, pull only small results**.
Listing, schemas, previews, and column profiles are answered by metadata queries
or server-side aggregates, so nothing scales with table size. Only `export`
moves a full table across the network, and it refuses to do so for large tables
unless you opt in.

Filesystem analogy
------------------
    databases                 ls   (top level)
    schemas   <db>            ls   <db>/
    tables    <db> <schema>   ls -l (with row counts + on-disk bytes)
    describe  <table>         inspect a file's header/columns
    head      <table>         first N rows (LIMIT)
    sample    <table>         a random N-row sample (SAMPLE)
    profile   <table>         df.describe(), computed in the warehouse
    query     "<sql>"         run read-only SQL
    export    <table>         download to a local CSV (guardrailed)

Credentials come from Snowflake's native ~/.snowflake/connections.toml
(honoring $SNOWFLAKE_HOME). Pick the section with --connection / $SNOWFLAKE_CONNECTION;
if there is exactly one section it is used by default.

Every command accepts --format {table,csv,json} so output is either easy to read
or easy to parse.

NOTE: this file is deliberately not named ``snowflake.py`` — that would shadow
the ``snowflake`` package and break ``import snowflake.connector``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import snowflake.connector


# --------------------------------------------------------------------------- #
# connections.toml handling
# --------------------------------------------------------------------------- #
def _connections_path() -> str:
    home = os.environ.get("SNOWFLAKE_HOME") or os.path.expanduser("~/.snowflake")
    return os.path.join(home, "connections.toml")


def list_connection_names() -> list[str]:
    """Enumerate connection sections in connections.toml.

    Supports both the top-level ``[<name>]`` layout of connections.toml and the
    nested ``[connections.<name>]`` layout of config.toml.
    """
    path = _connections_path()
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        conf = tomllib.load(f)
    nested = conf.get("connections", {})
    names = {k for k, v in conf.items() if k != "connections" and isinstance(v, dict)}
    if isinstance(nested, dict):
        names |= set(nested.keys())
    return sorted(names)


def resolve_connection_name(requested: str | None) -> str:
    """Pick the connection to use, erroring with a helpful list if ambiguous."""
    if requested:
        return requested
    env = os.environ.get("SNOWFLAKE_CONNECTION")
    if env:
        return env
    names = list_connection_names()
    if len(names) == 1:
        return names[0]
    raise SystemExit(
        "No connection specified and it can't be inferred. Pass --connection NAME "
        f"or set $SNOWFLAKE_CONNECTION.\nAvailable: {names or '(none found at ' + _connections_path() + ')'}"
    )


def connect(args: argparse.Namespace):
    """Open a Snowflake connection from connections.toml plus per-run overrides.

    We let snowflake-connector-python read the named section natively (it handles
    every authenticator, including programmatic_access_token), then layer on a
    statement timeout so a runaway query can't hang the session.
    """
    name = resolve_connection_name(getattr(args, "connection", None))
    kwargs: dict = {
        "connection_name": name,
        "login_timeout": 30,
        "network_timeout": args.timeout,
        "client_session_keep_alive": False,
        "session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": args.timeout},
    }
    for opt in ("database", "schema", "warehouse", "role"):
        val = getattr(args, opt, None)
        if val:
            kwargs[opt] = val
    return snowflake.connector.connect(**kwargs)


# --------------------------------------------------------------------------- #
# result fetching + rendering
# --------------------------------------------------------------------------- #
def fetch_df(cur):
    """Return a pandas DataFrame for the last executed statement.

    Uses the Arrow fast-path (fetch_pandas_all) when the statement supports it
    (plain SELECT/WITH); falls back to fetchall for SHOW/DESCRIBE/etc.
    """
    import pandas as pd

    try:
        return cur.fetch_pandas_all()
    except Exception:
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return pd.DataFrame(rows, columns=cols)


def render(df, fmt: str) -> None:
    """Print a DataFrame as an aligned table, CSV, or JSON records."""
    import pandas as pd

    if fmt == "csv":
        sys.stdout.write(df.to_csv(index=False))
    elif fmt == "json":
        # default=str so Decimals/Timestamps serialize cleanly
        print(json.dumps(df.to_dict(orient="records"), indent=2, default=str))
    else:
        if df.empty:
            print("(0 rows)")
            return
        with pd.option_context(
            "display.max_rows", None,
            "display.max_columns", None,
            "display.width", None,
            "display.max_colwidth", 60,
        ):
            print(df.to_string(index=False))
        print(f"\n({len(df)} row{'s' if len(df) != 1 else ''})")


def human_bytes(n) -> str:
    # None or a pandas/NumPy NaN (e.g. a view's null BYTES) → em-dash, not "nanPB".
    if n is None or n != n:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# --------------------------------------------------------------------------- #
# streaming helpers (bounded memory for large transfers)
# --------------------------------------------------------------------------- #
_STREAM_CHUNK_ROWS = 200_000


def stream_result_to_csv(cur, out_path: str) -> int:
    """Write the current result set to CSV in Arrow batches, one batch at a time.

    Peak memory is a single batch (a few hundred MB) regardless of table size —
    the Arrow fast path is preserved, unlike row-by-row iteration.
    """
    import pandas as pd

    total = 0
    first = True
    for batch in cur.fetch_pandas_batches():
        batch.to_csv(out_path, index=False, header=first, mode="w" if first else "a")
        total += len(batch)
        first = False
    if first:
        # Empty result: still emit a header-only CSV from the column metadata.
        cols = [c[0] for c in cur.description]
        pd.DataFrame(columns=cols).to_csv(out_path, index=False)
    return total


def iter_source_chunks(path: str, chunk_rows: int):
    """Yield DataFrames from a CSV/Parquet source in bounded row-count chunks."""
    import pandas as pd

    if path.lower().endswith((".parquet", ".pq")):
        import pyarrow.parquet as pq

        for batch in pq.ParquetFile(path).iter_batches(batch_size=chunk_rows):
            yield batch.to_pandas()
    else:
        for chunk in pd.read_csv(path, chunksize=chunk_rows):
            yield chunk


# --------------------------------------------------------------------------- #
# identifier helpers
# --------------------------------------------------------------------------- #
def quote_ident(name: str) -> str:
    """Double-quote a single identifier, escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _parse_ident_parts(ref: str):
    """Tokenize a dotted identifier into (raw_value, was_quoted) parts.

    Respects double-quoted segments (so dots inside quotes don't split) and the
    ``""`` escape for a literal quote within a quoted identifier.
    """
    parts, cur, i, n, quoted, seen_q = [], [], 0, len(ref), False, False
    while i < n:
        c = ref[i]
        if c == '"':
            if quoted and i + 1 < n and ref[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            quoted = not quoted
            seen_q = True
            i += 1
            continue
        if c == "." and not quoted:
            parts.append(("".join(cur), seen_q))
            cur, seen_q = [], False
            i += 1
            continue
        cur.append(c)
        i += 1
    if quoted:
        raise SystemExit(f"Invalid identifier {ref!r}: unbalanced double quote.")
    parts.append(("".join(cur), seen_q))
    return parts


def _resolve_ident(raw: str, was_quoted: bool) -> str:
    """Apply Snowflake folding: unquoted identifiers upper-case, quoted verbatim."""
    return raw if was_quoted else raw.upper()


def resolve_default(value):
    """Resolve a --database/--schema CLI value to its stored (folded) identifier."""
    if value is None:
        return None
    return _resolve_ident(*_parse_ident_parts(value)[0])


def resolve_column(name: str) -> str:
    """Fold a single column identifier to its stored form, like table names.

    Unquoted → upper-case (how Snowflake stores unquoted DDL columns), quoted →
    verbatim. So --columns amount hits AMOUNT, and --columns '"amount"' hits a
    quoted lower-case column.
    """
    return _resolve_ident(*_parse_ident_parts(name.strip())[0])


def write_location(db, schema):
    """(database, schema) to hand write_pandas.

    The connector requires a schema whenever a database is given, so a lone
    database (bare table + --database, no --schema) is dropped — the session that
    connect() opened already has that database selected, plus its default schema.
    """
    return (db if schema else None), schema


class TableRef:
    """A parsed table reference with Snowflake identifier folding applied.

    ``db``/``schema``/``name`` are the *stored* identifier values (what
    INFORMATION_SCHEMA holds — upper-cased unless the source quoted them), for
    metadata string matching. ``sql`` renders them back as explicitly quoted SQL,
    which is unambiguous regardless of the session's identifier case rules.
    """

    def __init__(self, db, schema, name):
        self.db, self.schema, self.name = db, schema, name

    @property
    def sql(self) -> str:
        # A database is only meaningful alongside a schema — "db"."name" would be
        # read as schema.table. Drop a db with no schema and lean on the session
        # database (connect() sets it from --database, the source of that db).
        if self.db and self.schema:
            parts = (self.db, self.schema, self.name)
        elif self.schema:
            parts = (self.schema, self.name)
        else:
            parts = (self.name,)
        return ".".join(quote_ident(p) for p in parts)

    def __repr__(self) -> str:
        return self.sql


def parse_table_ref(ref: str, default_db=None, default_schema=None) -> TableRef:
    """Parse ``[db.][schema.]table`` (quoted or not) into a folded TableRef.

    Missing db/schema fall back to the already-resolved defaults, so a bare name
    resolves within --database/--schema, a two-part ref sets schema+name, and a
    three-part ref is fully qualified.
    """
    raw = _parse_ident_parts(ref)
    parts = [_resolve_ident(v, q) for v, q in raw]
    # A Snowflake object ref is object | schema.object | db.schema.object. Reject
    # extra parts (a typo like a.b.c.d would otherwise silently target b.c.d) and
    # empty parts (e.g. "db..table"), which could mis-target a write.
    if not 1 <= len(parts) <= 3 or any(p == "" for p in parts):
        raise SystemExit(
            f"Invalid table reference {ref!r}: expected [db.][schema.]table "
            "with non-empty parts."
        )
    if len(parts) == 3:
        return TableRef(parts[0], parts[1], parts[2])
    if len(parts) == 2:
        return TableRef(default_db, parts[0], parts[1])
    return TableRef(default_db, default_schema, parts[0])


def table_ref(table: str, args: argparse.Namespace) -> TableRef:
    """Build a TableRef from a CLI table argument plus --database/--schema."""
    return parse_table_ref(
        table,
        resolve_default(getattr(args, "database", None)),
        resolve_default(getattr(args, "schema", None)),
    )


def current_database(cur) -> str | None:
    cur.execute("SELECT CURRENT_DATABASE()")
    return cur.fetchone()[0]


def current_schema(cur) -> str | None:
    cur.execute("SELECT CURRENT_SCHEMA()")
    return cur.fetchone()[0]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_connections(args) -> None:
    names = list_connection_names()
    if not names:
        print(f"No connections found in {_connections_path()}")
        return
    for n in names:
        print(n)


def cmd_databases(args) -> None:
    con = connect(args)
    try:
        cur = con.cursor()
        cur.execute('SHOW TERSE DATABASES')
        df = fetch_df(cur)
        keep = [c for c in ("name", "kind", "created_on") if c in df.columns]
        render(df[keep] if keep else df, args.format)
    finally:
        con.close()


def cmd_schemas(args) -> None:
    con = connect(args)
    try:
        cur = con.cursor()
        db = resolve_default(args.database)
        if db:
            cur.execute(f"SHOW TERSE SCHEMAS IN DATABASE {quote_ident(db)}")
        else:
            cur.execute("SHOW TERSE SCHEMAS")
        df = fetch_df(cur)
        keep = [c for c in ("database_name", "name", "created_on") if c in df.columns]
        render(df[keep] if keep else df, args.format)
    finally:
        con.close()


def cmd_tables(args) -> None:
    """List tables/views with ROW_COUNT and on-disk BYTES — an `ls -l` for tables.

    These come from INFORMATION_SCHEMA.TABLES (pure metadata, no data scan), so
    you can gauge how big a table is *before* touching its rows. Ordered largest
    first, since size is what drives network cost.
    """
    con = connect(args)
    try:
        cur = con.cursor()
        db = resolve_default(args.database) or current_database(cur)
        if not db:
            raise SystemExit("No database in context. Pass --database DB (see `databases`).")
        schema = resolve_default(args.schema)
        where = ["TABLE_SCHEMA <> 'INFORMATION_SCHEMA'"]
        if schema:
            where.append(f"TABLE_SCHEMA = '{_sql_str(schema)}'")
        sql = (
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, "
            "LAST_ALTERED "
            f"FROM {quote_ident(db)}.INFORMATION_SCHEMA.TABLES "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY BYTES DESC NULLS LAST"
        )
        cur.execute(sql)
        df = fetch_df(cur)
        if not df.empty and args.format == "table":
            df = df.copy()
            df["SIZE"] = df["BYTES"].map(human_bytes)
            df = df[["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE", "ROW_COUNT", "SIZE", "LAST_ALTERED"]]
        render(df, args.format)
    finally:
        con.close()


def _sql_str(value: str) -> str:
    """Escape a Python string for use as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _columns_df(cur, table: str, args):
    """Column metadata (name/type/nullable/default/comment) via INFORMATION_SCHEMA."""
    ref = table_ref(table, args)
    db = ref.db or current_database(cur)
    if not db:
        raise SystemExit("No database in context. Pass --database DB or a qualified table name.")
    # Scope to a schema so a same-named table in another schema can't shadow the
    # lookup; fall back to the session's current schema when the ref omits one.
    schema = ref.schema or current_schema(cur)
    where = [f"TABLE_NAME = '{_sql_str(ref.name)}'"]
    if schema:
        where.append(f"TABLE_SCHEMA = '{_sql_str(schema)}'")
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COMMENT, "
        "ORDINAL_POSITION "
        f"FROM {quote_ident(db)}.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE {' AND '.join(where)} ORDER BY ORDINAL_POSITION"
    )
    return fetch_df(cur)


def cmd_describe(args) -> None:
    con = connect(args)
    try:
        cur = con.cursor()
        df = _columns_df(cur, args.table, args)
        if df.empty:
            raise SystemExit(f"Table not found or no columns: {args.table}")
        render(df, args.format)
    finally:
        con.close()


def cmd_head(args) -> None:
    con = connect(args)
    try:
        cur = con.cursor()
        cur.execute(f"SELECT * FROM {table_ref(args.table, args).sql} LIMIT {int(args.rows)}")
        render(fetch_df(cur), args.format)
    finally:
        con.close()


def cmd_sample(args) -> None:
    """A random N-row sample via Snowflake SAMPLE (cheap, representative).

    Views don't support SAMPLE, so fall back to LIMIT on failure.
    """
    con = connect(args)
    try:
        cur = con.cursor()
        t = table_ref(args.table, args).sql
        n = int(args.rows)
        try:
            cur.execute(f"SELECT * FROM {t} SAMPLE ({n} ROWS)")
            df = fetch_df(cur)
        except snowflake.connector.errors.ProgrammingError:
            cur.execute(f"SELECT * FROM {t} LIMIT {n}")
            df = fetch_df(cur)
            print("(SAMPLE unsupported here; fell back to LIMIT)", file=sys.stderr)
        render(df, args.format)
    finally:
        con.close()


_NUMERIC_TYPES = {
    "NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "SMALLINT",
    "TINYINT", "BYTEINT", "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE",
    "DOUBLE PRECISION", "REAL",
}

# Semi-structured, geospatial, and binary types are not orderable/hashable, so
# MIN/MAX and DISTINCT over them error out — one such column would fail the whole
# profile query. For these we compute only a non-null count.
_UNORDERABLE_TYPES = {
    "VARIANT", "OBJECT", "ARRAY", "MAP", "GEOGRAPHY", "GEOMETRY", "VECTOR", "BINARY",
}


def profile_flags(dtype: str):
    """(orderable, numeric) for a Snowflake DATA_TYPE — which aggregates are safe."""
    dt = dtype.upper()
    return dt not in _UNORDERABLE_TYPES, dt in _NUMERIC_TYPES


def cmd_profile(args) -> None:
    """Per-column profile (like df.describe()) computed entirely in the warehouse.

    A single aggregate query returns non-null counts, null %, distinct count, and
    min/max (plus mean/stddev for numerics). Only the tiny summary crosses the
    network — never the rows. Distinct counts use APPROX_COUNT_DISTINCT by default
    (HyperLogLog, far cheaper on large tables); pass --exact for COUNT(DISTINCT).
    """
    import pandas as pd

    con = connect(args)
    try:
        cur = con.cursor()
        cols_df = _columns_df(cur, args.table, args)
        if cols_df.empty:
            raise SystemExit(f"Table not found or no columns: {args.table}")

        # Fold requested column names the same way table identifiers are folded,
        # then match against the stored INFORMATION_SCHEMA names.
        wanted = {resolve_column(c) for c in args.columns.split(",")} if args.columns else None
        rows = []
        for _, r in cols_df.iterrows():
            if wanted is not None and r["COLUMN_NAME"] not in wanted:
                continue
            rows.append((r["COLUMN_NAME"], str(r["DATA_TYPE"]).upper()))
        if wanted is not None and not rows:
            available = ", ".join(cols_df["COLUMN_NAME"].astype(str))
            raise SystemExit(
                f"None of --columns {args.columns!r} match {args.table} "
                f"(names are case-sensitive; unquoted folds to upper). Available: {available}"
            )
        if args.max_columns and len(rows) > args.max_columns:
            print(
                f"(profiling first {args.max_columns} of {len(rows)} columns; "
                "narrow with --columns or raise --max-columns)",
                file=sys.stderr,
            )
            rows = rows[: args.max_columns]

        distinct_fn = "COUNT(DISTINCT {q})" if args.exact else "APPROX_COUNT_DISTINCT({q})"
        selects = ["COUNT(*) AS total_rows"]
        for i, (name, dtype) in enumerate(rows):
            q = quote_ident(name)
            orderable, numeric = profile_flags(dtype)
            selects.append(f"COUNT({q}) AS c{i}_nonnull")
            if orderable:
                selects.append(distinct_fn.format(q=q) + f" AS c{i}_distinct")
                selects.append(f"CAST(MIN({q}) AS STRING) AS c{i}_min")
                selects.append(f"CAST(MAX({q}) AS STRING) AS c{i}_max")
            if numeric:
                selects.append(f"CAST(AVG({q}) AS STRING) AS c{i}_mean")
                selects.append(f"CAST(STDDEV({q}) AS STRING) AS c{i}_stddev")

        cur.execute(f"SELECT {', '.join(selects)} FROM {table_ref(args.table, args).sql}")
        agg = fetch_df(cur).iloc[0]
        # Snowflake upper-cases unquoted aliases; normalize so lookups by the
        # lower-case alias names below succeed regardless of fetch path.
        agg.index = agg.index.str.lower()
        total = int(agg["total_rows"])

        distinct_key = "distinct" if args.exact else "distinct~"
        out = []
        for i, (name, dtype) in enumerate(rows):
            orderable, numeric = profile_flags(dtype)
            nonnull = int(agg[f"c{i}_nonnull"])
            nulls = total - nonnull
            out.append({
                "column": name,
                "type": dtype,
                "non_null": nonnull,
                "nulls": nulls,
                "null_pct": round(100 * nulls / total, 2) if total else 0.0,
                distinct_key: int(agg[f"c{i}_distinct"]) if orderable else None,
                "min": agg.get(f"c{i}_min") if orderable else None,
                "max": agg.get(f"c{i}_max") if orderable else None,
                "mean": agg.get(f"c{i}_mean") if numeric else None,
                "stddev": agg.get(f"c{i}_stddev") if numeric else None,
            })
        print(f"{table_ref(args.table, args).sql}: {total} rows", file=sys.stderr)
        render(pd.DataFrame(out), args.format)
    finally:
        con.close()


_READ_VERBS = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")


def _strip_leading_comments(stmt: str) -> str:
    """Drop leading line/block comments so the first keyword is the real verb."""
    body = stmt
    while True:
        b = body.lstrip()
        if b.startswith("--"):
            body = b.split("\n", 1)[1] if "\n" in b else ""
        elif b.startswith("/*") and "*/" in b:
            body = b.split("*/", 1)[1]
        else:
            return b


def _assert_read_only(sql: str):
    """Reject anything that isn't a single read statement.

    Guards against accidental writes and multi-statement injection. Returns the
    comment-stripped statement and its verb, so callers don't re-derive the verb
    from the raw (possibly comment-led) text.
    """
    statements = [s for s in sql.split(";") if s.strip()]
    if len(statements) > 1:
        raise SystemExit("Only a single statement is allowed (found multiple ';'-separated).")
    stmt = statements[0].strip() if statements else ""
    body = _strip_leading_comments(stmt)
    verb = body.split(None, 1)[0].upper() if body else ""
    if verb not in _READ_VERBS:
        raise SystemExit(
            f"Refusing to run a non-read statement (starts with {verb or 'nothing'!r}). "
            f"Allowed: {', '.join(_READ_VERBS)}."
        )
    # A `WITH` statement is normally a CTE, but Snowflake also has the anonymous
    # stored-procedure form `WITH <name> AS PROCEDURE ... CALL <name>()`, whose
    # body can run DML. The `AS PROCEDURE` token pair is distinctive to that form,
    # so reject it rather than trying to fully parse the WITH clause.
    if re.search(r"\bAS\s+PROCEDURE\b", body, re.IGNORECASE):
        raise SystemExit(
            "Refusing an anonymous stored-procedure form (WITH ... AS PROCEDURE ... "
            "CALL), which can execute writes. Only read queries are allowed."
        )
    return body, verb


def apply_auto_limit(body: str, verb: str, limit: int, no_limit: bool) -> str:
    """Cap an eligible query's row count by wrapping it as a subquery.

    Wrapping (``SELECT * FROM (<query>) LIMIT n``) is robust where substring
    detection is not: a literal or comment containing the word ``LIMIT``, or an
    existing inner ``LIMIT``, can't defeat the cap. Only SELECT/WITH are wrapped;
    SHOW/DESCRIBE/EXPLAIN are returned unchanged.
    """
    if no_limit or verb not in ("SELECT", "WITH"):
        return body
    inner = body.rstrip().rstrip(";").rstrip()
    return f"SELECT * FROM (\n{inner}\n) LIMIT {int(limit)}"


def cmd_query(args) -> None:
    """Run a read-only query.

    Printing to stdout is a preview: SELECT/WITH are capped at --limit (default
    100) unless --no-limit. Writing to --out is not a preview, so silently
    truncating would be wrong — it requires an explicit choice: --no-limit (add
    --stream if large) for the full result, or --limit N to cap it deliberately.
    """
    if args.stream and not args.out:
        raise SystemExit("--stream writes to a file; pass --out PATH.")
    body, verb = _assert_read_only(args.sql)

    if args.no_limit:
        cap = None
    elif args.out:
        if args.limit is None and not args.stream:
            raise SystemExit(
                "query --out writes a file, not a preview: pass --no-limit for the full "
                "result (add --stream if it's large), or --limit N to cap it."
            )
        cap = args.limit  # None under --stream => full result
    else:
        cap = args.limit if args.limit is not None else 100

    stmt = apply_auto_limit(body, verb, cap or 0, no_limit=cap is None)
    con = connect(args)
    try:
        cur = con.cursor()
        cur.execute(stmt)
        if args.out and args.stream:
            n = stream_result_to_csv(cur, args.out)
            print(f"streamed {n} rows to {args.out}")
        elif args.out:
            df = fetch_df(cur)
            df.to_csv(args.out, index=False)
            print(f"wrote {len(df)} rows to {args.out}")
        else:
            render(fetch_df(cur), args.format)
    finally:
        con.close()


def cmd_import(args) -> None:
    """Load a local CSV/Parquet into a Snowflake table — the inverse of `export`.

    This is how you get generated data *into* Snowflake, the same way you'd write
    a CSV locally: generate to a file (e.g. via the generate-from-schema skill),
    then import it. Uses write_pandas, which PUTs compressed Parquet chunks to a
    temporary internal stage and COPYs them in — chunked and parallel, so it stays
    network-efficient even for large frames.

    Modes:
      append  (default) create the table if missing, else append rows
      replace           drop and recreate the table from this file

    With --stream the source is read in bounded row chunks (default 200k rows,
    or --chunk-size) so memory never holds the whole file — for data larger than
    RAM. In replace mode only the first chunk overwrites; the rest append, so the
    file's other chunks aren't wiped.
    """
    import pandas as pd
    from snowflake.connector.pandas_tools import write_pandas

    path = args.csv
    if not os.path.exists(path):
        raise SystemExit(f"Source file not found: {path}")
    # Symmetric with export: the whole-file path materializes one in-memory copy
    # of the dataset, so refuse very large sources unless streaming/forced. The
    # in-RAM footprint concern is the same in both directions.
    if not args.stream and not args.force:
        size = os.path.getsize(path)
        if size > args.max_mb * 1024 * 1024:
            raise SystemExit(
                f"{path} is {human_bytes(size)} (> --max-mb {args.max_mb}); it would be "
                "held in memory (~3-5x that). Re-run with --stream (bounded memory) or --force."
            )
    ref = table_ref(args.table, args)
    tbl = ref.name
    # write_pandas needs a schema whenever a database is passed; drop a lone
    # database and let the session (which connect() set from --database) supply it.
    write_db, write_schema = write_location(ref.db, ref.schema)
    overwrite = args.mode == "replace"
    dest = ref.sql
    # quote_identifiers=True preserves the source header's case in the created
    # columns (case-sensitive), matching how the read side expects them.
    con = connect(args)
    try:
        if args.stream:
            total = ncols = 0
            first = True
            for chunk in iter_source_chunks(path, args.chunk_size or _STREAM_CHUNK_ROWS):
                if chunk.empty:
                    continue
                success, _, nrows, _ = write_pandas(
                    con, chunk, tbl, database=write_db, schema=write_schema,
                    auto_create_table=True,
                    overwrite=overwrite and first,  # only the first chunk replaces
                    quote_identifiers=True,
                )
                if not success:
                    raise SystemExit(f"write_pandas reported failure loading into {tbl}")
                total += nrows
                ncols = len(chunk.columns)
                first = False
            if first:
                raise SystemExit(f"{path} has no rows to load.")
            verb = "replaced" if overwrite else "loaded"
            print(f"{verb} {total} rows x {ncols} cols into {dest} (streamed)")
            return

        df = pd.read_parquet(path) if path.lower().endswith((".parquet", ".pq")) else pd.read_csv(path)
        if df.empty:
            raise SystemExit(f"{path} has no rows to load.")
        success, nchunks, nrows, _ = write_pandas(
            con, df, tbl,
            database=write_db, schema=write_schema,
            auto_create_table=True,
            overwrite=overwrite,
            quote_identifiers=True,
            chunk_size=args.chunk_size,
        )
        if not success:
            raise SystemExit(f"write_pandas reported failure loading into {tbl}")
        verb = "replaced" if overwrite else "loaded"
        print(f"{verb} {nrows} rows x {len(df.columns)} cols into {dest} ({nchunks} chunk(s))")
    finally:
        con.close()


def export_guard_reason(*, byte_size, row_count, max_mb, max_rows, limit, sample, force, stream):
    """Return why an export should be blocked, or None to allow it.

    The check is on the rows/bytes actually fetched, not on the flags used. A
    --limit/--sample only helps to the extent it is *small*: `--limit 1000000000`
    doesn't bound anything, so it can't bypass the guard. Column projection and a
    WHERE clause don't bound size and aren't considered here. Unknown size (a
    view, or a table missing from metadata) with no bound also blocks — opt in
    with --stream/--force.
    """
    if force or stream:
        return None
    # Smallest positive row bound requested via --limit/--sample (None = none).
    bounds = [b for b in (limit, sample) if b is not None and b > 0]
    bound = min(bounds) if bounds else None

    effective_rows = row_count
    if bound is not None:
        effective_rows = bound if row_count is None else min(row_count, bound)
    effective_bytes = byte_size
    if byte_size is not None and row_count and bound is not None:
        # Scale the on-disk size by the fraction of rows we'll actually pull.
        effective_bytes = byte_size * min(bound, row_count) / row_count

    if effective_rows is None and effective_bytes is None:
        return "has unknown size (a view, or no table metadata)"
    if effective_bytes is not None and effective_bytes > max_mb * 1024 * 1024:
        return f"is ~{human_bytes(effective_bytes)} (> --max-mb {max_mb})"
    if effective_rows is not None and effective_rows > max_rows:
        return f"has {effective_rows} rows (> --max-rows {max_rows})"
    return None


def cmd_export(args) -> None:
    """Download a table (or subset) to a local CSV, with a size guardrail.

    Whole-dataset by default: it checks ROW_COUNT/BYTES first and refuses tables
    over --max-mb / --max-rows — or of unknown size (views) — unless you pass
    --force or bound the rows with --limit / --sample. (--columns and --where
    shape the query but don't bound its size, so they don't skip the guard.) Pass
    --stream to fetch in Arrow batches with bounded memory instead — this skips
    the size guard, since memory no longer scales with the table.
    """
    ref = table_ref(args.table, args)
    con = connect(args)
    try:
        cur = con.cursor()
        # Look up size from metadata, scoped to db + schema + name so a same-named
        # table in another schema can't spoof the guard. Without a resolvable
        # schema we leave size unknown, which the guard treats as "must opt in".
        db = ref.db or current_database(cur)
        schema = ref.schema or current_schema(cur)
        row_count = byte_size = None
        if db and schema:
            cur.execute(
                "SELECT ROW_COUNT, BYTES FROM "
                f"{quote_ident(db)}.INFORMATION_SCHEMA.TABLES "
                f"WHERE TABLE_NAME = '{_sql_str(ref.name)}' "
                f"AND TABLE_SCHEMA = '{_sql_str(schema)}' "
                "ORDER BY BYTES DESC NULLS LAST LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                row_count, byte_size = row

        reason = export_guard_reason(
            byte_size=byte_size, row_count=row_count,
            max_mb=args.max_mb, max_rows=args.max_rows,
            limit=args.limit, sample=args.sample,
            force=args.force, stream=args.stream,
        )
        if reason:
            raise SystemExit(
                f"{ref.sql} {reason}. Re-run with --stream (bounded memory) or --force, "
                "or reduce with a smaller --limit/--sample."
            )

        cols = ", ".join(quote_ident(resolve_column(c)) for c in args.columns.split(",")) if args.columns else "*"
        sql = f"SELECT {cols} FROM {ref.sql}"
        if args.sample:
            sql += f" SAMPLE ({int(args.sample)} ROWS)"
        if args.where:
            sql += f" WHERE {args.where}"
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql)
        if args.stream:
            n = stream_result_to_csv(cur, args.out)
            print(f"streamed {n} rows to {args.out}")
        else:
            df = fetch_df(cur)
            df.to_csv(args.out, index=False)
            print(f"wrote {len(df)} rows x {len(df.columns)} cols to {args.out}")
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    # Shared options live on a parent parser so they can be passed *after* the
    # subcommand — `tables --database X` reads more naturally than the reverse.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--connection", help="connections.toml section (or $SNOWFLAKE_CONNECTION)")
    common.add_argument("--database", help="database context / override")
    common.add_argument("--schema", help="schema context / override")
    common.add_argument("--warehouse", help="warehouse override")
    common.add_argument("--role", help="role override")
    common.add_argument("--timeout", type=int, default=120, help="statement timeout seconds (default 120)")
    common.add_argument("--format", choices=("table", "csv", "json"), default="table",
                        help="output format (default table)")

    p = argparse.ArgumentParser(
        prog="snowflake_analyst.py",
        description="Inspect and analyze Snowflake data like local CSV files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("connections", parents=[common],
                   help="list connection names in connections.toml").set_defaults(func=cmd_connections)
    sub.add_parser("databases", parents=[common],
                   help="list databases (ls, top level)").set_defaults(func=cmd_databases)
    sub.add_parser("schemas", parents=[common],
                   help="list schemas (ls of a database)").set_defaults(func=cmd_schemas)
    sub.add_parser("tables", parents=[common],
                   help="list tables with row counts + bytes (ls -l)").set_defaults(func=cmd_tables)

    d = sub.add_parser("describe", parents=[common], help="show a table's columns and types")
    d.add_argument("table")
    d.set_defaults(func=cmd_describe)

    h = sub.add_parser("head", parents=[common], help="first N rows (LIMIT)")
    h.add_argument("table")
    h.add_argument("-n", "--rows", type=int, default=20)
    h.set_defaults(func=cmd_head)

    s = sub.add_parser("sample", parents=[common], help="random N-row sample (SAMPLE)")
    s.add_argument("table")
    s.add_argument("-n", "--rows", type=int, default=20)
    s.set_defaults(func=cmd_sample)

    pr = sub.add_parser("profile", parents=[common], help="per-column stats, computed server-side")
    pr.add_argument("table")
    pr.add_argument("--columns", help="comma-separated subset of columns")
    pr.add_argument("--max-columns", type=int, default=50, help="cap columns profiled (default 50)")
    pr.add_argument("--exact", action="store_true", help="exact distinct counts (slower)")
    pr.set_defaults(func=cmd_profile)

    q = sub.add_parser("query", parents=[common], help="run a read-only SQL query")
    q.add_argument("sql")
    q.add_argument("--limit", type=int, default=None,
                   help="row cap for SELECT/WITH (default 100 when printing; required "
                        "with --out unless --no-limit/--stream)")
    q.add_argument("--no-limit", action="store_true", help="do not cap rows (full result)")
    q.add_argument("--out", help="write the result to this CSV instead of printing")
    q.add_argument("--stream", action="store_true",
                   help="stream result to --out in Arrow batches (bounded memory)")
    q.set_defaults(func=cmd_query)

    im = sub.add_parser("import", parents=[common],
                        help="load a local CSV/Parquet into a table (inverse of export)")
    im.add_argument("csv", help="source CSV or Parquet file")
    im.add_argument("table", help="destination table (bare, or DB.SCHEMA.TABLE)")
    im.add_argument("--mode", choices=("append", "replace"), default="append",
                    help="append rows (default) or replace the table")
    im.add_argument("--chunk-size", type=int,
                    help="rows per chunk (upload chunk; or read batch when --stream)")
    im.add_argument("--max-mb", type=int, default=1000,
                    help="refuse source files larger than this (default 1000; same as export)")
    im.add_argument("--force", action="store_true", help="skip the size guardrail")
    im.add_argument("--stream", action="store_true",
                    help="read the source in bounded row chunks (for data larger than RAM)")
    im.set_defaults(func=cmd_import)

    e = sub.add_parser("export", parents=[common], help="download a table to a local CSV (guardrailed)")
    e.add_argument("table")
    e.add_argument("--out", required=True, help="destination CSV path")
    e.add_argument("--columns", help="comma-separated subset of columns")
    e.add_argument("--where", help="WHERE predicate to filter rows")
    e.add_argument("--sample", type=int, help="download a random N-row SAMPLE instead")
    e.add_argument("--limit", type=int, help="cap rows downloaded")
    e.add_argument("--max-mb", type=int, default=1000, help="refuse tables larger than this (default 1000)")
    e.add_argument("--max-rows", type=int, default=1_000_000, help="refuse tables with more rows (default 1e6)")
    e.add_argument("--force", action="store_true", help="skip the size guardrail")
    e.add_argument("--stream", action="store_true",
                   help="fetch in Arrow batches with bounded memory (skips size guard)")
    e.set_defaults(func=cmd_export)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except snowflake.connector.errors.Error as e:
        print(f"Snowflake error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
