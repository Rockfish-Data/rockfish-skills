"""Blend two or more time-series datasets into one Rockfish dataset.

Reference implementation for the `blend-data` skill. Every input is inspected
and checked with server-side SQL (no full download), aligned to a common
schema, given globally unique session keys, blended, sorted by time, and
verified.

Two blend modes:

  dag    (default) one branch per input, all feeding one DatasetSave:
             load_i -> SQL(align_i) [-> Sample_i] --+
                                                    +-> DatasetSave(concat_tables=True)
         DatasetSave appends tables in arrival order, so a second workflow
         sorts the result. Leaves the unsorted blend behind as an intermediate.
  union  one SQL action: UNION ALL of every aligned input, ORDER BY time.
         One workflow, sorted, no intermediate, but the whole blend is
         materialized inside one action. No per-source sampling.

Usage:
    python blend.py --dataset 401XMCUqMDpF7Uu0mLnhQE --dataset 3nmJkuoPr7Vxv3OAxvixRD \\
        --time-field ts --session-field job --tags real,syn --name jobs-blended
    python blend.py ... --sessions 100,100 --seed 7    # cap sessions per source
    python blend.py ... --mode union
    python blend.py ... --dry-run                       # validate + print SQL only
"""

import argparse
import asyncio
import json
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc

import rockfish as rf
import rockfish.actions as ra

SESSION_KEY = "session_key"  # int64 key DatasetSave offsets on concat


@dataclass
class Source:
    dataset_id: str
    name: str
    tag: str
    schema: pa.Schema
    rows: int
    sessions: int
    session_basis: str  # field that identifies a session in *this* input


def connect(profile: str | None):
    if profile == "env":
        return rf.Connection.from_env()
    return rf.Connection.from_config(profile) if profile else rf.Connection.from_config()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalize(t: pa.DataType) -> pa.DataType:
    # string / large_string are one logical type; parquet round trips flip them.
    if t == pa.large_string():
        return pa.string()
    if t == pa.large_binary():
        return pa.binary()
    return t


async def inspect_source(conn, dataset_id: str, tag: str, session_field: str) -> Source:
    ds = await conn.get_dataset(dataset_id)
    head = (await ds.sql("SELECT * FROM my_table LIMIT 0", conn=conn)).table
    meta = json.loads((head.schema.metadata or {}).get(b"source_metadata", b"{}"))
    # Generated datasets carry a session_field (e.g. session_key) that can be
    # finer than the entity id: synthetic `job` values repeat across sessions.
    basis = meta.get("session_field") or session_field
    if basis not in head.column_names:
        basis = session_field
    if basis not in head.column_names:
        raise SystemExit(f"{dataset_id}: session field {basis!r} not in {head.column_names}")
    counts = (await ds.sql(
        f"SELECT COUNT(*) AS n, COUNT(DISTINCT {quote(basis)}) AS s FROM my_table", conn=conn
    )).table.to_pylist()[0]
    return Source(dataset_id, ds.name(), tag, head.schema, counts["n"], counts["s"], basis)


def validate(sources: list[Source], time_field: str, session_field: str, on_extra: str) -> list[pa.Field]:
    """Return the common fields (in first-source order) or exit on a conflict."""
    base = sources[0].schema
    problems, extras = [], []
    common = []
    for f in base:
        if f.name == SESSION_KEY:
            continue  # regenerated below
        types = {s.tag: normalize(s.schema.field(f.name).type)
                 for s in sources if f.name in s.schema.names}
        if len(types) < len(sources):
            extras.append(f"{f.name} (only in {', '.join(types)})")
        elif len(set(types.values())) > 1:
            problems.append(f"{f.name}: " + ", ".join(f"{k}={v}" for k, v in types.items()))
        else:
            common.append(pa.field(f.name, normalize(f.type)))
    for s in sources[1:]:
        for name in s.schema.names:
            if name not in base.names and name != SESSION_KEY:
                extras.append(f"{name} (only in {s.tag})")
    names = [f.name for f in common]
    for required in (time_field, session_field):
        if required not in names:
            problems.append(f"{required!r} must be present, with one type, in every input")
    if not pa.types.is_timestamp(base.field(time_field).type) and time_field in names:
        problems.append(f"{time_field!r} is {base.field(time_field).type}, not a timestamp")

    for e in sorted(set(extras)):
        print(f"  extra field: {e}")
    for p in problems:
        print(f"  CONFLICT {p}")
    if problems:
        raise SystemExit("schema conflicts; fix them in data prep (SQL CAST / CoerceDtypes / rename) first")
    if extras and on_extra == "fail":
        raise SystemExit("inputs have non-shared fields and --on-extra=fail")
    return common


