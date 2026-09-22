# Pipeline reference

The full API surface for model-based generation, in the order you use it. Model configs and hyperparameters live in [`models.md`](models.md); scoring lives in [`evaluation.md`](evaluation.md).

## Contents

- [Connecting](#connecting)
- [Loading a dataset](#loading-a-dataset)
- [Profiling — `dataset_profiler`](#profiling--dataset_profiler)
- [Dataset properties — the documented path](#dataset-properties--the-documented-path)
- [Recommender — the documented path](#recommender--the-documented-path)
- [Preprocessing](#preprocessing)
- [Training](#training)
- [The Model Store](#the-model-store)
- [Generating](#generating)
- [Shaping the output](#shaping-the-output)
- [Workflow mechanics](#workflow-mechanics)

## Connecting

```python
import rockfish as rf
import rockfish.actions as ra
import rockfish.labs as rl

conn = rf.Connection.from_config()   # ~/.config/rockfish/config.toml
conn = rf.Connection.from_env()      # ROCKFISH_API_KEY / _API_URL / _PROJECT_ID / _ORGANIZATION_ID
```

## Loading a dataset

```python
dataset = rf.Dataset.from_csv("finance", "finance.csv")
dataset = rf.Dataset.from_pandas("finance", df)
dataset = rf.Dataset.from_table("finance", pyarrow_table)
dataset = rf.Dataset.from_parquet("finance", "finance.parquet")
dataset = await rf.Dataset.from_id(conn, "1X7gaBHH2ikuariu0uL9HG")   # remote
df = dataset.to_pandas()
```

**`rf.Dataset(...)` raises `RuntimeError` by design** — it is a factory, not a constructor. Every entry point is a `from_*` function. Each takes an optional third `table_metadata` argument.

A `LocalDataset` wraps a PyArrow table (`dataset.table`). For session-aware metrics later, attach table metadata:

```python
dataset = dataset.with_table_metadata(rf.TableMetadata(metadata=["customer", "age", "gender"]))
```

To narrow the source before training, `dataset.sync_sql(query)` runs a local SQL filter:

```python
dataset = dataset.sync_sql("SELECT * FROM my_table WHERE amount BETWEEN 0 AND 1000.0")
```

## Profiling — `dataset_profiler`

The current recommended path. `profile_table` and `recommend` need only PyArrow; the builder functions import `rockfish.actions` lazily.

```python
from rockfish.labs.dataset_profiler import (
    profile_table, recommend, build_train_workflow, build_generate_workflow,
    preprocess_actions, build_encoder, train_action, generate_action,
    detect_state_fields, decode_constraints,
)

profile = profile_table(dataset.table, name="finance")   # accepts a pandas DataFrame too
config = recommend(profile, session_hints={"session_key": "customer"})
```

### `TableProfile`

| Attribute | Meaning |
| --- | --- |
| `name`, `row_count`, `columns` | `columns` is a list of `ColumnStat` |
| `timestamp_candidates`, `session_candidates` | detected, ordered |
| `session_lengths` | `{column: SessionLengthStats}` for the top candidates |
| `estimated_cells`, `estimated_bytes`, `requires_chunking` | sizing, drives the chunk plan |
| `state_field_candidates` | detected state-machine columns |
| `warnings` | |
| `.column(name)` | lookup by name |

`ColumnStat`: `name, dtype, kind, is_nullable, distinct_count, null_count, null_fraction, min_value, max_value, avg_str_len, looks_like_json_array, sample_values, avg_bytes, is_epoch_timestamp, epoch_unit`. `kind` is `numeric` / `string` / `timestamp` / `boolean` / `other`.

`SessionLengthStats`: `column, sessions, min_len, p50, p95, max_len, mean`, plus `.skew` (`max_len / p50`) and `.archetype`:

| archetype | skew | Meaning |
| --- | --- | --- |
| `panel` | < 2 | fixed-length sessions — no padding waste, larger batches are free |
| `alarm` | ≥ 10 | heavily skewed — large batches waste most of every batch on padding |
| `mixed` | 2–10 | conservative middle; alarm-ish defaults |

### `RecommendedConfig`

| Attribute | Meaning |
| --- | --- |
| `decision` | `time_gan` / `tab_gan` / `time_ssm` / `tab_ssm` / `time_rtf2` / `tab_rtf2` / `time_transformer` / `tab_transformer` / `refuse` |
| `rule_fired` | which rule decided (`R0`–`R5`, or `refuse`) |
| `refusal_reason` | set when `decision == "refuse"` |
| `drop_columns` | columns the D-rules removed |
| `fill_strategies` | `{column: "zero" \| "forward" \| "backward" \| "empty" \| "drop"}` |
| `metadata_columns`, `session_column`, `timestamp_column` | the time-model roles |
| `output_count`, `train_sample_rows` | |
| `output_max_length`, `sub_session_chunk_size`, `max_rows_per_session` | derived from the session-length archetype and a token budget |
| `session_archetype` | `panel` / `alarm` / `mixed` |
| `chunk_plan` | a `ChunkPlan` |
| `timestamp_prep` | `{column: SQL}` for epoch-numeric timestamps |
| `categorical_cast_columns` | low-cardinality numerics to cast to VARCHAR |
| `relational_max_chunks`, `estimated_child_row_tokens` | |
| `state_field_constraints` | detected transition maps, **for the user to confirm** |
| `warnings`, `notes` | surface both |

### The drop rules

`recommend()` removes columns before anything else. Always show `config.drop_columns` and `config.notes` to the user — this is where a needed column quietly disappears.

| Rule | Drops |
| --- | --- |
| D1 | ≥ 200 rows and > 95% distinct strings/numerics — identifiers; recreate post-hoc as UUIDs. **Watch this one on continuous columns**: a float sensor reading or a monotone counter is near-unique by nature, so real measurements get caught by a rule meant for IDs (the note even calls them "high-cardinality unique string"). Worse, the drop feeds the continuous-measurement count that [model routing](models.md#choosing-a-model) uses, so it can silently change which model you are told to train |
| D2b | name matches an identifier/audit-reference pattern, **at any cardinality** — a foreign key repeats by nature, so a ratio test structurally cannot catch it |
| D2 | name looks like an ID and > 50% distinct |
| D3 | constant (≤ 1 distinct) — reattach post-hoc |
| D4 | 100% null, **or** ≥ 50% null when the default fill is directional. A forward/backward fill *fabricates* signal across a sparse column; a zero/empty fill merely *states* that null means absence, so scalar-filled columns are kept however sparse |
| D5 | known raw-text column names with average length > 200 |
| D6 | JSON-array column with > 30% distinct — needs preprocessing |

The session and timestamp columns are put back if a D-rule caught them and the data takes the time path.

### State-machine detection

```python
candidates = detect_state_fields(dataset.table, session_key="pod", order_by="timestamp")
for c in candidates:
    print(c.field, c.n_values, c.transition_map, c.session_coverage,
          c.is_monotone_counter, c.is_censored_accumulator)

constraints = decode_constraints(candidates)   # {field: {value: [allowed next]}}
```

Detection has hard thresholds, and a column that misses one is simply absent from the result — there is no warning. A candidate must have **≥ 200 rows**, **2–20 distinct values**, transitions in **≥ 2 distinct sessions**, and a **self-transition rate ≥ 0.95**. That last one is the surprising one: a state field has to be *sticky*. A categorical that changes on most rows is a churning attribute, not a lifecycle, and is not detected however state-like it looks.

Each `StateFieldCandidate` carries the measured transition structure plus signals for how to present it:

- `is_monotone_counter` — every off-diagonal transition strictly increases a number (restart counts, violation counts). A legitimate constraint (3 → 1 is impossible) but a counter, not a lifecycle.
- `is_censored_accumulator` — the value drifts and nothing absorbs, so the "endings" are observation artifacts, not completions. Still a valid decode constraint, but do not present it as a lifecycle.
- `session_coverage` / `transition_breadth` — a field can be present everywhere but move in only a handful of sessions.
- `absorbing_mass` / `end_drift_rate` / `terminal_reuse` — whether the process genuinely completes or was simply open when the window closed.

Feed confirmed maps to `sampling.state_constraints` on an SSM time generate. A sampled graph can miss rare legal edges, so have the user confirm before enforcing.

### From recommendation to workflow

```python
builder = build_train_workflow(dataset, profile, config, model_labels={"run": "v1"})
workflow = await builder.start(conn)
```

`build_train_workflow` assembles `dataset-load → [SQL projection: drops + scalar fills + casts] → [Transform fill pairs] → [ChunkTimeSSM] → train`. The individual pieces are also public: `preprocess_actions(profile, config)`, `build_encoder(profile, config)`, `train_action(profile, config)`, `generate_action(config, output_count=...)`.

```python
builder = build_generate_workflow(config, model, output_count=5000)
```

Two things `build_train_workflow` does *not* do: it never applies `max_rows_per_session` (there is no SDK field for it — cap upstream if needed), and it does not implement the N-chain row/session-range chunk split, which is loader-side. The chunk plan's pre-chunker flag *is* honored: for a large `time_ssm` table it keeps a single training chain and prepends a `ChunkTimeSSM` instead of splitting into N chains.

## Dataset properties — the documented path

A separate subsystem from the profiler. It produces a richer typed description and supports PII and association-rule detection.

```python
from rockfish.labs.dataset_properties import (
    DatasetPropertyExtractor, DatasetType, EncoderType, FieldType,
    CategoricalFieldProperties, ContinuousFieldProperties, FieldProperties,
    AssociationRule, ColumnRule, get_dataset_config,
)

props = DatasetPropertyExtractor(
    dataset,
    dataset_type=DatasetType.TIMESERIES,     # inferred if omitted
    metadata_fields=["age", "gender"],
    session_key="customer",
    timestamp="timestamp",
    additional_property_keys=["association_rules", "pii_type"],
).extract()
```

Anything you pass is taken as given; the rest is detected consistently around it.

A `TabularDatasetProperties` carries `dataset_type`, `field_properties`, `metadata_fields`, `n_rows`, `n_cols`. A `TimeseriesDatasetProperties` adds `measurement_fields`, `timestamp`, `session_key`, `n_sessions`, `avg_session_len`, `max_session_len`.

`field_properties` maps each name to a `FieldProperties` subclass:

```python
props.field_properties["age"]
# CategoricalFieldProperties(_dtype=int32, _original_etype=categorical,
#   col_position=0, etype=categorical, ndim=5, pii_type=UNDETECTED)

props.field_properties["amount"]
# ContinuousFieldProperties(..., ndim=1, min_value=1.25, max_value=133.37, pii_type=UNDETECTED)
```

`additional_property_keys` options are `"association_rules"` (fills `props.rules` with detected `AssociationRule`s over categorical fields) and `"pii_type"` (fills each field's `pii_type`).

`props.filter_fields(ftype=FieldType.MEASUREMENT, etype=EncoderType.CATEGORICAL)` selects by role and encoding.

To update properties:

```python
# start from the existing object — the named property is replaced
updated = DatasetPropertyExtractor.from_existing(
    props, rules=[AssociationRule(field_names=["age", "dob"])]
).extract()

# or rebuild, so the change propagates to dependent properties
new = DatasetPropertyExtractor(dataset, field_properties=edited, ...).extract()
```

`get_dataset_config(props, keep_session_keys=False)` converts a `TimeseriesDatasetProperties` into an encoder `DatasetConfig`. **RF-Time-GAN only** — it emits `type="session"` for the session key (or `"categorical"` when `keep_session_keys=True`), and raises if the session key is not categorical.

## Recommender — the documented path

`rockfish.labs.steps.Recommender` knows the four legacy models only (`ModelType.TAB_GAN`, `TAB_TRANSFORMER`, `TIME_GAN`, `TIME_TRANSFORMER`). For SSM or rtf2 routing use `dataset_profiler.recommend` instead.

```python
from rockfish.labs.steps import Recommender, HandleMissingValues, HandleAssociatedFields, HandlePiiFields, ModelSelection, ModelType

out = Recommender(props).run()
print(out.report)                 # human-readable description of what it decided
builder = rf.WorkflowBuilder()
builder.add_path(dataset, *out.actions, ra.DatasetSave(name="synthetic"))
workflow = await builder.start(conn)
```

Restrict it to specific steps:

```python
out = Recommender(props, steps=[HandleMissingValues()]).run()
out = Recommender(props, steps=[ModelSelection(model_type=ModelType.TIME_GAN)]).run()
```

Steps available: `HandleMissingValues` (emits `FillNull` actions), `HandleAssociatedFields` (join/split for dependent fields), `HandlePiiFields`, `ModelSelection`.

## Preprocessing

Whatever produced the recommendation, preprocessing is ordinary actions between load and train.

**Column drops, scalar fills, and casts** fold into one SQL projection — one action, not one per column:

```python
sql = ra.SQL(ra.SQL.Config(
    query='SELECT "a", COALESCE("b", 0) AS "b", CAST("phase" AS VARCHAR) AS "phase" FROM my_table',
    table_name="my_table",
))
```

**Directional fills** become `Transform` actions, and must be emitted in **pairs** — a forward fill cannot fill a leading null run, and the surviving null reaches the encoder as `NaT`, which is not in the codec vocabulary:

```python
from rockfish.actions.apply_transform import Field, FillNull, FillNullForward, FillNullBackward

ra.Transform(ra.Transform.Config(function=FillNullForward(Field("ts"))))
ra.Transform(ra.Transform.Config(function=FillNullBackward(Field("ts"))))
```

**These are session-blind — do not use them on sessionized data.** `FillNullForward` calls `pyarrow.compute.fill_null_forward` over the whole column with no notion of sessions, so a session whose first row is null inherits the **previous session's last value**. Measured on a 160-session fixture with 12% nulls: 27 sessions begin with a null and 30 rows differ from a per-session fill. `dataset_profiler.preprocess_actions` emits these same Transforms, so the recommended path has the same property.

For a sessionized column, do the fill in the SQL projection with a window partitioned by the session key:

```sql
SELECT ..., COALESCE(qd_fwd, qd_bwd) AS queue_depth
FROM (
  SELECT ...,
         LAST_VALUE("queue_depth" IGNORE NULLS) OVER (
           PARTITION BY session ORDER BY ts
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)  AS qd_fwd,
         FIRST_VALUE("queue_depth" IGNORE NULLS) OVER (
           PARTITION BY session ORDER BY ts
           ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)  AS qd_bwd
  FROM my_table
)
```

The `COALESCE` is the pair: `LAST_VALUE` is the forward pass, `FIRST_VALUE` the backward one that covers a leading null run.

**Epoch-numeric timestamps** need a cast before they can serve as the time axis — `config.timestamp_prep` gives you the expression, e.g. `to_timestamp_seconds(CAST("ts" AS BIGINT))`.

**Low-cardinality numerics that are really states** need a VARCHAR cast, from `config.categorical_cast_columns`. A pod phase stored as `1.0 / 2.0 / 3.0` otherwise trains as continuous and generates `2.37`.

### Transform reference

| Need | Action | Inverse |
| --- | --- | --- |
| Drop columns, scalar fills, casts, arbitrary projection | `ra.SQL` | — |
| Directional null fill | `ra.Transform` + `FillNullForward` / `FillNullBackward` | — (emit in pairs) |
| Scalar null fill | `ra.Transform` + `FillNull` / `FillNullAggregation` | — |
| Remove columns outright | `ra.DropFields` | — |
| Categorical → integer codes | `ra.LabelEncode` | `ra.LabelDecode` |
| Heavy right skew spanning orders of magnitude (bytes, counts, latency) | `ra.LogEncode` — `log1p`, rounds to 3 dp — see below | `ra.LogDecode` — `expm1` |
| **Bounded numeric (capped by a constant or another column)** | `ra.SQL` logit projection — see below | `ra.SQL` sigmoid projection |
| Arithmetic on existing columns, in place | `ra.Transform` + `Add` / `Subtract` / `Multiply` / `Divide` / `Cast` | — |
| Arithmetic into a **new** column | `ra.Apply(function=…, append_field=…)` | — |
| Value recode | `ra.Transform` + `Remap` | — |
| Combine or split a column | `ra.JoinFields` / `ra.SplitField` | each other |
| Subset rows / add a train-test split column | `ra.Sample`, `ra.SampleLabel` | — |
| Timestamp arithmetic | `ra.AlterTimestamp`, `ra.AddDuration`, `ra.SubtractTimestamp` | — |
| Nested structures | `ra.Flatten` / `ra.Unflatten` | each other |
| Fix dtypes | `ra.CoerceDtypes` | — |

`ra.Transform` replaces a field in place; `ra.Apply` appends a new one. The `Function` set is fixed (`FillNull`, `FillNullForward`, `FillNullBackward`, `FillNullAggregation`, `Interarrival`, `JoinFields`, `Remap`, `Cast`, `Add`, `Subtract`, `Multiply`, `Divide`) — **there is no `Ln` or `Exp` function**, so anything transcendental goes through `ra.SQL`.

### Numeric columns whose scale fights the model

Two preprocessing cases share one root cause: **the model never sees your units.** Continuous fields are normalised into a fixed range before training and denormalised after, so a column whose values live at a different scale from the model's working range either breaks its own rules or collapses into a single indistinguishable value. Both are fixed by training in a transformed space and inverting afterwards, and the transform is chosen by one question:

| Does the column have a ceiling? | Transform | Inverse | Why |
| --- | --- | --- | --- |
| **Yes** — capped by a constant or another column | `logit(v / cap)` | `cap × sigmoid(z)` | the model's output domain becomes the whole real line, so no emitted value can break the bound |
| **No**, but it spans orders of magnitude | `log1p(v)` | `expm1(z)` | compresses the dynamic range so small values stay distinguishable from each other |

### Bounded numeric columns

A column is **bounded** when its valid range is capped — by a constant (`cpu_pct` in [0, 100]) or by another column in the same row (`usage` ≤ `capacity`, `used_seats` ≤ `licensed_seats`, `paid_amount` ≤ `invoice_total`).

Models emit unconstrained reals. Trained on the raw column, they generate 103% CPU and disks 1.3× full. Clipping afterwards — `clip_in_range` or a SQL filter — "fixes" it but piles probability mass exactly on the boundary, which then shows up in evaluation as a spike the real data does not have. **Train in logit space and invert with a sigmoid instead**, so the bound holds by construction.

**Detection is manual.** Neither `dataset_profiler` nor `DatasetPropertyExtractor` detects bounded columns — `ColumnStat.min_value` / `max_value` give you the observed range and nothing more. Flag a column yourself when:

- values sit in [0, 1] or [0, 100] and the name reads as a ratio, rate, percent, utilization, or fraction;
- every row satisfies `col_a <= col_b` for some other numeric column and the pair is semantically part/whole;
- the user's schema or domain notes declare a maximum.

**Confirm the cap with the user before transforming.** An observed maximum is not a ceiling: `max(response_time)` in a sample is just the slowest request seen.

**Forward, before training** — one `ra.SQL` projection ahead of the train action:

```python
EPS = 1e-6
encode = ra.SQL(query=f"""
    SELECT *,
           ln(ratio / (1 - ratio)) AS usage_logit
    FROM (
      SELECT *,
             least(greatest("usage" / "capacity", {EPS}), 1 - {EPS}) AS ratio,
             CASE WHEN "usage" <= 0          THEN 'zero'
                  WHEN "usage" >= "capacity" THEN 'one'
                  ELSE 'interior' END        AS usage_bucket
      FROM my_table
    )
""")
```

Train on `usage_logit` (continuous) and `usage_bucket` (categorical); mark the raw `usage` column `type="ignore"` in the encoder so the model never sees it.

**Inverse, after generation** — the mirror projection, the way `ra.LogDecode` pairs with `ra.LogEncode`:

```python
decode = ra.SQL(query="""
    SELECT *,
           CASE WHEN usage_bucket = 'zero' THEN 0.0
                WHEN usage_bucket = 'one'  THEN "capacity"
                ELSE "capacity" / (1 + exp(-usage_logit)) END AS "usage"
    FROM my_table
""")
```

`logit` maps (0, 1) onto the whole real line, so the model's natural output domain now matches the data's, and `sigmoid` maps any real — however far out of distribution — back inside. No clipping and no rejection sampling. A second benefit: logit *stretches* the region near the walls, so disks at 0.97 / 0.98 / 0.99 occupy 1.1 units of logit space instead of 2% of the raw range, and the model can resolve "nearly full" rather than smearing it.

**The endpoints are mandatory handling, not a detail.** `logit(0) = -inf` and `logit(1) = +inf`, and real data hits both — fresh volumes read 0%, and the disk that paged on-call reads exactly 100%. Unhandled, this produces `NaN` during fit. Two options:

| Approach | When |
| --- | --- |
| **ε-clamp** — `least(greatest(p, eps), 1 - eps)`, eps ≈ 1e-6 | the endpoints are rare measurement artifacts |
| **Zero/one-inflation** — model `p == 0` and `p == 1` as their own discrete outcomes and fit logit only to the open interior | **prefer this whenever an endpoint is semantically meaningful.** 100%-full is the event the customer onboarded the data to reproduce; collapsing it into the tail destroys exactly that signal |

The projection above does both: it clamps for numerical safety *and* carries `usage_bucket` so the inflated endpoints survive. That matters because `capacity * sigmoid(z)` is open on both ends — it can never equal exactly 0 or exactly `capacity`. If the synthetic data must contain literal boundary values, inflation is required, not optional.

**Ordering when the cap is another column.** The transform is conditional on the cap, so: carry through or generate `capacity` first, generate `z`, then reconstruct `usage` from both. If `capacity` is itself synthesized, the pair must be modeled jointly — an independently sampled capacity breaks the very correlation with usage that made the constraint meaningful.

**Tell the user what shape to expect.** With Gaussian `z`, the reconstructed column is **logit-normal**, which is bimodal for larger sigma — mass pushed toward both walls. That is correct behavior, not a fitting failure, but it surprises anyone expecting a bell curve around the mean.

Evaluation changes too — see [`evaluation.md`](evaluation.md#bounded-columns).

### Heavy-tailed unbounded columns

A column with **no ceiling but an enormous dynamic range** — bytes transferred, request counts, per-step increments — has the opposite problem. Nothing is violated; instead the whole distribution collapses.

**The reason is normalisation, not the model.** The model does not see bytes; it sees the column normalised into a fixed range. Measured on a real Kubernetes pod-network dataset, per-step increments of `k8s_pod_network_io`:

| Percentile | Raw bytes | Normalised raw | Normalised `log1p` |
| --- | --- | --- | --- |
| p50 | 0 | 0.000000 | 0.000 |
| p90 | 1,664,715 | 0.000588 | 0.658 |
| p99 | 11,035,023 | 0.003895 | 0.745 |
| p99.9 | 40,528,674 | 0.014304 | 0.805 |
| max | 2,833,386,394 | 1.000000 | 1.000 |

A single 2.8 GB outlier sets the scale, so **99.895% of all increments land below 0.01**. A 1.6 MB step and an 11 MB step are the same number to the model, and both are indistinguishable from zero. After `log1p` they sit 0.09 apart. The model is not failing to learn the distribution — it is being handed a column in which the distribution has already been destroyed.

**`log1p`, not `log`.** `log1p(0) = 0`, so exact zeros stay representable; plain `log` gives `-inf` and poisons the fit. That matters more than it sounds: in that dataset **60% of increments are exactly zero**, which is signal — the pod sent nothing that step — not missing data.

The SDK pair already does this:

```python
builder.add_path(dataset, ra.LogEncode(field="bytes_sent"), train)
# ... after generation
builder.add_path(model, generate, ra.LogDecode(field="bytes_sent"), save)
```

`LogEncode` applies `log1p`, **rounds to 3 decimal places, and casts to float32**; `LogDecode` applies `expm1` and casts back to the original dtype, rounding to `field_ndigits` (default 3). The rounding is a real precision ceiling — about 0.1% relative resolution on the raw value — which is harmless for traffic counters and not harmless for a column where the low-order digits carry meaning. `LogDecode` recovers the original dtype from a `log-encode/N` entry that `LogEncode` pushes onto the Arrow schema metadata. That entry survives train, generate and save, so decoding in a **later, separate workflow** works — verified: a generated dataset carried `log-encode/1` and decoded `20.799` back to `1.08e9`. What does break it is a **pandas round trip**: `to_pandas()` / `from_pandas()` drops schema metadata, and `LogDecode` then fails with `TableMetadataKeyError: table metadata key not found: log-encode/1`.

Use a `ra.SQL` projection instead when you need the transform without the rounding, or when the column is a **counter**: model the per-step increment rather than the level, so `log1p(diff)` on the way in and `cumsum(expm1(...))` on the way out, which also keeps the reconstructed series monotone.

**Do not stack the two transforms casually.** A quantity that is both unbounded in aggregate and bounded per part — a memory total and its components — is handled by transforming the envelope with `log1p` and each dependent as `logit(part / envelope)`, so the parts stay under the total that was generated for them.

## Training

```python
builder = rf.WorkflowBuilder()
builder.add_dataset(dataset)
builder.add_action(train, parents=[dataset])
workflow = await builder.start(conn)
print(f"Workflow: {workflow.id()}")

async for log in workflow.logs():
    print(log)

model = await workflow.models().last()
```

Or, with the chained form:

```python
builder = rf.WorkflowBuilder()
builder.add_path(dataset, *preprocess, train)
workflow = await builder.start(conn)
await workflow.wait(raise_on_failure=True)
```

The transformer, rtf2, and SSM train actions run `check_table()` client-side before the workflow is submitted, so config/data mismatches raise `ActionConfigError` locally instead of failing remotely. **The two GAN actions (`TrainTimeGAN`, `TrainTabGAN`) do not implement it** — validate their encoders against `dataset.table.column_names` yourself, or the mistake only surfaces as a remote failure.

Stamp labels at train time so the model is findable later:

```python
train = ra.TrainTimeSSM(ra.TrainTimeSSM.Config(encoder=encoder, model_labels={"source": "finance", "v": "3"}))
```

## The Model Store

Every trained model is persisted and queryable.

```python
model = await workflow.models().last()       # or .nth(0), or .collect()
model = await rf.Model.from_id(conn, "59XvuKcM9t5gqRXdIkiZdp")

model = await model.add_labels(conn, source="finance", v="3")   # merge
model = await model.set_labels(conn, workflow_id="59Xvu...")    # replace
image = await model.download_model_image()
```

Query the store — all labels given must match:

```python
async for model in conn.models(labels={"source": "finance"}):
    print(model)

async for model in conn.models(after="2026-08-20T02:35:30Z"):
    ...
async for model in conn.models(after=datetime.now(timezone.utc) - timedelta(hours=6)):
    ...
async for model in conn.models(labels={"source": "finance"},
                               after="2026-08-20T02:35:30Z",
                               before="2026-08-20T02:45:30Z"):
    ...
```

Time filters accept RFC3339 strings, timezone-aware `datetime`s, or relative times.

**`conn.models()` and `conn.datasets()` default to `limit=10`.** Published examples iterate them as if they returned everything. Pass `limit=` explicitly whenever the answer matters; they also take `order=` (`Order.DESCENDING` by default).

To generate from a stored model in a fresh workflow, use `ra.ModelLoad`:

```python
load = ra.ModelLoad(ra.ModelLoad.Config(model_id="59XvuKcM9t5gqRXdIkiZdp"))
builder.add_action(load)
builder.add_action(generate, parents=[load])
```

## Generating

```python
generate = ra.GenerateTimeGAN(ra.GenerateTimeGAN.Config(doppelganger=ra.GenerateTimeGAN.DGConfig()))
target = ra.SessionTarget(target=30_000)
save = ra.DatasetSave(name="synthetic")

builder = rf.WorkflowBuilder()
builder.add_model(model)
builder.add_action(generate, parents=[model, target])
builder.add_action(target, parents=[generate])
builder.add_action(save, parents=[generate])
workflow = await builder.start(conn)

syn = await workflow.datasets().concat(conn)
```

### `SessionTarget`

| Field | Default | Notes |
| --- | --- | --- |
| `target` | `None` | generate until this many sessions (time) or records (tabular). `None` → match the training-set size |
| `max_cycles` | `20` | 1–1000; hitting it logs a warning and stops |
| `use_match_count` | `False` | count filter matches rather than sessions — use when a filter sits between generate and target |

The double edge is the mechanism, not a mistake: `generate` feeds `target`, `target` feeds `generate`. Each cycle `SessionTarget` counts what arrived, subtracts from the target, and requests the shortfall. It groups by the table metadata's session field (or group fields, or metadata) and falls back to row count.

**The config's own `sessions` / `records` field only *reduces* output below the default cap.** To exceed the source size you need `SessionTarget`. Defaults with neither: `min(1000, source sessions/records)`, or `min(23_000, source rows)` for RF-Tab-GAN.

For SSM time models, `ra.add_sharded_generate` is usually better than a `SessionTarget` loop — see [`models.md`](models.md#sharded-generation).

### `DatasetSave`

| Field | Default | Notes |
| --- | --- | --- |
| `name` | required | pass as a keyword: `ra.DatasetSave(name="synthetic")` |
| `concat_tables` | `True` | concatenates upstream outputs and offsets each shard's `session_key` into a contiguous range |
| `concat_session_key` | `None` | |
| `dataset_labels` | `{}` | |
| `drop_default_session_key` | `False` | |
| `drop_fields` | `[]` | |

## Shaping the output

### Conditional generation on metadata — RF-Time-GAN only

```python
generate = ra.GenerateTimeGAN(ra.GenerateTimeGAN.Config(
    doppelganger=ra.GenerateTimeGAN.DGConfig(
        given_metadata={"age": ["2", "3"], "gender": ["F"]}
    )
))
```

Generates sessions with those metadata combinations using patterns learned from the training data — including combinations that were scarce or absent in the source.

### Logits biasing — SSM and rtf2

```python
sampling = ra.GenerateTabSSM.SamplingConfig(
    bias_dict={"label": {"anomaly": 5.0}, "proto": {"tcp": 1.0, "udp": -1.0}}
)
```

An additive bias on the tokens encoding a categorical value. A soft nudge; ~30 is a near-hard pin. Do not use ±inf — it does not survive config JSON.

### Rare events — filter and top up

Generate, filter to the condition, and let `SessionTarget` keep generating until enough *matching* rows exist.

```python
condition_filter = ra.PostAmplify({"query_ast": {"eq": ["fraud", 1]}})
# or
condition_filter = ra.SQL(query="SELECT * FROM my_table WHERE fraud = 1")

target = ra.SessionTarget(target=100)

builder = rf.WorkflowBuilder()
builder.add_model(model)
builder.add_action(generate, parents=[model, target])
builder.add_action(condition_filter, parents=[generate])
builder.add_action(target, parents=[condition_filter])      # target counts MATCHES
builder.add_action(save, parents=[condition_filter])
```

Note the target's parent is the *filter*, not the generate — that is what makes the loop count matches. `PostAmplify.Config` also takes `group_aggregations`, `drop_match_percentage` (default 0.0), and `drop_other_percentage` (default 1.0). Works with all model families.

For several conditions at different volumes, run one chain per condition and concatenate.

### Enforcing a distribution — `ra.Replace`

`Replace(field, condition, resample=None, seed=None)`. Conditions pick which rows to replace; resamples say what to put there.

Equal representation across a field's values:

```python
replacement = ra.Replace(
    field="flavor",
    condition=ra.EqualizeCondition(equalization=True),
)
```

Business-rule enforcement — a SQL condition returns a `mask` column, and matching rows are resampled from a fixed value set:

```python
replacement = ra.Replace(
    field="color",
    condition=ra.SQLCondition(query="SELECT door = 'rear' AND color != 'black' AS mask FROM my_table"),
    resample=ra.ValuesResample(replace_values=["black"]),
)
```

Other conditions: `ra.TopKCondition(top_k=N)` (keep the N most frequent, replace the rest), `ra.ThresholdCondition(threshold=f)` (replace values below a frequency share, 0–1). Other resample: `ra.SQLResample(query=...)` returning `values` and `weights` columns.

### Out-of-range values

`clip_in_range=True` on RF-Tab-GAN generation, or `clip_per_sample=True` on RF-Time-GAN, keeps continuous values inside the training range — at the cost of piling mass on the boundaries. For any model, filtering after the fact avoids that distortion:

```python
syn = syn.sync_sql("SELECT * FROM my_table WHERE amount BETWEEN 0 AND 1000.0")
```

## Workflow mechanics

```python
builder = rf.WorkflowBuilder()
builder.add_dataset(dataset)                  # source
builder.add_model(model)                      # source
builder.add_action(action, parents=[...])     # node
builder.add_path(dataset, a, b, c)            # linear chain
builder.add_labels(...)
builder.worker_group(...)
print(builder.mermaid())                      # render the graph before submitting

workflow = await builder.start(conn)
```

`WorkflowBuilder.from_workflow(workflow)` reopens an existing graph; `remove`, `reparent`, and `get_action` edit it.

```python
workflow.id()
async for log in workflow.logs(): ...
await workflow.wait(raise_on_failure=True)
await workflow.stop()

syn = await workflow.datasets().concat(conn)          # one concatenated LocalDataset
async for remote in workflow.datasets():              # or stream them
    ds = await remote.to_local(conn)
model = await workflow.models().last()
models = await workflow.models().collect()
```

Streams support `.take(n)`, `.filter(fn)`, `.nth(i)`, `.last()`, `.collect()`, and `.tqdm()` / `.notebook()` for progress.

Saved datasets are also queryable from the connection, the same way models are:

```python
async for ds in conn.datasets(labels={"kind": "synthetic"}):
    ...
```
