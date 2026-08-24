"""Run a GenerateFromDataSchema workflow and verify the generated data.

The published docs (https://docs.rockfish.ai/sdk/actions-ent.html) and this
skill's `patterns.md` cover how to *build* schemas. This file covers the parts
they don't: running a workflow to completion, pulling the results back as
DataFrames, and asserting that the generated data actually holds the guarantees
the schema claims.

Three examples, each ending in real checks:
  1. Composite foreign key    -> referential integrity across a two-column key.
  2. Count-driven fan-out     -> child row counts, per-parent ordinals, and a
                                 parent total that matches its child rows.
  3. Timeseries expansion     -> the cardinality x ticks row-count contract and
                                 per-session structure.

Requires rockfish >= 0.79.0 and credentials from either a config file at
~/.config/rockfish/config.toml or the ROCKFISH_* environment variables.

Exits non-zero if any check fails, so it works as a smoke test.

Run:
    python entity-gen.py                      # run all three
    python entity-gen.py -e 2                 # run only example 2
    python entity-gen.py -e 1 -e 3            # run examples 1 and 3
    python entity-gen.py --connection env     # force ROCKFISH_* env vars
"""
import argparse
import asyncio
import os
import sys

# RoundParams, UniqueParams, GroupOrdinalParams, LogNormalDistParams and
# AggregateFromChildParams all landed in 0.79.0. Guard every rockfish import,
# not just the 0.79-era names: a missing package raises ModuleNotFoundError from
# the very first import, which would sail past a guard placed any later.
MIN_SDK = "0.79.0"

try:
    import rockfish as rf
    import rockfish.actions as ra
    from rockfish.actions.ent import (
        AggregateFromChildParams,
        CategoricalParams,
        Column,
        ColumnCategoryType,
        ColumnType,
        DataSchema,
        Derivation,
        DerivationFunctionType,
        Domain,
        DomainType,
        Entity,
        EntityRelationship,
        EntityRelationshipType,
        GlobalTimestamp,
        GroupOrdinalParams,
        IDParams,
        LogNormalDistParams,
        RoundParams,
        Timestamp,
        TimeseriesParams,
        UniformDistParams,
        UniqueParams,
    )
except ImportError as exc:  # ModuleNotFoundError is a subclass, so both land here
    import importlib.metadata as _md

    try:
        _have = _md.version("rockfish")
    except _md.PackageNotFoundError:
        _have = "not installed"
    verb = "needs" if _have == "not installed" else "needs at least"
    raise SystemExit(
        f"This example {verb} rockfish {MIN_SDK} (found: {_have}).\n"
        f"  {exc}\n"
        "Install or upgrade with:\n"
        "  uv pip install --find-links https://packages.rockfish.ai "
        "--upgrade 'rockfish[labs]'"
    ) from exc



