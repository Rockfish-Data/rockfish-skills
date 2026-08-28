# Patterns and recipes

> Authored for this skill, drawing on the
> [telecom RAN tutorial](https://docs.rockfish.ai/uc-demos/use-case-product-demo-telecom-ran.html)
> (the walkthrough and FAQ track it closely) and the "Tips and Best Practices" section of the
> SDK reference. Most snippets are original.

Task-shaped recipes for `GenerateFromDataSchema`. Field-level detail lives in
[`schema-reference.md`](schema-reference.md); the data-model decision lives in
[`data-models.md`](data-models.md).

All snippets assume the usual imports from `rockfish.actions.ent`.

---

## Sizing the output

**Tabular entity** — rows = `cardinality`.

**Time-series entity with `TIMESERIES` columns (dense)** — rows = `cardinality × ticks`,
where ticks is the number of `time_interval` steps in `[t_start, t_end]`, inclusive of both
ends. Every instance gets a row at every tick.

Two days at `15min` is `2 × 96 + 1 = 193` ticks:

| Entity | cardinality | rows |
| --- | --- | --- |
| `transport_link` | 6 | 6 × 193 = 1,158 |
| `core_node` | 16 | 16 × 193 = 3,088 |
| `cell_site` | 100 | 100 × 193 = 19,300 |

**Time-series entity with a `STATE_MACHINE` (sparse)** — the formula above does not apply.
Each session starts at a random tick and occupies consecutive ticks only until it reaches a
terminal state, so rows ≈ `cardinality × mean walk length`, independent of the window width.
Widening the window spreads the sessions out; it does not add rows. Over the 193-tick window
above, sessions averaging ~4 steps yield ~400 rows at `cardinality=100`, not 19,300.

**Choosing cardinalities.** Match real-world scale, but iterate small — 5–20 instances while
you are still shaping the schema, 100–1000+ for the final run. Think in ratios: 100 cells to
6 transport links is ~17 cells per link.

**Scaling one schema.** Rather than editing per-entity counts, set `DataSchema.scale_factor`
and mark which entities grow:

```python
DataSchema(
    entities=[
        Entity(name="devices", cardinality=100, scale_with_factor=False, columns=[...]),  # fixed catalog
        Entity(name="readings", cardinality=10_000, columns=[...]),                        # grows
    ],
    scale_factor=10.0,   # readings → 100_000; devices stays at 100
)
```

Count-driven children ignore `cardinality` entirely and scale through their parent's row count.

---

## Reproducibility

```python
DataSchema(entities=[...], seed=42)
```

One global `seed` makes the entire run deterministic — same schema, same seed, identical
output. Leave it unset when you want fresh variation: the server draws a run seed, logs it,
and attaches it to every uploaded dataset as an `ent_seed` label, so a run can be reproduced
after the fact by setting `seed` to that value.

Per-component seeds (`CategoricalParams.seed`, `NormalDistParams.seed`,
`GlobalTimestamp.seed`, …) override the global stream for just that component — useful when
most of a schema should vary run-to-run but a few columns must stay fixed.

---

## Foreign keys

### Simple FK, sampled

Declare the child column as `FOREIGN_KEY` (no domain, no derivation) and let the
relationship fill it:

```python
Column(name="user_id", data_type="string",
       column_type=ColumnType.FOREIGN_KEY,
       column_category_type=ColumnCategoryType.METADATA)
...
EntityRelationship(parent_entity="users", child_entity="sessions",
                   relationship_type=EntityRelationshipType.ONE_TO_MANY,
                   join_columns={"user_id": "user_id"})
```

The alternative — a `DERIVED` column using `SAMPLE_FROM_COLUMN` with
`dependent_columns=["users.user_id"]` — also works and is the only derivation permitted to
reference another entity. Prefer the `FOREIGN_KEY` form: it declares the relationship
explicitly and is what composite keys require.

### Composite FK

Declare every FK column, then bind all pairs in one relationship. Rockfish samples matching
*tuples*, so referential integrity holds across the whole key:

```python
# in cell_site
Column(name="Transport_Device_ID",    data_type="string", column_type=ColumnType.FOREIGN_KEY,
       column_category_type=ColumnCategoryType.METADATA),
Column(name="Transport_Interface_ID", data_type="string", column_type=ColumnType.FOREIGN_KEY,
       column_category_type=ColumnCategoryType.METADATA),

EntityRelationship(
    parent_entity="transport_link", child_entity="cell_site",
    relationship_type=EntityRelationshipType.ONE_TO_MANY,
    join_columns={"Device_ID": "Transport_Device_ID",
                  "Interface_ID": "Transport_Interface_ID"},
)
```

The parent-side columns are *not* repeated in the child's `columns` list.

### Correlated FK — pick within a group

An order should pick a shipping address belonging to *its own* customer, not any address:

```python
Column(
    name="address_id", data_type="string",
    column_type=ColumnType.DERIVED,
    column_category_type=ColumnCategoryType.METADATA,
    derivation=Derivation(
        function_type=DerivationFunctionType.SAMPLE_FROM_COLUMN_WHERE,
        dependent_columns=["customer_id"],           # the local filter value
        params=SampleFromColumnWhereParams(
            source_value_column="addresses.address_id",
            source_filter_column="addresses.customer_id",
            seed=7,
        ),
    ),
)
```

---

## Parent/child fan-out (orders and line items)

A count column on the parent drives the child's row count. Number children within their
parent with `GROUP_ORDINAL`, and roll totals back up with `AGGREGATE_FROM_CHILD`, so parent
counters match child rows by construction:

```python
# orders
Column(name="line_count", data_type="int64",
       column_type=ColumnType.INDEPENDENT, column_category_type=ColumnCategoryType.METADATA,
       domain=Domain(type=DomainType.CATEGORICAL, params=CategoricalParams(values=[1, 2, 3, 4, 5]))),
Column(name="order_total", data_type="decimal128(18, 2)",
       column_type=ColumnType.DERIVED, column_category_type=ColumnCategoryType.METADATA,
       derivation=Derivation(
           function_type=DerivationFunctionType.AGGREGATE_FROM_CHILD,
           dependent_columns=[],                     # must be empty
           params=AggregateFromChildParams(child_entity="line_items",
                                           operation="sum", value_column="amount"))),

# line_items — cardinality is ignored
Column(name="order_id", data_type="string",
       column_type=ColumnType.FOREIGN_KEY, column_category_type=ColumnCategoryType.METADATA),
Column(name="line_no", data_type="int64",
       column_type=ColumnType.INDEPENDENT, column_category_type=ColumnCategoryType.METADATA,
       domain=Domain(type=DomainType.GROUP_ORDINAL, params=GroupOrdinalParams(start=0))),

EntityRelationship(parent_entity="orders", child_entity="line_items",
                   relationship_type=EntityRelationshipType.ONE_TO_MANY,
                   join_columns={"order_id": "order_id"},
                   child_count_column="line_count")
```

Count-driven fan-out is `ONE_TO_MANY` only, and a child may be count-driven by at most one
relationship. Nothing else in the parent entity may depend on the `AGGREGATE_FROM_CHILD`
column — it is filled in a post-pass.

To count only some children, add a filter:
`AggregateFromChildParams(child_entity="line_items", operation="count", filter_column="kind", filter_value="return")`.

---

## Denormalized columns that must agree with the parent

`inherit_columns` copies a value from the **same** parent row the child joined to, instead of
sampling it independently — so a line item's `customer_key` always matches its order's:

```python
EntityRelationship(
    parent_entity="orders", child_entity="line_items",
    relationship_type=EntityRelationshipType.ONE_TO_MANY,
    join_columns={"order_id": "order_id"},
    inherit_columns={"customer_key": "customer_key", "region": "region"},
)
```

The receiving child columns must be declared `column_type=FOREIGN_KEY` — that marker means "a relationship fills this in," not "this is a key." Leave them out of `join_columns`: adding them there would make them part of the relationship's key instead of a copied attribute.

---

## Skewed activity ("whales")

Real activity is Pareto-shaped: a few parents account for most children. Weight parent
selection by a numeric parent column:

```python
# on the parent: a right-skewed activity weight
Column(name="activity_weight", data_type="float64",
       column_type=ColumnType.INDEPENDENT, column_category_type=ColumnCategoryType.METADATA,
       domain=Domain(type=DomainType.LOGNORMAL_DIST,
                     params=LogNormalDistParams(mu=1.0, sigma=1.2))),

EntityRelationship(parent_entity="accounts", child_entity="transactions",
                   relationship_type=EntityRelationshipType.ONE_TO_MANY,
                   join_columns={"account_id": "account_id"},
                   weight_column="activity_weight")
```

Only for a sampled FK on `ONE_TO_MANY` — not combinable with `child_count_column`.

---

## Homophily / co-location

With probability `affinity_prob`, match a child to a parent sharing a value — a transaction
preferring a merchant in the cardholder's own country. Weighting still applies within the
chosen pool:

```python
EntityRelationship(
    parent_entity="merchants", child_entity="transactions",
    relationship_type=EntityRelationshipType.ONE_TO_MANY,
    join_columns={"merchant_id": "merchant_id"},
    affinity_column="country",              # on merchants
    affinity_local_column="card_country",   # on transactions, already populated
    affinity_prob=0.8,
    weight_column="merchant_volume",
)
```

`affinity_local_column` must already hold a value when the FK is sampled (inherited or
looked up), and cannot be a column this same relationship fills.

---

## Hierarchies and chains

An employee's manager, a comment's parent comment — one entity referencing itself. Each row
points at a strictly-earlier row, so the result is acyclic by construction:

```python
EntityRelationship(
    parent_entity="comments", child_entity="comments",
    relationship_type=EntityRelationshipType.ONE_TO_MANY,
    join_columns={"comment_id": "parent_comment_id"},
    self_reference=True,
    partition_by="thread_id",   # a reply references an earlier comment in the SAME thread
    root_fraction=0.3,          # 30% of rows are top-level (null parent)
)
```

`parent_comment_id` is declared `FOREIGN_KEY`. Without `self_reference=True`, a relationship
from an entity to itself is rejected.

---

## Realistic timeseries measurements

```python
Column(
    name="RRC_ConnEstabFail", data_type="int64",
    column_type=ColumnType.STATEFUL,
    column_category_type=ColumnCategoryType.MEASUREMENT,
    domain=Domain(type=DomainType.TIMESERIES, params=TimeseriesParams(
        base_value=5.0,               # ~5 failures per interval
        min_value=0.0,                # no negative failures
        max_value=20.0,
        seasonality_type="peak_offpeak",
        peak_start_hour=8, peak_end_hour=22,
        seasonality_strength=0.35,    # 35% time-of-day swing
        noise_level=0.2,
        spike_probability=0.02,       # 2% chance of an anomaly per tick
        spike_magnitude=0.3,
        interval_minutes=15,          # match global_timestamp.time_interval
        seed=211,
    )),
)
```

A high-availability metric wants the opposite shape — no daily pattern, low noise, rare
anomalies:

```python
TimeseriesParams(base_value=99.5, min_value=97.0, max_value=100.0,
                 seasonality_type="none", noise_level=0.05,
                 spike_probability=0.005, spike_magnitude=0.5,
                 interval_minutes=15, seed=210)
```

Tuning notes:

- `min_value`/`max_value` are hard clips. Set them to real physical bounds (a percentage
  caps at 100, a count floors at 0).
- `seasonality_strength` and `noise_level` are fractions of the base value, not absolutes.
- To inject a specific labelled anomaly (a known outage window, a ramp) rather than random
  spikes, generate a clean baseline here and use the `inject-scenarios` skill.

---

## Consistent derived measurements

Derive totals instead of generating them independently, so the arithmetic always holds:

```python
# RRC_ConnEstabSucc and RRC_ConnEstabFail are STATEFUL measurements
Column(
    name="RRC_ConnEstabAtt", data_type="int64",
    column_type=ColumnType.DERIVED,
    column_category_type=ColumnCategoryType.MEASUREMENT,
    derivation=Derivation(
        function_type=DerivationFunctionType.SUM,
        dependent_columns=["RRC_ConnEstabSucc", "RRC_ConnEstabFail"],
        params=SumParams(),
    ),
)
```

Derived columns may depend on other derived columns — Rockfish resolves the order.

---

## Running counters

A ledger or odometer column: a running total within each session, in timestamp order,
resetting at session boundaries.

```python
Column(name="events_total", data_type="int64",
       column_type=ColumnType.DERIVED,
       column_category_type=ColumnCategoryType.MEASUREMENT,   # must be measurement
       derivation=Derivation(
           function_type=DerivationFunctionType.CUMULATIVE,
           dependent_columns=["events_this_window"],           # exactly one
           params=CumulativeParams(operation="sum")))
```

---

## Money

Use a parameterized decimal, and round the distribution onto real units:

```python
Column(name="amount_raw", data_type="float64",
       column_type=ColumnType.INDEPENDENT, column_category_type=ColumnCategoryType.METADATA,
       domain=Domain(type=DomainType.LOGNORMAL_DIST, params=LogNormalDistParams(mu=3.5, sigma=0.8))),
Column(name="amount", data_type="decimal128(18, 2)",
       column_type=ColumnType.DERIVED, column_category_type=ColumnCategoryType.METADATA,
       derivation=Derivation(function_type=DerivationFunctionType.ROUND,
                             dependent_columns=["amount_raw"],
                             params=RoundParams(decimals=2))),
```

---

## Multimodal columns

A column that is part point-mass, part heavy tail — churned customers at zero spend, active
customers lognormal:

```python
Domain(type=DomainType.MIXTURE, params=MixtureParams(components=[
    {"weight": 0.3, "domain": Domain(type=DomainType.CATEGORICAL,
                                     params=CategoricalParams(values=[0.0]))},
    {"weight": 0.7, "domain": Domain(type=DomainType.LOGNORMAL_DIST,
                                     params=LogNormalDistParams(mu=4.0, sigma=0.6))},
]))
```

Mixture weights are normalized for you. Components cannot be `UNIQUE` or `GROUP_ORDINAL`.

When the mode should depend on *another column* rather than a fixed weight, use
`CONDITIONAL_SAMPLE` instead:

```python
Derivation(function_type=DerivationFunctionType.CONDITIONAL_SAMPLE,
           dependent_columns=["status"],
           params=ConditionalSampleParams(cases={
               "healthy": Domain(type=DomainType.NORMAL_DIST, params=NormalDistParams(mean=10.0, std=1.0)),
               "broken":  Domain(type=DomainType.NORMAL_DIST, params=NormalDistParams(mean=100.0, std=20.0)),
           }, default=Domain(type=DomainType.NORMAL_DIST, params=NormalDistParams(mean=10.0, std=5.0))))
```

---

## Realistic PII-like values

```python
# guaranteed-unique account key
Domain(type=DomainType.UNIQUE, params=UniqueParams(format="template", template_str="ACCT-{index:06d}"))
# realistic names
Domain(type=DomainType.NAMED_ENTITY_PROVIDER,
       params=NamedEntityProviderParams(provider=NamedEntityProvider.PERSON_FULL_NAME, locale="en"))
# unique-ish emails from a large pool
Domain(type=DomainType.NAMED_ENTITY_PROVIDER,
       params=NamedEntityProviderParams(provider=NamedEntityProvider.PERSON_EMAIL,
                                        unique_values=5000, with_replacement=False))
# a valid-looking card number
Domain(type=DomainType.NAMED_ENTITY_PROVIDER,
       params=NamedEntityProviderParams(provider=NamedEntityProvider.PAYMENT_CREDIT_CARD_NUMBER))
# MAC addresses for device data
Domain(type=DomainType.UNIQUE, params=UniqueParams(format="mac", mac_prefix="02"))
```

Switching `locale` switches the language/region: `locale="de"` gives German names and
cities. Set `unique_values` large enough that the pool is not exhausted — `with_replacement`
is force-enabled if the row count exceeds the pool.

---

## Behavioral sequences

```python
Column(
    name="page", data_type="string",
    column_type=ColumnType.STATEFUL,
    column_category_type=ColumnCategoryType.MEASUREMENT,
    domain=Domain(type=DomainType.STATE_MACHINE, params=StateMachineParams(
        trigger_column_name="action",     # creates an implicit "action" column
        initial_state="homepage",
        states=["homepage", "search", "product", "cart", "checkout", "exit"],
        terminal_states=["exit"],
        transitions=[
            Transition(trigger="browse",       source="homepage", dest="search",   probability=0.6),
            Transition(trigger="view_product", source="homepage", dest="product",  probability=0.3),
            Transition(trigger="leave",        source="homepage", dest="exit",      probability=0.1),
            # search is non-terminal, so it needs outgoing transitions too
            Transition(trigger="view_product", source="search",   dest="product",  probability=0.7),
            Transition(trigger="leave",        source="search",   dest="exit",      probability=0.3),
            Transition(trigger="add_to_cart",  source="product",  dest="cart",      probability=0.5),
            Transition(trigger="leave",        source="product",  dest="exit",      probability=0.5),
            Transition(trigger="checkout",     source="cart",     dest="checkout",  probability=0.7),
            Transition(trigger="leave",        source="cart",     dest="exit",       probability=0.3),
            Transition(trigger="complete",     source="checkout", dest="exit",      probability=1.0),
        ],
    )),
)
```

- Probabilities are **weights**, normalized per source state: `[2, 1, 1]` becomes
  `[0.5, 0.25, 0.25]`.
- Every non-terminal state needs at least one outgoing transition, or a session that reaches
  it cannot progress.
- A `conditions` gate needs a *producer*: some earlier transition must set that context
  variable via `context_updates`, or the gated transition is never eligible and sessions
  stall. Check every condition has a path that sets it before it is needed.
- `trigger_column_name` and each `context_variables` key become real output columns; their
  names must not collide with anything else in the entity, including the timestamp.
- Gate transitions with `conditions` and flip flags with `context_updates` for flows where
  order matters (payment received before shipping):

```python
StateMachineParams(
    trigger_column_name="event",
    initial_state="pending",
    states=["pending", "processing", "shipped", "delivered"],
    terminal_states=["delivered"],
    transitions=[
        # Something must SET the flag before a gated transition can fire. Without
        # this self-loop, `pending` has no eligible path out and every session
        # stalls there -- a condition with no producer is a deadlock.
        Transition(trigger="receive_payment", source="pending", dest="pending",
                   probability=0.5, context_updates={"payment_received": True}),
        Transition(trigger="process", source="pending", dest="processing",
                   probability=0.9, conditions=["payment_received"],
                   context_updates={"in_fulfillment": True}),
        # processing and shipped are non-terminal, so they need exits as well
        Transition(trigger="ship",    source="processing", dest="shipped",   probability=1.0),
        Transition(trigger="deliver", source="shipped",    dest="delivered", probability=1.0),
    ],
    context_variables={"payment_received": False, "in_fulfillment": False},
)
```

---

## Walkthrough: telecom RAN

A three-entity RAN schema, the shape most Rockfish demos follow. Full tutorial:
[docs.rockfish.ai/uc-demos/use-case-product-demo-telecom-ran.html](https://docs.rockfish.ai/uc-demos/use-case-product-demo-telecom-ran.html);
notebooks in the [public tutorials repo](https://github.com/Rockfish-Data/public_tutorials/blob/main/Use%20Cases/RAN%20Schema%20based%20Data%20Generation/README.md).

Entities:

- `transport_link` — 6 network infrastructure devices, keyed by `(Device_ID, Interface_ID)`.
- `core_node` — 16 core elements (MME, AMF, SMF, UPF).
- `cell_site` — 100 radio access points, referencing `transport_link` via the composite FK
  `(Transport_Device_ID, Transport_Interface_ID)`.

Global window: `2025-01-01T00:00:00Z` → `2025-01-03T00:00:00Z` at `15min` (193 ticks).

Column assignment for `cell_site`:

| Column | Type | Category | Domain / derivation |
| --- | --- | --- | --- |
| `Cell_ID` | independent | metadata | `ID` |
| `Base_Station_ID` | independent | metadata | `CATEGORICAL` over `["eNB_001", …, "gNB_002"]` |
| `Location_Lat` / `Location_Lon` | independent | metadata | `UNIFORM_DIST` over a lat/lon box |
| `Transport_Device_ID` / `Transport_Interface_ID` | foreign_key | metadata | filled by the relationship |
| `Cell_Availability` | stateful | measurement | `TIMESERIES`, `seasonality_type="none"` |
| `RRC_ConnEstabSucc` / `RRC_ConnEstabFail` | stateful | measurement | `TIMESERIES`, `peak_offpeak` |
| `RRC_ConnEstabAtt` | derived | measurement | `SUM` of Succ + Fail |
| `DL_PRB_Utilization` | stateful | measurement | `TIMESERIES`, `peak_offpeak` |

Assemble and run:

```python
schema = DataSchema(
    entities=[transport_link, core_node, cell_site],
    entity_relationships=relationships,
    global_timestamp=global_timestamp,
    seed=42,
)

generate = ra.GenerateFromDataSchema(ra.GenerateFromDataSchema.Config(schema=schema))

async def main():
    async with rf.Connection.from_config() as conn:
        builder = rf.WorkflowBuilder()
        builder.add(generate)          # add() returns None -- not chainable
        workflow = await builder.start(conn)
        await workflow.wait(raise_on_failure=True)
        for remote_ds in await workflow.datasets().collect():
            ds = await remote_ds.to_local(conn)
            print(f"{ds.name()}: {ds.table.num_rows} rows")


asyncio.run(main())   # or just `await main()` inside a notebook
```

Output: `transport_link` 1,158 rows, `core_node` 3,088 rows, `cell_site` 19,300 rows.

---

## FAQ

**When is a column `INDEPENDENT` vs `STATEFUL`?** Independent if the value does not change
over time (IDs, locations, device types); stateful if it evolves per timestamp (bandwidth,
CPU, counters). Independent implies metadata; stateful implies measurement.

**`METADATA` vs `MEASUREMENT`?** Metadata describes the entity — who/what/where. Measurement
is a per-timestamp metric — how much/how many. See [`data-models.md`](data-models.md).

**Which `data_type`?** `string` for IDs, names, categorical text; `int64` for whole-number
counts; `float64` for continuous measurements; `decimal128(18, 2)` for money;
`timestamp` for entity timestamps.

**Can derived columns depend on other derived columns?** Yes — the dependency order is
resolved automatically. The one exception is `AGGREGATE_FROM_CHILD`, computed in a post-pass,
which nothing else may depend on.

**What order are columns generated in?** Independent → foreign keys → stateful → derived
(in dependency order). This is why a metadata column cannot depend on a measurement column.

**Where do I get realistic `CATEGORICAL` values?** Enumerate them yourself for small fixed
sets, or use `NAMED_ENTITY_PROVIDER` when you want a large realistic pool instead of a list.

**How do I add a labelled anomaly?** Generate a clean baseline here, then use the
`inject-scenarios` skill to inject a spike, outage, ramp, or shift with known ground truth.
