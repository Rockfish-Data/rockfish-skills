---
name: generate-from-schema
description: Generate synthetic datasets from a schema specification using the Rockfish SDK. Use when a user wants to create synthetic tabular or time-series data with specific structure — independent, stateful, or derived columns, state machines, timeseries with seasonality, entity relationships (foreign keys, composite keys, parent/child fan-out, self-references), or realistic PII-like values (names, emails, addresses, SSNs) via NamedEntityProvider. Also generates OpenTelemetry telemetry from a service call graph — distributed-trace span trees with correlated RED metrics and logs derived from the same spans. Trigger on phrases like "generate synthetic data", "fake data from a schema", "create a test dataset", "GenerateFromDataSchema", "OpenTelemetry / span / trace / call-graph data", or mentions of entity/foreign-key/state-machine data.
---

# Generate from schema

Use `rockfish.actions.GenerateFromDataSchema` to produce synthetic datasets from a declarative schema. One `DataSchema` yields one dataset (PyArrow table) per `Entity`, with referential integrity, temporal patterns, and reproducible output.

## When to use this skill

Use when the user wants synthetic tabular or time-series data with:

- Specific column shapes — IDs, globally-unique keys, categoricals, statistical distributions, mixtures.
- Time-varying measurements (timeseries with seasonality/noise/spikes) or behavioral sequences (state machines).
- Computed columns — arithmetic, value mapping, running totals, cross-entity roll-ups, string templates.
- Cross-entity relationships — foreign keys, composite keys, count-driven fan-out, whales, hierarchies.
- Realistic PII-like values (names, emails, addresses, SSNs, cards) that carry no real customer data.
- OpenTelemetry telemetry — distributed-trace span trees from a service call graph, with correlated RED metrics and logs. See [OpenTelemetry span trees](#opentelemetry-span-trees).

Use a different skill when:

- The user **already has real data** and wants a synthetic stand-in that reproduces its distributions — `generate-from-data` trains a model on it instead of inventing rows from a description.
- The user wants to inject *incidents* (spikes, outages, ramps, sustained shifts) into an existing time-series dataset — `inject-incidents`.

## Concept

A schema is a tree. Every generated column is one of four **column types**, and each column is also either **metadata** (constant per entity instance) or **measurement** (varies per timestamp):

```
DataSchema(seed, scale_factor)
├── entities: list[Entity]            # name, cardinality, scale_with_factor
│   ├── columns: list[Column]         # name, data_type, column_type, column_category_type
│   │   ├── column_type = independent → domain=Domain(...)      # metadata only
│   │   ├── column_type = stateful    → domain=TIMESERIES|STATE_MACHINE  # measurement only
│   │   ├── column_type = derived     → derivation=Derivation(...)
│   │   └── column_type = foreign_key → no domain, no derivation  # metadata only
│   ├── timestamp: Timestamp          # required iff the entity has measurement columns
│   └── span_tree | span_metrics | span_logs  # optional: a dedicated OpenTelemetry
│                                             # generator produces this entity's rows
│                                             # instead of the column pipeline (see below)
├── entity_relationships: list[EntityRelationship]
└── global_timestamp: GlobalTimestamp  # required iff any entity has a timestamp
```

Generation order is automatic: independent → foreign keys → stateful → derived (in dependency order). A `span_tree`/`span_metrics`/`span_logs` entity is produced whole by its generator instead, then joins the others through relationships like any table.

## How to use

1. Decide the data model first — **tabular** (all metadata, no timestamp) or **time-series** (metadata + measurement + timestamp). See [`reference/data-models.md`](reference/data-models.md).
2. Build the `DataSchema` from typed dataclasses (validated at construction — prefer these) or an equivalent JSON dict.
3. Wrap it in `ra.GenerateFromDataSchema.Config(schema=..., upload_datasets=True)`.
4. Add it to a `WorkflowBuilder` and start it, then `await workflow.wait(raise_on_failure=True)`. Note `builder.add()` returns `None`, so it cannot be chained.
5. Pull results back with `workflow.datasets()`.

```python
import asyncio

import rockfish as rf
import rockfish.actions as ra
from rockfish.actions.ent import DataSchema, Entity, Column, ColumnType, ColumnCategoryType, Domain, DomainType, IDParams

schema = DataSchema(
    entities=[
        Entity(
            name="users",
            cardinality=50,
            columns=[
                Column(
                    name="user_id",
                    data_type="string",
                    column_type=ColumnType.INDEPENDENT,
                    column_category_type=ColumnCategoryType.METADATA,
                    domain=Domain(type=DomainType.ID, params=IDParams(template_str="USER_{id}")),
                ),
            ],
        )
    ],
    seed=42,  # makes the whole run reproducible
)

generate = ra.GenerateFromDataSchema(
    ra.GenerateFromDataSchema.Config(schema=schema, upload_datasets=True)
)

async def main():
    async with rf.Connection.from_config() as conn:
        builder = rf.WorkflowBuilder()
        builder.add(generate)          # add() returns None -- not chainable
        workflow = await builder.start(conn)
        await workflow.wait(raise_on_failure=True)
        for remote in await workflow.datasets().collect():
            ds = await remote.to_local(conn)
            print(ds.name(), ds.table.num_rows)


asyncio.run(main())
```

`Connection.from_config()` reads `~/.config/rockfish/config.toml`; `Connection.from_env()` reads `ROCKFISH_API_KEY` / `ROCKFISH_API_URL` / `ROCKFISH_PROJECT_ID` / `ROCKFISH_ORGANIZATION_ID`. The `async with` closes the HTTP session cleanly.

## Picking a domain

`domain` says how an independent or stateful column's values are produced.

| Need | Domain |
| --- | --- |
| Templated primary key tied to row position | `ID` — `IDParams(template_str="USER_{id}")` |
| Plain counter | `SEQUENTIAL_INT` |
| Guaranteed-unique key, MAC address, or IP | `UNIQUE` — `format` is `template`, `mac`, or `ipv4` |
| Per-parent child index (0, 1, 2 … within each parent) | `GROUP_ORDINAL` — count-driven children only |
| Small fixed set of choices, optionally weighted | `CATEGORICAL` |
| Realistic names, emails, addresses, SSNs, cards | `NAMED_ENTITY_PROVIDER` |
| Bounded numeric range | `UNIFORM_DIST` |
| Symmetric numeric quantity | `NORMAL_DIST` |
| Right-skewed quantity spanning orders of magnitude (latency, income, size) | `LOGNORMAL_DIST` |
| Waiting time between events | `EXPONENTIAL_DIST` |
| Multimodal column (e.g. point mass at 0 + heavy tail) | `MIXTURE` — blends other domains |
| Variable-length token sequence rendered as a string | `SEQUENCE` |
| Time-varying measurement with seasonality/noise/spikes | `TIMESERIES` — stateful only |
| Behavioral state progression | `STATE_MACHINE` — stateful only |

## Picking a derivation

`derivation` says how a derived column is computed from other columns.

| Need | Derivation |
| --- | --- |
| Total or product of numeric columns | `SUM`, `MULTIPLY` |
| Round to fixed decimals (money to cents) | `ROUND` |
| Foreign key sampled from another entity's column | `SAMPLE_FROM_COLUMN` (the only derivation allowed to reference `entity.column`) |
| Correlated FK restricted to rows matching a local value | `SAMPLE_FROM_COLUMN_WHERE` |
| Distribution that varies by another column's category | `CONDITIONAL_SAMPLE` |
| Value lookup / recode | `MAP_VALUES` |
| Row-aligned alias of one column | `COPY` |
| Running total/peak/floor within a session | `CUMULATIVE` — measurement column on a timestamped entity |
| Count or sum of a child entity rolled onto the parent | `AGGREGATE_FROM_CHILD` — takes no `dependent_columns` |
| Timestamp to epoch seconds/ms or a strftime string | `FORMAT_TIMESTAMP` |
| Timestamp plus a per-row offset column | `SHIFT_TIMESTAMP` |
| Composed string from several columns | `STRING_TEMPLATE` |
| Fixed-position substring | `SUBSTRING` |
| Luhn check digit appended to an identifier | `LUHN_APPEND` |

## OpenTelemetry span trees

Beyond the column pipeline, an entity can be produced by a **dedicated telemetry generator**. You describe a service **call graph** once, and Rockfish emits a correlated OpenTelemetry signal set — distributed-trace spans, per-service RED metrics, and logs — reconciled by construction because the metrics and logs are derived from the same spans. Needs **rockfish ≥ 0.82.2**. Everything below is importable from `rockfish.actions.ent`.

Three generators attach to an `Entity`. Each **replaces** the column pipeline for that entity (the generator owns every value) and reads from another entity:

| Entity field | Produces | Reads from |
| --- | --- | --- |
| `span_tree=SpanTreeParams(...)` | one span tree per driver row: nested SERVER/CLIENT/INTERNAL spans, db/cache/queue leaves, async CONSUMER traces, with nested timings and propagated errors | a plain "trace" entity (one row per request) |
| `span_metrics=SpanMetricsParams(...)` | per-service RED metric points in time buckets: calls, errors, latency p50/p95/p99, and an OTLP explicit-bucket histogram | a `span_tree` entity |
| `span_logs=SpanLogsParams(...)` | INFO log per inbound span, ERROR per failed span, each carrying `trace_id`/`span_id` | a `span_tree` entity |

The call graph the span-tree generator walks:

| Class | What it is |
| --- | --- |
| `CallGraph(entry, services, calls, consumers=…, content=…, …)` | the topology, rooted at the `entry` service that receives requests |
| `SpanService(name, self_latency_mu, self_latency_sigma, error_rate, …)` | a node: a service and its own-time latency (lognormal log-ms) and base error rate |
| `SpanCall(caller, callee, kind, probability, max_fanout, protocol)` | a directed edge; `kind` ∈ `server` (nested SERVER span) / `db` / `cache` (leaf CLIENT) / `queue` (leaf PRODUCER) |
| `SpanConsumer(service, source, …)` | an async consumer of a `queue` topic; each publish spawns a new linked CONSUMER trace |
| `SpanContent(root_routes, datastore_tables, rpc_namespace, …)` | the request/operation vocabulary the generator names spans from — data, not logic, so the engine stays domain-neutral |
| `SpanIncident(service, start_unix_nano, end_unix_nano, latency_mult, error_rate)` | a time-bounded degradation planted in the traces — the signature an SRE triages |
| `ArrivalPattern(shape, amplitude, cycles, burstiness, autocorrelation, …)` | how trace start times spread across the window: `flat`/`wave`/`custom` rate plus autocorrelated bursts, so arrivals look like real traffic instead of a flat Poisson rate |

**Fixed output columns (the rule that trips people up).** Each generator owns an exact output column set, and the entity must declare *exactly* those columns — no more, no fewer — each as `METADATA`, typed `int64` for the nanosecond/count fields and `string` otherwise. The declared `domain` is never executed; it is a placeholder the generator overwrites. A `span_tree` entity, for example, must declare exactly `span_id, trace_id, parent_span_id, service_name, span_kind, span_name, start_time_unix_nano, end_time_unix_nano, duration_ns, status_code, attributes, resource_attributes, event_count, linked_trace_id, linked_span_id`. Use the `SpanTreeParams.OUTPUT_COLUMNS` / `SpanMetricsParams.OUTPUT_COLUMNS` / `SpanLogsParams.OUTPUT_COLUMNS` tuples to build the column list rather than hand-listing them.

The full field-level reference is in [`reference/schema-reference.md`](reference/schema-reference.md#opentelemetry-span-trees); a complete worked schema (the OpenTelemetry Astronomy Shop, five correlated tables) is in [`reference/patterns.md`](reference/patterns.md#walkthrough-opentelemetry-from-a-call-graph).

## Rules that cause most validation failures

- **Timestamp is all-or-nothing.** An entity has a `timestamp` **if and only if** it has at least one measurement column. If any entity has a timestamp, `DataSchema.global_timestamp` is required.
- **Category constrains type.** Independent and foreign-key columns must be `METADATA`; stateful columns must be `MEASUREMENT`; derived columns may be either.
- **Metadata cannot depend on measurement.** Metadata is generated once per entity instance, before timestamp expansion. The reverse (measurement depending on metadata) is fine.
- **Only `SAMPLE_FROM_COLUMN` crosses entities** in `dependent_columns`. Everything else must reference same-entity columns.
- **`FOREIGN_KEY` marks provenance, not a key.** It means the column carries no domain or derivation because a relationship supplies its values. Every such column must be named by some relationship where its entity is the child — either in `join_columns` (it is part of the key) or in `inherit_columns` (it is a denormalized copy from the joined parent row). Being `FOREIGN_KEY` does not make a column part of the key, so never move an inherited column into `join_columns` to satisfy this rule.
- **State machines create implicit columns.** `trigger_column_name` and every `context_variables` key become real columns; their names must not collide with any other column (including the timestamp).
- **`CategoricalParams.weights` must sum to 1.0** (unlike transition and mixture weights, which are normalized for you).
- **A span generator entity must declare the generator's exact output columns** — the exact set, all `METADATA`, no derivations, with `int64` on the nanosecond/count fields and `string` elsewhere. A `SpanIncident` must target a service that actually emits spans (reachable from a trace root within `max_depth`) and must change something (`latency_mult != 1.0` or an `error_rate` different from the service baseline); a `SpanConsumer.source` must be a `queue` topic some reachable producer publishes to. Each of these is validated at construction.

## Reference

Read these when you need detail beyond the tables above:

- [`reference/schema-reference.md`](reference/schema-reference.md) — every class and field with types, defaults, and validation rules.
- [`reference/data-models.md`](reference/data-models.md) — time-series vs tabular, and how metadata/measurement/timestamp/session key map onto `column_category_type`.
- [`reference/patterns.md`](reference/patterns.md) — worked recipes (fan-out, whales, affinity, hierarchies, running counters, money, correlated FKs), row-count math, scaling, and a telecom RAN walkthrough.
- [`reference/entity-gen.py`](reference/entity-gen.py) — runnable end-to-end examples with output validation.

## Gotchas

- **Cardinality is instances, not rows.** For a **timeseries** entity, rows = `cardinality × ticks`, where ticks span `[t_start, t_end]` at `time_interval` (inclusive of both ends). 100 cells over 2 days at `15min` = 100 × 193 = 19,300 rows. This formula does **not** hold for state-machine entities (next bullet), and a count-driven child ignores `cardinality` entirely.
- **`TimeseriesParams.interval_minutes` should match `global_timestamp.time_interval`** — they are configured separately and a mismatch distorts the seasonal shape.
- **Timeseries entities expand densely; state-machine entities do not.** A timeseries-only entity emits one row per instance per tick. A state-machine entity places each session at a random start tick and walks consecutive ticks until a terminal state, so its rows ≈ `cardinality × mean walk length` — independent of window width. Sizing one with `cardinality × ticks` can overestimate by one or two orders of magnitude.
- **Set `seed` for anything reproducible.** Without it the server draws a fresh seed, logs it, and stamps it on each uploaded dataset as an `ent_seed` label — so a run can be reproduced after the fact.
- **`scale_factor` grows fact entities only.** Set `Entity.scale_with_factor=False` on reference/dimension entities so catalogs stay fixed as the run scales.
- **Use `decimal128(18, 2)` for money**, not `float64`, so amounts stay exact to the cent.
- **Timestamps come back as ISO 8601 *strings*, not Arrow timestamps.** Values are UTC with an explicit `+00:00` offset (`'2025-01-01T00:00:00+00:00'`) whatever style `t_start`/`t_end` were written in — but the column's Arrow type is `string`, and `Timestamp(data_type=...)` does not change that. Parse before any `.dt` use: `pd.to_datetime(df["timestamp"], utc=True)`.
- **`NamedEntityProvider` uniqueness is best-effort**: pass `unique_values=N, with_replacement=False`, and note that replacement is force-enabled if rows exceed the pool.
- **A span-tree entity's row count is driven by its trace entity, not `cardinality`.** One span tree per `trace_entity` row, and each tree is many spans (a request that touches N services yields on the order of 2N+ spans), so rows ≈ `trace rows × mean spans per trace`. Set the span entity's own `cardinality=1`; it is ignored. `span_metrics` rows ≈ `services × time buckets`; `span_logs` rows ≈ inbound spans + failed spans.
- **This skill targets rockfish 0.82.2** (0.79.0 covers everything except the OpenTelemetry span-tree classes, which need 0.82.2). On an older SDK the newer names (`scale_factor`, the advanced `EntityRelationship` fields, `ROUND`, `SUBSTRING`, `LUHN_APPEND`, `SHIFT_TIMESTAMP`, `SAMPLE_FROM_COLUMN_WHERE`, and the `CallGraph` / `Span*Params` / `ArrivalPattern` telemetry classes) fail at *import*, not at generation. See [Requirements](reference/schema-reference.md#requirements).