def select_list(common: list[pa.Field], src: Source, session_field: str,
                source_field: str | None, namespace: bool) -> list[str]:
    cols = []
    for f in common:
        if f.name == session_field and namespace and pa.types.is_string(f.type):
            # keep entity ids from different sources distinct: real J00137 != synthetic J00137
            cols.append(f"'{src.tag}-' || {quote(f.name)} AS {quote(f.name)}")
        else:
            cols.append(quote(f.name))
    if source_field:
        cols.append(f"'{src.tag}' AS {quote(source_field)}")
    return cols


def align_query(common, src, session_field, source_field, namespace) -> str:
    cols = select_list(common, src, session_field, source_field, namespace)
    # Every branch emits the same int64 key, so DatasetSave can offset it on append.
    cols.append(f"CAST(DENSE_RANK() OVER (ORDER BY {quote(src.session_basis)}) - 1 AS BIGINT) "
                f"AS {SESSION_KEY}")
    return f"SELECT {', '.join(cols)} FROM my_table"


def union_query(common, sources, session_field, source_field, namespace, time_field) -> str:
    parts = []
    for i, src in enumerate(sources):
        cols = select_list(common, src, session_field, source_field, namespace)
        cols += [f"{i} AS _blend_src", f"CAST({quote(src.session_basis)} AS VARCHAR) AS _blend_basis"]
        parts.append(f"SELECT {', '.join(cols)} FROM t{i}")
    out = [quote(f.name) for f in common] + ([quote(source_field)] if source_field else [])
    return (
        f"SELECT {', '.join(out)}, "
        f"CAST(DENSE_RANK() OVER (ORDER BY _blend_src, _blend_basis) - 1 AS BIGINT) AS {SESSION_KEY} "
        f"FROM ({' UNION ALL '.join(parts)}) "
        f"ORDER BY {quote(time_field)}, {SESSION_KEY}"
    )


async def run(conn, builder, label):
    wf = await builder.start(conn)
    print(f"  [{label}] workflow {wf.id()}")
    await wf.wait(raise_on_failure=False)
    status = await wf.status()
    if status != "completed":
        async for log in wf.logs():
            if "ERROR" in str(log):
                print(f"  [{label}] {log}")
        raise SystemExit(f"{label} workflow {wf.id()} {status}")
    return await wf.datasets().last()


async def verify(conn, ds, sources, caps, time_field, source_field):
    counts = (await ds.sql(
        f"SELECT COUNT(*) AS n, COUNT(DISTINCT {SESSION_KEY}) AS s FROM my_table", conn=conn
    )).table.to_pylist()[0]
    want_sessions = sum(min(s.sessions, c) if c else s.sessions for s, c in zip(sources, caps))
    ok = counts["s"] == want_sessions
    if not any(caps):
        want_rows = sum(s.rows for s in sources)
        ok &= counts["n"] == want_rows
        print(f"  rows {counts['n']} (expected {want_rows})")
    else:
        print(f"  rows {counts['n']} (sampled)")
    print(f"  sessions {counts['s']} (expected {want_sessions})")
    if source_field:
        mixed = (await ds.sql(
            f"SELECT COUNT(*) AS k FROM (SELECT {SESSION_KEY} FROM my_table GROUP BY {SESSION_KEY} "
            f"HAVING COUNT(DISTINCT {quote(source_field)}) > 1) x", conn=conn
        )).table.to_pylist()[0]["k"]
        ok &= mixed == 0
        print(f"  session keys spanning more than one source: {mixed}")
        by_src = (await ds.sql(
            f"SELECT {quote(source_field)} AS src, COUNT(*) AS n, COUNT(DISTINCT {SESSION_KEY}) AS s "
            f"FROM my_table GROUP BY 1 ORDER BY 1", conn=conn
        )).table.to_pylist()
        for r in by_src:
            print(f"    {r['src']}: {r['n']} rows, {r['s']} sessions")
    # Pulls only the time column — file order is the order a reader sees.
    ts = (await ds.sql(f"SELECT {quote(time_field)} FROM my_table", conn=conn)).table[0]
    ordered = len(ts) < 2 or pc.all(pc.greater_equal(ts[1:], ts[:-1])).as_py()
    ok &= ordered
    print(f"  ordered by {time_field}: {ordered}")
    return ok