async def run(conn: rf.Connection, schema: DataSchema) -> dict:
    """Generate a schema and return {entity_name: DataFrame}.

    This is the shape every caller needs: start the workflow, wait for it to
    finish (raising on failure rather than silently yielding nothing), then
    resolve each remote dataset to a local one and hand back pandas frames
    keyed by entity name. Datasets come back in no guaranteed order, so key
    them by `name()` rather than indexing with `nth()`.
    """
    generate = ra.GenerateFromDataSchema(
        ra.GenerateFromDataSchema.Config(schema=schema, upload_datasets=True)
    )

    builder = rf.WorkflowBuilder()
    builder.add(generate)  # add() returns None -- it is not chainable
    workflow = await builder.start(conn)
    print(f"Workflow ID: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    frames = {}
    for remote_ds in await workflow.datasets().collect():
        ds = await remote_ds.to_local(conn)
        frames[ds.name()] = ds.to_pandas()
        print(f"  {ds.name()}: {ds.table.num_rows} rows, {ds.table.column_names}")
    return frames


# Every failed check lands here so one run reports all of them, then exits
# non-zero. A silent exit 0 would make this file useless as a smoke test.
FAILURES: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    """Record and print a pass/fail line; failures set the process exit code."""
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{suffix}")
    if not passed:
        FAILURES.append(f"{label}{suffix}")


async def example_1_composite_fk(conn: rf.Connection) -> None:
    """Composite foreign key: two columns together form the reference.

    `trades` references `accounts` via (trade_broker_id, trade_account_number).
    Both FK columns are declared FOREIGN_KEY with no domain and no derivation --
    the relationship supplies their values as matching tuples, so referential
    integrity holds across the whole key rather than per column.
    """
    schema = DataSchema(
        entities=[
            Entity(
                name="accounts",
                cardinality=10,
                columns=[
                    Column(
                        name="broker_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=["BROKER_A", "BROKER_B", "BROKER_C"],
                            ),
                        ),
                    ),
                    Column(
                        name="account_number",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="ACC{id}"),
                        ),
                    ),
                ],
            ),
            Entity(
                name="trades",
                cardinality=30,
                columns=[
                    Column(
                        name="trade_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="TRD_{id}"),
                        ),
                    ),
                    # Composite FK: both halves declared, neither with a domain.
                    Column(
                        name="trade_broker_id",
                        data_type="string",
                        column_type=ColumnType.FOREIGN_KEY,
                        column_category_type=ColumnCategoryType.METADATA,
                    ),
                    Column(
                        name="trade_account_number",
                        data_type="string",
                        column_type=ColumnType.FOREIGN_KEY,
                        column_category_type=ColumnCategoryType.METADATA,
                    ),
                    Column(
                        name="price",
                        data_type="float64",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.UNIFORM_DIST,
                            params=UniformDistParams(lower=100.0, upper=500.0),
                        ),
                    ),
                ],
            ),
        ],
        entity_relationships=[
            EntityRelationship(
                parent_entity="accounts",
                child_entity="trades",
                relationship_type=EntityRelationshipType.ONE_TO_MANY,
                join_columns={
                    "broker_id": "trade_broker_id",
                    "account_number": "trade_account_number",
                },
            )
        ],
        seed=42,
    )

    frames = await run(conn, schema)
    accounts, trades = frames["accounts"], frames["trades"]
    print(trades.head())

    # The guarantee: every (broker, account) pair in the child exists in the
    # parent. Checking the columns separately would pass even if the generator
    # had mixed halves from different parent rows, so compare tuples.
    valid = set(zip(accounts["broker_id"], accounts["account_number"]))
    used = set(zip(trades["trade_broker_id"], trades["trade_account_number"]))
    orphans = used - valid
    check("every trade tuple exists in accounts", not orphans, f"{len(orphans)} orphans")
    check("row count matches cardinality", len(trades) == 30, f"{len(trades)} rows")


