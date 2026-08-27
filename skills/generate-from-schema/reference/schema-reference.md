# `rockfish.actions.ent` schema reference

> Field-level reference for `GenerateFromDataSchema`, written for this skill. The doc source
> ([`docs/sdk/actions-ent.md`](https://docs.rockfish.ai/sdk/actions-ent.html)) is mostly
> `:::` mkdocstrings directives, so the field data here was extracted from the SDK
> docstrings and the rendered page. Regenerate from the SDK when `rockfish.actions.ent`
> changes.

Everything below is importable from `rockfish.actions.ent`.

Defaults are shown as `= value`. Fields with no default are required.

---

## Action and config

### `GenerateFromDataSchema`

Source action. Builds one PyArrow table per entity.

```python
generate = ra.GenerateFromDataSchema(
    ra.GenerateFromDataSchema.Config(schema=schema, upload_datasets=True)
)
# equivalent kwargs form:
generate = ra.GenerateFromDataSchema(schema=schema, upload_datasets=True)
```

### `GenerateFromDataSchemaConfig`

| Field | Type | Description |
| --- | --- | --- |
| `schema` | `DataSchema \| dict` | Entities and relationships. A dict is structured into a `DataSchema`. |
| `entity_labels` | `dict[str, LabelDict]` `= {}` | Per-entity Rockfish labels, e.g. `{"users": {"use_for": "testing"}}`. |
| `dataset_name_prefix` | `str` `= ""` | Prefix for generated dataset names. |
| `upload_datasets` | `bool` `= True` | `True` uploads each entity as a dataset; `False` yields tables to downstream actions. |

---

## `DataSchema`

Root of the schema.

| Field | Type | Description |
| --- | --- | --- |
| `entities` | `list[Entity]` | At least one; names must be unique. |
| `entity_relationships` | `list[EntityRelationship]` `= []` | |
| `global_timestamp` | `GlobalTimestamp \| None` `= None` | Required if any entity has a `timestamp`. |
| `seed` | `int \| None` `= None` | Global seed; every random stream derives from it. When unset the server draws a run seed, logs it, and stamps it on each uploaded dataset as an `ent_seed` label. Component-level seeds override the derived stream for that component. Must be non-negative. Needs server ≥ 0.73.0. |
| `scale_factor` | `float` `= 1.0` | Volume multiplier applied to `cardinality` of every entity with `scale_with_factor=True`. Must be finite and positive. |

**Validation**

- At least one entity; entity names unique.
- Any entity with a `timestamp` ⇒ `global_timestamp` required.
- Relationship `parent_entity`/`child_entity` must exist; `join_columns` must reference existing columns on both sides.
- `FOREIGN_KEY` columns may only appear in a `child_entity`, unless that entity is itself a child in another relationship (parent → child → grandchild chains are allowed).
- `ONE_TO_ONE` requires `child.cardinality <= parent.cardinality`.
- Every `FOREIGN_KEY` column must be declared in some relationship where its entity is the child, in either `join_columns` **or** `inherit_columns` (the validator unions both). A `FOREIGN_KEY` column is therefore not necessarily part of the key — an inherited receiver is a plain denormalized attribute.
- `join_columns` and `inherit_columns` are **mutually exclusive** on the child side: a child column named in both is rejected (`... are already join columns`). A column is either part of the key or a copied attribute, never both.
- Each foreign-key/inherited child column must be filled by exactly **one** relationship. Two relationships claiming the same child column is rejected.
- A child may be count-driven by at most one relationship; `GROUP_ORDINAL` columns are only valid on a count-driven child.

---

## `Entity`

| Field | Type | Description |
| --- | --- | --- |
| `name` | `str` | Becomes the dataset name. |
| `cardinality` | `int` | Number of entity instances (not rows). Must be positive. |
| `columns` | `list[Column]` | At least one; names unique within the entity. |
| `timestamp` | `Timestamp \| None` `= None` | Required iff the entity has ≥ 1 measurement column. |
| `scale_with_factor` | `bool` `= True` | Whether `DataSchema.scale_factor` multiplies this entity's cardinality. Set `False` on reference/dimension entities. |

**Validation**

- `cardinality > 0`; non-empty `columns`; unique column names.
- Has a timestamp **iff** it has ≥ 1 measurement column.
- A metadata column cannot depend on a measurement column (metadata is generated before timestamp expansion). A measurement may depend on metadata.
- Cross-entity `dependent_columns` (`"entity.column"`) are only allowed for `SAMPLE_FROM_COLUMN`.
- A column depending on the entity's timestamp column must be a measurement.
- Nothing may depend on an `AGGREGATE_FROM_CHILD` column — it is filled in a post-pass.
- `CUMULATIVE` columns and `AGGREGATE_FROM_CHILD(cumulative=True)` must be measurements on a timestamped entity.
- State-machine implicit column names (`trigger_column_name`, `context_variables` keys) must not collide with any explicit column or the timestamp column.

---

## `Column`

| Field | Type | Description |
| --- | --- | --- |
| `name` | `str` | |
| `data_type` | `str` | PyArrow type alias: `"string"`, `"int64"`, `"float64"`, `"bool"`, `"timestamp"`, `"timestamp[us]"`, `"decimal128(18, 2)"`, … Domain output is cast best-effort. |
| `column_type` | `ColumnType` | `INDEPENDENT` / `STATEFUL` / `DERIVED` / `FOREIGN_KEY`. |
| `column_category_type` | `ColumnCategoryType` | `METADATA` / `MEASUREMENT`. |
| `domain` | `Domain \| None` `= None` | For independent and stateful columns only. |
| `derivation` | `Derivation \| None` `= None` | For derived columns only. |

**Type × category matrix**

| `column_type` | Requires | Category | Notes |
| --- | --- | --- | --- |
| `INDEPENDENT` | `domain` (non-temporal) | `METADATA` only | Generated once per entity instance. |
| `STATEFUL` | `domain` = `TIMESERIES` or `STATE_MACHINE` | `MEASUREMENT` only | Evolves per timestamp. |
| `DERIVED` | `derivation` | either | Computed after independent/FK/stateful. |
| `FOREIGN_KEY` | neither | `METADATA` only | Filled by the relationship. |

Use `decimal128(18, 2)` (not `float64`) for monetary amounts so values stay exact to the cent.

---

## Timestamps

### `Timestamp`

| Field | Type | Description |
| --- | --- | --- |
| `column_name` | `str` | e.g. `"timestamp"`, `"event_time"`. |
| `data_type` | `str` `= "timestamp"` | Declared only; the emitted column is Arrow `string` regardless (verified against 0.79.0 for both `"timestamp"` and `"timestamp[us]"`). |

### `GlobalTimestamp`

| Field | Type | Description |
| --- | --- | --- |
| `t_start` | `str` | ISO 8601. |
| `t_end` | `str` | ISO 8601; must be after `t_start`. |
| `time_interval` | `str` | `<n><unit>` where unit is `min`, `hour`, `day`, or `month` — `"1min"`, `"15min"`, `"1hour"`, `"7day"`, `"3month"`. |
| `seed` | `int \| None` `= None` | Seeds session placement and state-machine transitions. Falls back to `DataSchema.seed`. Non-negative. Needs server ≥ 0.73.0. |

**Semantics.** `[t_start, t_end]` is discretized into ticks spaced `time_interval` apart.

- Entities whose only stateful columns are **timeseries** expand densely: one row per instance per tick.
- Entities with a **state machine** place each session at a random start tick and occupy consecutive ticks from there until a terminal state; other measurements are sampled at those ticks.

**Timezone contract.** `t_start`/`t_end` accept a `Z` suffix, an explicit offset, or no timezone (naive is read as UTC). Every generated timestamp is emitted as UTC in ISO 8601 with `+00:00` (server ≥ 0.77.0; older servers mirror the style of `t_start`).

**Output type.** The timestamp column is an Arrow **`string`**, not a timestamp type — so in pandas it is `str` and the `.dt` accessor raises `AttributeError` until you convert:

```python
ts = pd.to_datetime(df["timestamp"], utc=True)   # -> datetime64[us, UTC]
```

Do this before any resampling, `.dt.hour` grouping, or time-based join.

---

## Relationships

### `EntityRelationshipType`

`ONE_TO_ONE` — each parent row relates to exactly one unique child row.
`ONE_TO_MANY` — each parent row may be referenced by many child rows.

### `EntityRelationship`

| Field | Type | Description |
| --- | --- | --- |
| `parent_entity` | `str` | Holds the key. |
| `child_entity` | `str` | Holds the foreign key. |
| `relationship_type` | `EntityRelationshipType` | |
| `join_columns` | `dict[str, str]` `= {}` | `{parent_column: child_fk_column}`. Cannot be empty. Multiple pairs = composite key. |
| `child_count_column` | `str \| None` `= None` | Integer column on the parent; a parent row with value `k` emits exactly `k` child rows. Child `cardinality` is ignored. `ONE_TO_MANY` only. |
| `inherit_columns` | `dict[str, str]` `= {}` | `{parent_column: child_column}` — child copies the value from the **same** parent row it joined to. The receiving child column must be `FOREIGN_KEY`. |
| `self_reference` | `bool` `= False` | Entity references itself. `parent_entity == child_entity`, one `join_columns` pair, each row points at a strictly-earlier row (null for roots) — acyclic by construction. Cannot be combined with `child_count_column` or `inherit_columns`. |
| `partition_by` | `str \| None` `= None` | With `self_reference`, confine references to earlier rows sharing this metadata column's value. |
| `root_fraction` | `float` `= 0.0` | With `self_reference`, fraction of non-forced rows left as roots (null reference), in addition to the first row of each partition. |
| `weight_column` | `str \| None` `= None` | Numeric parent column; children are assigned in proportion to it rather than uniformly (Pareto-like "whales"). Sampled FK on `ONE_TO_MANY` only — not with `child_count_column`. |
| `affinity_column` | `str \| None` `= None` | Parent column for homophily sampling. Requires `affinity_local_column`. Sampled FK on `ONE_TO_MANY` only. |
| `affinity_local_column` | `str \| None` `= None` | Child metadata column matched against `affinity_column`. Must already be populated when the FK is sampled (e.g. inherited); cannot be a column this same relationship fills. |
| `affinity_prob` | `float` `= 0.0` | Probability in `[0, 1]` that a child matches a same-value parent; the rest draw from all parents. `0` disables affinity. Weighting still applies within the chosen pool. |

**Composite keys.** Multiple `join_columns` pairs make Rockfish sample matching *tuples* from the parent, preserving referential integrity across all of them. Every FK column is declared `column_type=FOREIGN_KEY` with no domain or derivation; the parent-side columns are not repeated in the child's `columns`.

**Count-driven fan-out.** Pairs naturally with the `GROUP_ORDINAL` domain (per-parent index on the child) and the `AGGREGATE_FROM_CHILD` derivation (roll child rows back up to the parent), so parent counters and totals match the child rows by construction.

---

## Domains

`Domain(type=DomainType.X, params=XParams(...))`. The params class must match the type.

`DomainType`: `ID`, `SEQUENTIAL_INT`, `CATEGORICAL`, `UNIFORM_DIST`, `NORMAL_DIST`, `EXPONENTIAL_DIST`, `LOGNORMAL_DIST`, `STATE_MACHINE`, `TIMESERIES`, `NAMED_ENTITY_PROVIDER`, `UNIQUE`, `GROUP_ORDINAL`, `SEQUENCE`, `MIXTURE`.

`TIMESERIES` and `STATE_MACHINE` are the only domains allowed on stateful (measurement) columns; every other domain is for independent (metadata) columns. `GROUP_ORDINAL` is valid only on a count-driven child.

### Identifiers and keys

**`IDParams`** — `template_str: str = "id_{id}"`. Must contain `{id}`.

**`SequentialIntParams`** — `start: int = 1`.

**`UniqueParams`** — globally-unique values allocated from a contiguous index range.

| Field | Type | Description |
| --- | --- | --- |
| `format` | `"template" \| "mac" \| "ipv4"` `= "template"` | |
| `template_str` | `str` `= "id-{index}"` | For `format="template"`. Must render distinct values per index; format specs like `"{index:06d}"` are allowed. |
| `start` | `int` `= 0` | First index; must be ≥ 0. |
| `mac_prefix` | `str` `= "02"` | First MAC octet (hex). Use a locally-administered value: `02`, `06`, `0A`, `0E`. |
| `ipv4_base` | `str` `= "10.0.0.0"` | Base address for `format="ipv4"`; index is added to it. |

**`GroupOrdinalParams`** — `start: int = 0`. A count-driven child's 0-based index within its parent group: a parent fanning out to K children numbers them `start … start+K-1`.

### Categorical and realistic values

**`CategoricalParams`**

| Field | Type | Description |
| --- | --- | --- |
| `values` | `list[Any]` | Non-empty. May be strings, ints, floats. |
| `weights` | `list[float] \| None` `= None` | Same length as `values`, and **must sum to 1.0**. |
| `seed` | `int \| None` `= None` | |
| `with_replacement` | `bool` `= True` | `False` uses each value at most once. |

**`NamedEntityProviderParams`** — builds a pool with [Mimesis](https://mimesis.name) (primary) or [Faker](https://faker.readthedocs.io) (fallback), then samples it.

| Field | Type | Description |
| --- | --- | --- |
| `provider` | `str` | Mimesis dot-path (`"person.first_name"`) or Faker bare name (`"ssn"`, `"iban"`). Any provider string works — the `NamedEntityProvider` constants are for discoverability. |
| `locale` | `str` `= "en"` | e.g. `"en_us"`, `"de"`, `"ja"`. |
| `unique_values` | `int` `= 100` | Max pool size; may be smaller for low-cardinality providers. Must be positive. |
| `seed` | `int \| None` `= None` | |
| `with_replacement` | `bool` `= True` | `False` samples without replacement, but is **force-enabled** when rows exceed the pool size. |

**`NamedEntityProvider` constants**

- Person: `PERSON_FIRST_NAME`, `PERSON_LAST_NAME`, `PERSON_FULL_NAME`, `PERSON_EMAIL`, `PERSON_USERNAME`, `PERSON_TELEPHONE`, `PERSON_OCCUPATION`, `PERSON_NATIONALITY`, `PERSON_BLOOD_TYPE`
- Address: `ADDRESS_CITY`, `ADDRESS_COUNTRY`, `ADDRESS_STATE`, `ADDRESS_STREET_NAME`, `ADDRESS_POSTAL_CODE`, `ADDRESS_ZIP_CODE`, `ADDRESS_LATITUDE`, `ADDRESS_LONGITUDE`, `ADDRESS_CALLING_CODE`
- Finance: `FINANCE_BANK`, `FINANCE_COMPANY`, `FINANCE_CURRENCY_ISO_CODE`, `FINANCE_CURRENCY_SYMBOL`, `FINANCE_PRICE`, `FINANCE_STOCK_TICKER`, `FINANCE_CRYPTOCURRENCY_ISO_CODE`
- Payment: `PAYMENT_CREDIT_CARD_NUMBER`, `PAYMENT_CVV`, `PAYMENT_BITCOIN_ADDRESS`
- Internet: `INTERNET_IP_V4`, `INTERNET_IP_V6`, `INTERNET_MAC_ADDRESS`, `INTERNET_URL`, `INTERNET_USER_AGENT`, `INTERNET_HTTP_METHOD`, `INTERNET_HTTP_STATUS_CODE`, `INTERNET_TLD`, `INTERNET_SLUG`
- Cryptographic: `CRYPTOGRAPHIC_UUID`, `CRYPTOGRAPHIC_TOKEN_HEX`, `CRYPTOGRAPHIC_API_KEY`
- Code: `CODE_EAN`, `CODE_ISBN`, `CODE_IMEI`, `CODE_PIN`
- Datetime: `DATETIME_TIMEZONE`, `DATETIME_DAY_OF_WEEK`, `DATETIME_MONTH`
- Hardware: `HARDWARE_CPU`, `HARDWARE_PHONE_MODEL`, `HARDWARE_RESOLUTION`
- Transport: `TRANSPORT_CAR`, `TRANSPORT_AIRPLANE`
- Food: `FOOD_DISH`, `FOOD_DRINK`
- Development: `DEVELOPMENT_PROGRAMMING_LANGUAGE`, `DEVELOPMENT_OS`, `DEVELOPMENT_SOFTWARE_LICENSE`
- Text: `TEXT_WORD`, `TEXT_COLOR`, `TEXT_EMOJI`
- Faker fallback (bare names): `ADDRESS`, `SSN`, `IBAN`, `BBAN`, `ABA`

### Statistical distributions

| Params | Fields |
| --- | --- |
| `UniformDistParams` | `lower: float` (inclusive), `upper: float` (exclusive, must exceed `lower`), `seed = None` |
| `NormalDistParams` | `mean: float`, `std: float` (> 0), `seed = None` |
| `LogNormalDistParams` | `mu: float`, `sigma: float` (> 0), `seed = None` — mean/std of the underlying normal in **natural-log space**, not of the lognormal itself. `exp(Normal(mu, sigma))`. |
| `ExponentialDistParams` | `scale: float` (> 0, = 1/λ), `seed = None` |

### Structured and composite

**`MixtureParams`** — weighted mixture of sub-domains.

| Field | Type | Description |
| --- | --- | --- |
| `components` | `list[dict]` | Each `{"weight": float, "domain": Domain}`. Weights need not sum to 1 — they are normalized. Components may nest mixtures but may **not** be `UNIQUE` or `GROUP_ORDINAL` (whole-column allocators). |
| `seed` | `int \| None` `= None` | Seeds the per-row component assignment. |

**`SequenceParams`** — variable-length token sequence rendered as a string.

| Field | Type | Description |
| --- | --- | --- |
| `segments` | `list[dict]` | Ordered; each `{"tokens": [...], "min_repeat": int = 1, "max_repeat": int = min_repeat}`. Each segment contributes its tokens repeated a per-row count drawn uniformly from the range. |
| `encoding` | `"json" \| "delimited"` `= "json"` | `"json"` → `["a","b"]`; `"delimited"` → joined by `separator`. |
| `separator` | `str` `= ","` | |
| `seed` | `int \| None` `= None` | |

### Temporal (stateful columns only)

**`TimeseriesParams`**

| Field | Type | Description |
| --- | --- | --- |
| `base_value` | `float` | Central value the series oscillates around. |
| `min_value` | `float` | Hard floor (clipped); must be below `max_value`. |
| `max_value` | `float` | Hard ceiling (clipped). |
| `seasonality_type` | `"symmetric" \| "peak_offpeak" \| "none"` `= "symmetric"` | `symmetric` = smooth sinusoid over the day; `peak_offpeak` = higher inside the peak window; `none` = base + noise only. |
| `peak_start_hour` | `int` `= 8` | `[0, 24]`, must be < `peak_end_hour`. |
| `peak_end_hour` | `int` `= 22` | `[0, 24]`. |
| `seasonality_strength` | `float` `= 0.3` | `[0, 1]`. |
| `noise_level` | `float` `= 0.1` | `[0, 1]`. |
| `spike_probability` | `float` `= 0.0` | `[0, 1]`, per timestamp. |
| `spike_magnitude` | `float` `= 0.3` | `[0, 1]`, relative to range. |
| `interval_minutes` | `int` `= 15` | Should match `global_timestamp.time_interval`. |
| `seed` | `int \| None` `= None` | |

**`StateMachineParams`**

| Field | Type | Description |
| --- | --- | --- |
| `trigger_column_name` | `str` | Creates an **implicit column** holding the trigger/action per row. Must differ from the stateful column's name. |
| `initial_state` | `str` | Must be in `states`. |
| `states` | `list[str]` | |
| `terminal_states` | `list[str]` | All must be in `states`; reaching one ends the session. |
| `transitions` | `list[Transition]` | |
| `context_variables` | `dict[str, bool]` `= {}` | Each key becomes an **implicit boolean column**. Names must differ from the stateful column and `trigger_column_name`. |
| `column_name` | `str \| None` `= None` | Deprecated; overwritten by `Column.name`. Do not set it. |

**`Transition`**

| Field | Type | Description |
| --- | --- | --- |
| `trigger` | `str` | Action/event name. |
| `source` | `str` | Must be a valid state. |
| `dest` | `str` | Must be a valid state. Self-loops (`source == dest`) are allowed. |
| `probability` | `float` | `0 < p <= 1`. A **weight**: transitions sharing a source are normalized together, so `[2, 1, 1]` becomes `[0.5, 0.25, 0.25]`. |
| `conditions` | `list[str]` `= []` | Context variables that must be `True` for eligibility; each must be declared in `context_variables`. |
| `context_updates` | `dict[str, bool]` `= {}` | Applied after the transition; keys must be declared in `context_variables`. |

---

## Derivations

`Derivation(function_type=DerivationFunctionType.X, dependent_columns=[...], params=XParams(...))`.

`dependent_columns` reference format: `"column_name"` for the same entity, `"entity_name.column_name"` for cross-entity (**only** `SAMPLE_FROM_COLUMN`).

| Function type | Params | Dependent columns | Notes |
| --- | --- | --- | --- |
| `SUM` | `SumParams()` | 2+ numeric | Element-wise. |
| `MULTIPLY` | `MultiplyParams()` | 2+ numeric | Element-wise. |
| `ROUND` | `RoundParams(decimals=2)` | exactly 1 numeric | `decimals >= 0`. |
| `SAMPLE_FROM_COLUMN` | `SampleFromColumnParams(with_replacement=True, seed=None)` | 1, may be `"entity.column"` | The usual derived foreign key. |
| `SAMPLE_FROM_COLUMN_WHERE` | `SampleFromColumnWhereParams(source_value_column, source_filter_column, seed=None)` | exactly 1, and it **must** be a local same-entity column (a `"entity.column"` reference here is rejected) | Draws from `source_value_column` restricted to source rows whose `source_filter_column` equals this row's local value. Both source refs are `"entity.column"` on the same source entity. Correlated FK — e.g. an order picks an address owned by its own customer. |
| `CONDITIONAL_SAMPLE` | `ConditionalSampleParams(cases, default=None)` | exactly 1 | `cases: dict[Any, Domain]` keyed by the condition column's values (JSON-authored keys match by string form). Unmatched value with no `default` raises at generation time. |
| `MAP_VALUES` | `MapValuesParams(mapping, default=None)` | 1+ | `mapping` is a list of `{"from": ..., "to": ...}`; `"from"` may be a list for tuple mapping over several columns. Non-empty. |
| `COPY` | `CopyParams()` | exactly 1 | Row-aligned alias. |
| `CUMULATIVE` | `CumulativeParams(operation="sum")` | exactly 1 | `operation` ∈ `sum` / `min` / `max`. Session-aware running aggregate in timestamp order; resets at session boundaries. Must be a **measurement** column on a timestamped entity. |
| `AGGREGATE_FROM_CHILD` | `AggregateFromChildParams(...)` | **must be empty** | See below. |
| `FORMAT_TIMESTAMP` | `FormatTimestampParams(output="epoch_s", format_str=None)` | exactly 1 timestamp | `output` ∈ `epoch_s` / `epoch_ms` / `strftime`; `format_str` required for `strftime`. |
| `SHIFT_TIMESTAMP` | `ShiftTimestampParams(unit="seconds")` | exactly 2: `[base_timestamp, offset]` | `unit` ∈ `"seconds"`, `"minutes"`, `"hours"`, `"days"`, `"ms"`. Returns `base + offset`. |
| `STRING_TEMPLATE` | `StringTemplateParams(template)` | 1+ | `str.format` template; every named field must appear in `dependent_columns`. |
| `SUBSTRING` | `SubstringParams(start=0, length=None)` | exactly 1 string | `value[start:start+length]`; `start >= 0`, `length=None` means to the end. |
| `LUHN_APPEND` | `LuhnAppendParams()` | exactly 1 numeric string | Appends a Luhn (mod-10) check digit so the result validates. Non-digits are ignored in the computation but preserved. |

**`AggregateFromChildParams`**

| Field | Type | Description |
| --- | --- | --- |
| `child_entity` | `str` | A parent → child relationship to it must exist. |
| `operation` | `"count" \| "sum"` `= "count"` | |
| `value_column` | `str \| None` `= None` | Child column to sum; required for `"sum"`, ignored for `"count"`. |
| `filter_column` | `str \| None` `= None` | Optional child column filtered by equality. |
| `filter_value` | `Any` `= None` | Required when `filter_column` is set. |
| `cumulative` | `bool` `= False` | Accumulate the per-row aggregate within each session in timestamp order. Requires a measurement column on a timestamped parent, and the relationship must join on the parent timestamp. |

For composite foreign keys, prefer `column_type=FOREIGN_KEY` plus an `EntityRelationship` over explicit derivations — Rockfish handles the multi-column tuple sampling that maintains referential integrity.

`DerivationFunctionType` also contains `SAMPLE_FROM_COLUMNS`, which has no corresponding params class in the public API — do not use it.

---

## JSON form

Every typed object has an equivalent dict form, using the enum *string values*
(`"independent"`, `"metadata"`, `"categorical"`, `"map_values"`, …). Nested `Domain`
objects inside `MixtureParams.components` and `ConditionalSampleParams.cases` are
structured recursively, so a mixture in JSON nests plain `{"type": ..., "params": ...}`
dicts. Prefer the typed classes: they validate at construction rather than at run time.

```python
schema = {
    "entities": [{
        "name": "devices",
        "cardinality": 20,
        "columns": [
            {"name": "device_id", "data_type": "string",
             "column_type": "independent", "column_category_type": "metadata",
             "domain": {"type": "id", "params": {"template_str": "DEVICE_{id}"}}},
            {"name": "status", "data_type": "string",
             "column_type": "independent", "column_category_type": "metadata",
             "domain": {"type": "categorical",
                        "params": {"values": ["up", "down"], "weights": [0.9, 0.1]}}},
            {"name": "severity", "data_type": "string",
             "column_type": "derived", "column_category_type": "metadata",
             "derivation": {"function_type": "map_values",
                            "dependent_columns": ["status"],
                            "params": {"mapping": [{"from": "down", "to": "critical"}],
                                       "default": "unknown"}}},
        ],
    }],
    "entity_relationships": [],
    "seed": 42,
}
generate = ra.GenerateFromDataSchema(schema=schema, upload_datasets=True)
```

---

## Requirements

This reference documents **rockfish 0.79.0**, verified field-by-field against that
release. Install or upgrade with:

```bash
uv pip install --find-links https://packages.rockfish.ai --upgrade 'rockfish[labs]'
```

Confirm what you have with `importlib.metadata.version("rockfish")`. On an older SDK, a
feature added after that release fails at **import** time (`ImportError` from
`rockfish.actions.ent`) or as an unexpected keyword argument — not at generation time. The
features most likely to be missing on a pre-0.79 install are `DataSchema.scale_factor`,
`Entity.scale_with_factor`, the advanced `EntityRelationship` fields (`inherit_columns`,
`self_reference`, `weight_column`, `affinity_*`), and the `ROUND`, `SUBSTRING`,
`LUHN_APPEND`, `SHIFT_TIMESTAMP`, and `SAMPLE_FROM_COLUMN_WHERE` derivations. A few
behaviors also depend on the **server** version: `seed` needs ≥ 0.73.0, and
UTC-normalized output timestamps need ≥ 0.77.0.
