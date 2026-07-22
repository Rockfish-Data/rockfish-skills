---
name: snowflake-analyst
description: Explore, analyze, and load data in Snowflake the way you would local CSV files — list databases/schemas/tables like listing directories, inspect a table's columns, preview or sample rows, profile column statistics, and import generated/local data into a table. Use when a user wants to look at, understand, summarize, pull from, or write to a Snowflake table, warehouse, or database. Trigger on phrases like "what's in my Snowflake", "list the tables", "describe this table", "sample/preview a Snowflake table", "profile the columns", "run a query against Snowflake", "how big is this table", "export a Snowflake table to CSV", or "load/import/upload/write data into Snowflake". Built to stay cheap on large tables — computation is pushed to the warehouse and only small results cross the network.
---

# Snowflake analyst

Analyze Snowflake data like local CSV files, without dragging whole tables across
the network. The bundled tool
[`scripts/snowflake_analyst.py`](scripts/snowflake_analyst.py) wraps
`snowflake-connector-python` with a small set of filesystem-flavored commands.

## The core principle: push work down, pull only small results

Reading a CSV means loading the whole file. A Snowflake table can be terabytes,
so **never** do that reflexively. Every command here answers from **metadata** or
**server-side aggregates** — only `export` moves bulk data, and it refuses large
tables unless you opt in. When you need an answer *about* the data (counts,
distributions, aggregates), write SQL and let the warehouse compute it; bring back
only the summary.

## Commands (filesystem analogy)

| Command | Analogy | What it does |
| --- | --- | --- |
| `connections` | — | list connection names in `connections.toml` |
| `databases` | `ls` | list databases |
| `schemas [--database DB]` | `ls DB/` | list schemas |
| `tables [--database DB] [--schema S]` | `ls -l` | list tables **with row counts and on-disk bytes** |
| `describe TABLE` | inspect header | columns, types, nullability |
| `head TABLE [-n N]` | `head` | first N rows (`LIMIT`) |
| `sample TABLE [-n N]` | — | a random N-row `SAMPLE` (more representative than head) |
| `profile TABLE` | `df.describe()` | per-column non-null/null%/distinct/min/max/mean/stddev, computed in the warehouse |
| `query "SQL"` | — | run **read-only** SQL (auto-`LIMIT`ed) |
| `export TABLE --out f.csv` | download | pull to a local CSV, **guardrailed** by size |
| `import f.csv TABLE` | save / write | load a local CSV/Parquet **into** a table (inverse of export) |

Run `python scripts/snowflake_analyst.py <command> --help` for full flags.

## Recommended workflow

1. **Orient** — `databases`, then `tables --database DB` to see what exists *and
   how big it is*. The `SIZE`/`ROW_COUNT` columns tell you what's safe to pull.
2. **Understand shape** — `describe TABLE` for columns, `head`/`sample` for a feel
   of the values.
3. **Analyze in place** — `profile TABLE` for a column summary, or `query` for any
   aggregate/filter. This is where the real analysis happens; it scales to huge
   tables because the warehouse does the work.
4. **Only then, if needed, materialize** — `export` a *small* result or a filtered
   subset to CSV for local tooling (plots, notebooks). Don't export a big table
   just to compute something SQL could compute.

## Writing data in: `import` (the CSV workflow, for Snowflake)

Locally you *generate data and write a CSV*. The Snowflake equivalent is
**generate → import into a table**. `import` is the inverse of `export`:

```bash
python $S import gen.csv MYDB.PUBLIC.CUSTOMERS               # append (auto-creates)
python $S import gen.csv MYDB.PUBLIC.CUSTOMERS --mode replace  # drop & recreate
```

- **Bridge from generation.** To get *synthetic* data into Snowflake, use the
  `generate-from-schema` skill (or any tool) to write a CSV, then `import` it —
  the same two-step you'd do with a local CSV, just with Snowflake as the sink.
- **Modes.** `append` (default) creates the table if it doesn't exist, otherwise
  appends rows. `replace` drops and recreates it from the file (idempotent reloads).
- **Efficient by construction.** `import` uses `write_pandas`, which PUTs
  compressed Parquet chunks to a temporary internal stage and `COPY`s them in —
  chunked and parallel, so large frames stay network-friendly. Tune with
  `--chunk-size`.
- **Memory: same rule as `export`** (see below). By default `import` holds the
  whole file in memory and refuses sources over `--max-mb`; `--stream` reads it in
  bounded row chunks (200k, or `--chunk-size`). In `--mode replace` only the first
  chunk overwrites; the rest append, so the file isn't truncated.
- **It writes to the user's account.** `import` (and `export`) are the only
  state-changing paths; confirm the destination `DB.SCHEMA.TABLE` and mode before
  running one. Everything else in this skill is read-only.
- Parquet sources are detected by extension (`.parquet`/`.pq`); anything else is
  read as CSV.

## Latency & network notes

- **Size first.** `tables` reports `ROW_COUNT` and `BYTES` from
  `INFORMATION_SCHEMA` (pure metadata, no scan) so you know a table's cost before
  touching it. It's ordered largest-first.