async def example_2_fanout(conn: rf.Connection) -> None:
    """Count-driven fan-out: one parent row explodes into many child rows.

    `orders.line_count` drives how many `line_items` rows each order gets, so
    `line_items.cardinality` is ignored entirely. GROUP_ORDINAL numbers the
    children within each parent, and AGGREGATE_FROM_CHILD rolls the child
    amounts back up, which makes the parent total agree with its children by
    construction rather than by coincidence.
    """
    schema = DataSchema(
        entities=[
            Entity(
                name="orders",
                cardinality=100,
                columns=[
                    Column(
                        name="order_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.ID,
                            params=IDParams(template_str="ORD_{id}"),
                        ),
                    ),
                    # Drives the child row count.
                    Column(
                        name="line_count",
                        data_type="int64",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=[1, 2, 3, 4, 5],
                                weights=[0.35, 0.3, 0.2, 0.1, 0.05],
                            ),
                        ),
                    ),
                    # Rolled up from the children. Takes no dependent_columns:
                    # its source is params.child_entity. Nothing else in this
                    # entity may depend on it -- it is filled in a post-pass.
                    Column(
                        name="order_total",
                        data_type="float64",
                        column_type=ColumnType.DERIVED,
                        column_category_type=ColumnCategoryType.METADATA,
                        derivation=Derivation(
                            function_type=DerivationFunctionType.AGGREGATE_FROM_CHILD,
                            dependent_columns=[],
                            params=AggregateFromChildParams(
                                child_entity="line_items",
                                operation="sum",
                                value_column="amount",
                            ),
                        ),
                    ),
                ],
            ),
            Entity(
                name="line_items",
                cardinality=1,  # ignored: the row count comes from the parent
                columns=[
                    Column(
                        name="order_id",
                        data_type="string",
                        column_type=ColumnType.FOREIGN_KEY,
                        column_category_type=ColumnCategoryType.METADATA,
                    ),
                    # 0-based index within each parent group.
                    Column(
                        name="line_no",
                        data_type="int64",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.GROUP_ORDINAL,
                            params=GroupOrdinalParams(start=0),
                        ),
                    ),
                    Column(
                        name="amount_raw",
                        data_type="float64",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.LOGNORMAL_DIST,
                            params=LogNormalDistParams(mu=3.4, sigma=0.7),
                        ),
                    ),
                    # Land money on real units. For exact cents, prefer
                    # data_type="decimal128(18, 2)" over float64.
                    Column(
                        name="amount",
                        data_type="float64",
                        column_type=ColumnType.DERIVED,
                        column_category_type=ColumnCategoryType.METADATA,
                        derivation=Derivation(
                            function_type=DerivationFunctionType.ROUND,
                            dependent_columns=["amount_raw"],
                            params=RoundParams(decimals=2),
                        ),
                    ),
                ],
            ),
        ],
        entity_relationships=[
            EntityRelationship(
                parent_entity="orders",
                child_entity="line_items",
                relationship_type=EntityRelationshipType.ONE_TO_MANY,
                join_columns={"order_id": "order_id"},
                child_count_column="line_count",
            )
        ],
        seed=7,
    )

    frames = await run(conn, schema)
    orders, lines = frames["orders"], frames["line_items"]
    print(lines.head(10))

    expected = int(orders["line_count"].sum())
    check(
        "child rows == sum(parent line_count)",
        expected == len(lines),
        f"expected {expected}, got {len(lines)}",
    )

    per_order = lines.groupby("order_id")["line_no"].agg(["count", "max"])
    joined = orders.set_index("order_id").join(per_order)
    check(
        "each order has exactly line_count children",
        bool((joined["count"] == joined["line_count"]).all()),
    )
    # max == count-1 is not enough: [0, 2, 2] would satisfy it while ordinal 1
    # is missing. Compare the full sorted ordinal list against range(count).
    exact = lines.groupby("order_id")["line_no"].apply(
        lambda g: sorted(g.tolist()) == list(range(len(g)))
    )
    check(
        "GROUP_ORDINAL is exactly 0..count-1 per order, no gaps or repeats",
        bool(exact.all()),
        f"{int((~exact).sum())} order(s) with bad ordinals",
    )

    sums = lines.groupby("order_id")["amount"].sum().round(2)
    drift = (joined["order_total"].round(2) - sums).abs().max()
    check(
        "order_total == sum of its line amounts",
        drift < 0.011,
        f"max drift {drift:.4f}",
    )
    # Tolerance, not exact equality: these are float64 round-tripped through
    # Arrow and pandas, so "already rounded" means within representation error.
    # data_type="decimal128(18, 2)" would make this exact.
    off_grid = (lines["amount"] - lines["amount"].round(2)).abs().max()
    check(
        "ROUND landed amounts on the 2-decimal grid",
        off_grid < 1e-9,
        f"max deviation {off_grid:.2e}",
    )


