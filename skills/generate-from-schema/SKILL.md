---
name: generate-from-schema
description: Generate synthetic datasets from a schema specification using the Rockfish SDK. Use when a user wants to create synthetic tabular or time-series data with specific structure — independent or derived columns, state machines, timeseries, entity relationships (including composite foreign keys), or realistic PII-like values (names, emails, addresses, SSNs) via NamedEntityProvider. Trigger on phrases like "generate synthetic data", "fake data from a schema", "create a test dataset", "GenerateFromDataSchema", or mentions of entity/foreign-key/state-machine data.
---

# Generate from schema

Use `rockfish.actions.GenerateFromDataSchema` to produce synthetic datasets from a schema specification.

## When to use this skill

Use when the user wants to generate synthetic tabular or time-series data with:

- Specific column types (IDs, categoricals, numeric distributions).
- Derived columns (computed from other columns — e.g. mapping or sampling).
- Stateful behavior (state machines or timeseries).
- Cross-entity relationships (foreign keys, including composite keys).
- Realistic PII-like values (names, emails, addresses, SSNs).

If the user wants to inject *scenarios* (spikes, outages, ramps, shifts) into an existing time-series dataset, use the `inject-scenarios` skill instead.

## Concept

`rockfish.actions.GenerateFromDataSchema` takes a `DataSchema` and produces one synthetic dataset per `Entity`. A schema is a tree:

```
DataSchema
├── entities: list[Entity]
│   ├── name, cardinality
│   ├── columns: list[Column]
│   │   ├── name, data_type
│   │   ├── column_type  (independent | derived | stateful | foreign_key)
│   │   └── domain       (id | categorical | uniform_dist | state_machine
│   │                     | timeseries | named_entity_provider | ...)
│   └── (optional) timestamp
├── entity_relationships: list[EntityRelationship]
└── (optional) global_timestamp
```

## How to use

1. Construct a `DataSchema` matching the user's data requirements. Two equivalent forms:
   - **JSON dict** — convenient for simple cases, language-agnostic.
   - **Typed dataclasses** (`DataSchema`, `Entity`, `Column`, `Domain`, ...) — validated at construction, better for complex schemas.
2. Wrap it in `ra.GenerateFromDataSchema.Config(schema=..., upload_datasets=True)`.
3. Run via `WorkflowBuilder().add(generate).start(conn)`.
4. Pull results back with `workflow.datasets()`.

## Connection

```python
async with rf.Connection.from_config() as conn:
    ...
```

`from_config()` reads `~/.config/rockfish/config.toml` or `ROCKFISH_*` env vars. The `async with` ensures the underlying HTTP session is closed cleanly.

## Reference implementation

See [`reference/entity-gen.py`](reference/entity-gen.py) for four worked cases:

1. Simple device schema (JSON dict) — independent + derived columns.
2. User sessions (typed dataclasses) — state machine + timeseries + foreign key.
3. Trades (typed dataclasses) — composite foreign key.
4. Customers (typed dataclasses) — `NamedEntityProvider` for realistic PII-like values, with a multilingual variant.

## Gotchas

- **Cardinality vs. row count**: `Entity.cardinality` is the number of source rows generated for that entity *before* any sampling/expansion via relationships. A `sessions` entity referencing `users` has its own cardinality independent of `users.cardinality`.
- **State machines need a `trigger_column_name`**: the column named there appears in the output alongside the state column.
- **Composite foreign keys**: declare each FK column with `column_type=FOREIGN_KEY` and bind them in `EntityRelationship.join_columns` — the parent-side columns aren't repeated in the child entity's `columns` list.
- **`NamedEntityProvider` uniqueness**: pass `unique_values=N, with_replacement=False` when you need uniqueness in the generated pool (e.g. emails, SSNs).