- **Compute server-side; download only the summary.** `sample` uses Snowflake
  `SAMPLE` to avoid reading the whole table; `profile` runs aggregates in the
  warehouse and returns only the per-column summary — the rows never cross the
  network. (The aggregates still cost warehouse work, and distinct counts scan;
  `APPROX_COUNT_DISTINCT` is the cheaper default — pass `profile --exact` only
  when you truly need exact counts.)
- **Aggregate, don't download.** For "how many…", "average…", "top N…", use
  `query` with `GROUP BY`. The result is a handful of rows regardless of table size.
- A statement timeout (`--timeout`, default 120s) is set on every session so a
  runaway query can't hang.

### In-memory footprint & `--stream` — one rule, both directions

The cost to manage is **one in-RAM copy of the dataset** (~3–5× its on-disk size,
since CSV/pandas inflates and `import` needs a transient Parquet copy). That cost
is the **same whether data flows down (`export`, `query --out`) or up (`import`)**,
so both follow the identical rule:

- **Default = whole dataset in memory.** Fast and simple; fine as long as the
  dataset fits comfortably in available RAM. Both directions **refuse** a dataset
  over `--max-mb` (default **1000** each) — `export` on the table's `BYTES`,
  `import` on the source file size — rather than silently risking an OOM.
- **`--stream` = bounded memory.** Fetches (down) in Arrow batches / reads (up) in
  row chunks, so peak memory is one batch regardless of dataset size. It keeps the
  Arrow fast path (batched, not slow row-by-row) and skips the size guard, since
  memory no longer scales with the data.

**You (the agent) decide which to use — the tool won't auto-switch.** Before a big
transfer, check the size first (`tables` gives `BYTES`/`ROW_COUNT` for free; use
the file size for `import`), estimate peak ≈ size × ~5, and reach for `--stream`
when that's a large fraction of available RAM, when the size is unknown (e.g. a
view, whose `BYTES` is null), or when running somewhere lean (CI, a container).
Otherwise the default path is simpler. `--force` bypasses the guard without
streaming — only when you're sure it fits.

## Connection

Credentials are read from Snowflake's native `~/.snowflake/connections.toml`
(honoring `$SNOWFLAKE_HOME`) — the same file the Snowflake CLI and connector use.
Nothing is read from this repo. Pick a section with `--connection NAME` or
`$SNOWFLAKE_CONNECTION`; if the file has exactly one section it's used by default.
Override `--database` / `--schema` / `--warehouse` / `--role` per run. List the
available sections with `python scripts/snowflake_analyst.py connections`.

```toml
# ~/.snowflake/connections.toml   (chmod 600)
[my-warehouse]
account       = "xy12345"
user          = "alice"
authenticator = "programmatic_access_token"   # or password = "..."
token         = "..."
warehouse     = "COMPUTE_WH"
role          = "ANALYST"
```

## Examples

```bash
S=scripts/snowflake_analyst.py

# What data do I have, and how big is it?
python $S databases
python $S tables --database SALES --schema PUBLIC          # row counts + sizes

# Understand one table
python $S describe SALES.PUBLIC.ORDERS
python $S sample  SALES.PUBLIC.ORDERS -n 20
python $S profile SALES.PUBLIC.ORDERS                      # server-side df.describe()

# Analyze without downloading
python $S query "SELECT status, COUNT(*) n, AVG(total) FROM SALES.PUBLIC.ORDERS GROUP BY 1 ORDER BY n DESC"

# Materialize only what you need
python $S export SALES.PUBLIC.ORDERS --out orders_2026.csv --where "order_year = 2026"
python $S export SALES.PUBLIC.ORDERS --out sample.csv --sample 10000    # random 10k-row sample

# Load data in (e.g. synthetic data generated to a CSV)
python $S import synthetic_customers.csv SALES.PUBLIC.CUSTOMERS_SYN --mode replace

# Above-RAM data: stream in bounded batches (both directions)
python $S export SALES.PUBLIC.EVENTS --out events.csv --stream          # download, bounded memory
python $S import events.csv SALES.PUBLIC.EVENTS_COPY --stream --mode replace   # upload, bounded memory
```

Add `--format json` (or `csv`) to any command for machine-readable output.

## Gotchas

- **Identifier case.** Snowflake folds unquoted names to upper case; tables written
  by tools like the Rockfish connector are often stored **quoted and lower-case**.
  The tool quotes identifiers it generates. In hand-written `query` SQL, quote
  case-sensitive columns yourself: `SELECT "amount" FROM ...`.
- **Qualify tables** as `DB.SCHEMA.TABLE`, or pass `--database`/`--schema`. A bare
  name only resolves if the connection has a database/schema context.
- **`query` is read-only** by design: it rejects anything that isn't a single
  `SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN`, and refuses multiple statements.
- **`ROW_COUNT`/`BYTES` are null for views** — the `export` size guard can't gauge
  a view, so it treats an unshrunk view export as needing `--force`.
- The script file is intentionally **not** named `snowflake.py`; that would shadow
  the `snowflake` package and break `import snowflake.connector`.

## Setup

```bash
pip install 'snowflake-connector-python[pandas]'
```

(`[pandas]` pulls in the Arrow fast-path used for fetching results.)