async def main(args):
    tags = args.tags.split(",") if args.tags else [f"s{i}" for i in range(len(args.dataset))]
    caps = [int(c) if c else 0 for c in args.sessions.split(",")] if args.sessions else [0] * len(args.dataset)
    if len(tags) != len(args.dataset) or len(caps) != len(args.dataset):
        raise SystemExit("--tags and --sessions need one entry per --dataset")
    if args.mode == "union" and any(caps):
        raise SystemExit("--sessions needs --mode dag (Sample runs per branch)")
    source_field = None if args.source_field == "" else args.source_field

    async with connect(args.profile) as conn:
        print("1. inspect")
        sources = [await inspect_source(conn, d, t, args.session_field) for d, t in zip(args.dataset, tags)]
        for s in sources:
            print(f"  {s.tag}: {s.name} ({s.dataset_id}) {s.rows} rows, "
                  f"{s.sessions} sessions by {s.session_basis!r}")

        print("2. validate")
        common = validate(sources, args.time_field, args.session_field, args.on_extra)
        print(f"  common fields: {[f.name for f in common]}")

        namespace = not args.no_namespace
        if args.mode == "union":
            query = union_query(common, sources, args.session_field, source_field, namespace, args.time_field)
            print(f"3. blend (union)\n  {query}")
            if args.dry_run:
                return
            builder = rf.WorkflowBuilder()
            builder.add_path(
                ra.DatasetLoad(dataset_id=sources[0].dataset_id),
                ra.SQL(query=query, table_name="t0",
                       dataset_name_to_id={f"t{i}": s.dataset_id for i, s in enumerate(sources) if i}),
                ra.DatasetSave(name=args.name),
            )
            final = await run(conn, builder, "blend")
        else:
            print("3. blend (dag)")
            builder = rf.WorkflowBuilder()
            tails = []
            for src, cap in zip(sources, caps):
                query = align_query(common, src, args.session_field, source_field, namespace)
                print(f"  {src.tag}: {query}")
                path = [ra.DatasetLoad(dataset_id=src.dataset_id), ra.SQL(query=query)]
                if cap:
                    path.append(ra.Sample(session_key=SESSION_KEY, sample_size=cap,
                                          sample_type="random", seed=args.seed))
                builder.add_path(*path)
                tails.append(path[-1])
            if args.dry_run:
                return
            builder.add_action(
                ra.DatasetSave(name=f"{args.name}-unsorted", concat_tables=True,
                               concat_session_key=SESSION_KEY),
                parents=tails,
            )
            unsorted = await run(conn, builder, "blend")
            print(f"  intermediate {unsorted.id}")

            print("4. sort")
            builder = rf.WorkflowBuilder()
            builder.add_path(
                unsorted,
                ra.SQL(query=f"SELECT * FROM my_table ORDER BY {quote(args.time_field)}, {SESSION_KEY}"),
                ra.DatasetSave(name=args.name),
            )
            final = await run(conn, builder, "sort")

        print(f"5. verify {final.id}")
        if not await verify(conn, final, sources, caps, args.time_field, source_field):
            raise SystemExit("verification failed")
        print(f"blended dataset: {final.id}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", action="append", required=True, help="input dataset id (repeat, 2+)")
    p.add_argument("--time-field", required=True, help="timestamp field to order by")
    p.add_argument("--session-field", required=True, help="entity id field, e.g. job / device_id")
    p.add_argument("--name", default="blended", help="name of the output dataset")
    p.add_argument("--tags", help="comma-separated short tag per input (default s0,s1,...)")
    p.add_argument("--source-field", default="blend_source",
                   help="provenance column holding each row's tag; '' to omit")
    p.add_argument("--no-namespace", action="store_true",
                   help="don't prefix entity ids with the tag (only if ids are already disjoint)")
    p.add_argument("--sessions", help="comma-separated per-input session cap, 0/blank = all (dag mode)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--on-extra", choices=["drop", "fail"], default="drop",
                   help="fields not shared by every input: drop them or stop")
    p.add_argument("--mode", choices=["dag", "union"], default="dag")
    p.add_argument("--profile", help="config.toml profile, or 'env' for ROCKFISH_* variables")
    p.add_argument("--dry-run", action="store_true", help="inspect, validate, print SQL; start nothing")
    args = p.parse_args()
    if len(args.dataset) < 2:
        p.error("need at least two --dataset")
    asyncio.run(main(args))