async def example_3_timeseries(conn: rf.Connection) -> None:
    """Timeseries expansion: the cardinality x ticks row-count contract.

    A timeseries-only entity expands densely -- one row per instance per tick.
    This is the sizing rule people most often get wrong, so it is worth
    verifying against the schema rather than eyeballing the output. The window
    below is 12 hours at 15min, which is 49 ticks (both ends inclusive), so 8
    devices produce 8 x 49 = 392 rows.

    The window deliberately straddles peak_start_hour (08:00) so both the peak
    and off-peak regimes appear in the output; a window entirely inside one
    regime would make the seasonality settings unobservable.

    Note `interval_minutes` on the TimeseriesParams must be kept in step with
    `global_timestamp.time_interval` by hand -- they are configured separately
    and a mismatch quietly distorts the seasonal shape.
    """
    cardinality, interval_minutes, window_hours = 8, 15, 12
    schema = DataSchema(
        entities=[
            Entity(
                name="devices",
                cardinality=cardinality,
                timestamp=Timestamp(column_name="timestamp"),
                columns=[
                    # UNIQUE guarantees distinct keys; ID would also work here.
                    Column(
                        name="device_id",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.UNIQUE,
                            params=UniqueParams(
                                format="template", template_str="DEV-{index:04d}"
                            ),
                        ),
                    ),
                    Column(
                        name="mac",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.UNIQUE,
                            params=UniqueParams(format="mac", mac_prefix="02"),
                        ),
                    ),
                    Column(
                        name="location",
                        data_type="string",
                        column_type=ColumnType.INDEPENDENT,
                        column_category_type=ColumnCategoryType.METADATA,
                        domain=Domain(
                            type=DomainType.CATEGORICAL,
                            params=CategoricalParams(
                                values=["dc-1", "dc-2", "dc-3"],
                            ),
                        ),
                    ),
                    Column(
                        name="cpu_pct",
                        data_type="float64",
                        column_type=ColumnType.STATEFUL,
                        column_category_type=ColumnCategoryType.MEASUREMENT,
                        domain=Domain(
                            type=DomainType.TIMESERIES,
                            params=TimeseriesParams(
                                base_value=50.0,
                                min_value=10.0,
                                max_value=95.0,
                                seasonality_type="peak_offpeak",
                                peak_start_hour=8,
                                peak_end_hour=22,
                                seasonality_strength=0.4,
                                noise_level=0.15,
                                spike_probability=0.05,
                                spike_magnitude=0.3,
                                interval_minutes=interval_minutes,
                            ),
                        ),
                    ),
                ],
            )
        ],
        global_timestamp=GlobalTimestamp(
            t_start="2025-01-01T00:00:00Z",
            t_end="2025-01-01T12:00:00Z",
            time_interval=f"{interval_minutes}min",
        ),
        seed=42,
    )

    frames = await run(conn, schema)
    devices = frames["devices"]
    print(devices.head())

    ticks = (window_hours * 60) // interval_minutes + 1  # both ends inclusive
    check(
        "rows == cardinality x ticks",
        len(devices) == cardinality * ticks,
        f"expected {cardinality} x {ticks} = {cardinality * ticks}, got {len(devices)}",
    )
    check(
        "one row per device per tick",
        bool((devices.groupby("device_id").size() == ticks).all()),
    )
    check(
        "metadata is constant within each session",
        bool((devices.groupby("device_id")["location"].nunique() == 1).all()),
    )
    check(
        "measurements respect min/max clipping",
        bool(devices["cpu_pct"].between(10.0, 95.0).all()),
        f"range {devices['cpu_pct'].min():.1f}-{devices['cpu_pct'].max():.1f}",
    )
    check(
        "timestamps are timezone-aware UTC",
        str(devices["timestamp"].dt.tz) in ("UTC", "+00:00"),
        f"tz={devices['timestamp'].dt.tz}",
    )

    # peak_offpeak seasonality should lift the mean inside the peak window.
    # Distributional rather than exact, so the bar is deliberately just
    # "strictly higher" -- with ~200 samples per regime and a 0.4 strength
    # step, the two means separate comfortably.
    hours = devices["timestamp"].dt.hour
    peak = devices[hours >= 8]["cpu_pct"]
    offpeak = devices[hours < 8]["cpu_pct"]
    check(
        "both regimes present in the window",
        len(peak) > 0 and len(offpeak) > 0,
        f"{len(peak)} peak rows, {len(offpeak)} off-peak rows",
    )
    if len(peak) and len(offpeak):
        check(
            "peak-hour mean exceeds off-peak mean",
            peak.mean() > offpeak.mean(),
            f"peak {peak.mean():.1f} vs off-peak {offpeak.mean():.1f}",
        )


EXAMPLES = {
    1: ("Composite foreign key and referential integrity", example_1_composite_fk),
    2: ("Count-driven fan-out, ordinals, and roll-up", example_2_fanout),
    3: ("Timeseries expansion and row-count contract", example_3_timeseries),
}


def connect(mode: str) -> rf.Connection:
    """Open a connection the way the caller asked for.

    `from_config()` reads ~/.config/rockfish/config.toml; `from_env()` reads
    ROCKFISH_API_KEY (plus the optional ROCKFISH_API_URL / ROCKFISH_PROJECT_ID /
    ROCKFISH_ORGANIZATION_ID). "auto" prefers env when an API key is exported,
    since that is the usual CI / container setup, and otherwise falls back to
    the config file.
    """
    if mode == "config":
        return rf.Connection.from_config()
    if mode == "env":
        return rf.Connection.from_env()
    if os.environ.get("ROCKFISH_API_KEY"):
        return rf.Connection.from_env()
    return rf.Connection.from_config()


async def main(example_numbers: list[int], connection: str = "auto") -> None:
    async with connect(connection) as conn:
        for n in example_numbers:
            title, func = EXAMPLES[n]
            banner = f" Example {n}: {title} "
            print(f"\n{'=' * 72}\n{banner.center(72, '=')}\n{'=' * 72}\n")
            await func(conn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and verify GenerateFromDataSchema workflows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-e", "--example",
        type=int,
        action="append",
        choices=sorted(EXAMPLES),
        help="Run a specific example (1-3). Repeatable. Default: run all three.",
    )
    parser.add_argument(
        "--connection",
        choices=("auto", "config", "env"),
        default="auto",
        help="Credential source: 'config' for ~/.config/rockfish/config.toml, "
             "'env' for ROCKFISH_* variables, 'auto' (default) to prefer env "
             "when ROCKFISH_API_KEY is set.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.example or list(EXAMPLES), args.connection))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")
