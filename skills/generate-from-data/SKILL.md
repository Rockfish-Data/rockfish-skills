---
name: generate-from-data
description: Generate synthetic data from real data by training a Rockfish model on it, then scoring the result and iterating. Use when a user has actual data (a CSV, a Snowflake table, a DataFrame) and wants a synthetic stand-in that reproduces its distributions, correlations, and session structure — as opposed to inventing rows from a schema. Covers the whole loop: profiling and property extraction, choosing quality metrics before training, preprocessing, model selection across the GAN / transformer / SSM families, encoder and hyperparameter configuration, train and generate workflows, the Model Store, conditional and amplified generation, and fidelity / privacy / downstream-utility evaluation including the RFScore report card. Trigger on phrases like "synthetic version of this table", "train a model on my data", "fit a generative model", "make fake data that looks like this", "RF-Time-GAN", "DoppelGANger", "CTGAN", "REaLTabFormer", "TrainTimeSSM", "model store", "fidelity score", "RFScore", "is my synthetic data any good".
---

# Generate from data

Turn a real table into a synthetic one that behaves like it by training a generation model and use it to generate data, score the output, and change something — until the score clears the bar or you can say why it never will.

## When to use this skill

Use when the user **has sufficient data** and wants a synthetic stand-in for it — sharing without exposing records, amplifying a rare class, stress-testing at 100× volume, or standing up a realistic dev fixture.

Use a different skill when:

- The user has **no data or insufficient data** and wants rows invented from a described structure → `generate-from-schema`.
- The user has a **baseline time series** and wants anomalies perturbed into it → `inject-incidents`.
- The source table lives in **Snowflake** and needs exploring or loading first → `snowflake-analyst`, then come back here.

## The loop

This is not a pipeline that runs once. It is a loop, and the loop is the skill:

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                        (6) says which step to fix                    │
   ▼                                                                      │
1. ANALYZE ──► 2. SET THE ──► 3. PREPARE ──► 4. TRAIN ──► 5. GENERATE ──► 6. EVALUATE
   profile        TARGET         drops          encoder       volume         score vs.
   properties     CardSpec       fills          model         shaping        the target
   session key    noise floor    casts          hyper-        conditions     from step 2
   state fields   which dims     bounded cols   parameters                   diagnose
   bounds         the gate
```

**Step 2 is the one people skip, and it has to come before step 4.** You need to define the target first, often after analyzing the data, before the preparation step all the way to generate the data. Three concrete reasons it belongs up front:

- `noise_floor()` needs **only the real data**. Run it before you train anything. It tells you the ceiling, and sometimes it tells you the data cannot support the ask at all.
- `CardSpec.from_profile()` is built from the step-1 profile. Analysis produces the evaluation spec and the training config *from the same object*, so they cannot drift apart.
- The SSM and rtf2 trainers take `quality_check.min_score` (default **0.85**) as a training input and stop when they reach it. That is the same 0.85 the report card gates on. The metric is not a postscript; it is a hyperparameter.

Steps 3–5 are Rockfish **workflows** — a `WorkflowBuilder` graph submitted to the backend. Steps 1, 2, and 6 are local.

**One turn of the loop costs a training run.** Before spending another, exhaust the cheap moves: step 6 tells you which step owns the failure, and the answer is more often 1 or 3 than 4. See [Evaluate and diagnose](#6-evaluate-and-diagnose).

## 1. Analyze

```python
import rockfish as rf
import rockfish.actions as ra
import rockfish.labs as rl

conn = rf.Connection.from_config()          # ~/.config/rockfish/config.toml
dataset = rf.Dataset.from_csv("finance", "finance.csv")
```

`Connection.from_config()` allows the user to store Rockfish configurations of different profiles in one place, out of the current repo. Alternatively, `Connection.from_env()` reads from the environment variables, `ROCKFISH_API_KEY` / `ROCKFISH_API_URL` / `ROCKFISH_PROJECT_ID` / `ROCKFISH_ORGANIZATION_ID` instead. 

Two subsystems answer the same questions (which column is the session key, which is the timestamp, which fields are categorical vs continuous). **Pick one and stay in it.**

| | `rockfish.labs.dataset_profiler` | `rockfish.labs.dataset_properties` + `labs.steps.Recommender` |
| --- | --- | --- |
| Entry point | `profile_table(table)` → `recommend(profile)` | `DatasetPropertyExtractor(ds).extract()` → `Recommender(props).run()` |
| Routes to | every current model family, incl. SSM and rtf2 | the four legacy models only |
| Also gives you | drop rules, fill strategies, chunk plan, state-machine detection, token-budgeted `output_max_length` | association rules, PII detection, `FillNull` / dependent-field steps |
| Emits | a configured `WorkflowBuilder` via `build_train_workflow()` | a list of actions via `recommender_output.actions` |
| Use it when | you want the current recommended path, or the data is long-session / stateful / large | you need PII or association-rule handling, or you are following the published docs |

The profiler is the default. It is the newer engine and the only one that knows about the SSM family.

```python
from rockfish.labs.dataset_profiler import profile_table, recommend, build_train_workflow

profile = profile_table(dataset.table, name="finance")
config = recommend(profile, session_hints={"session_key": "customer"})
print(config.decision, config.rule_fired)   # e.g. "time_ssm" "R3"
print(config.drop_columns, config.warnings, config.notes)
```

Always show `config.decision`, `config.drop_columns`, and `config.warnings` to the user before going further — the recommender silently drops ID-like, constant, and mostly-null columns, and that is the single most surprising thing it does.

Also settle here, while you are looking at the data: **which columns are bounded** (capped by a constant or by another column) and **which are state machines**. Neither is auto-detected end to end, both change step 3, and finding them after a training run costs a full turn of the loop.

## 2. Set the target

Before training. Decide what "good" means, in code, and get the ceiling.

```python
from rockfish.labs.report_card import CardSpec, StateFieldSpec, noise_floor

spec = CardSpec.from_profile(profile, session_key="customer", timestamp="timestamp")
floor = noise_floor(dataset.to_pandas(), spec)     # real half vs real half
print(floor.summary())
```

`CardSpec.from_profile` carries the profiler's detected state fields and counters straight into evaluation, so the thing you measure is the thing you analyzed. Add `metadata_columns=` by hand.

**It silently drops the transition maps** — `from_profile` reads an attribute name the profiler does not define, so `legal_transitions` is always `None` and the card falls back to whatever the real sample happened to show. If the user confirmed a legal-transition map, set `StateFieldSpec(legal_transitions=...)` yourself; see [`reference/evaluation.md`](reference/evaluation.md#the-report-card).

Then write down three things:

| Decision | Why it has to be now |
| --- | --- |
| **The floor** | `noise_floor()` is what the real data scores against *itself*. It is never 1.0. Judging a run against 1.0 instead of the floor is the most common misreading of these numbers |
| **Which dimensions matter** | `RFScore = min(marginal, correlation, association)` and `TS score = min(session_length, autocorr, transition)`. If the use case lives or dies on transitions, say so now — a run can pass RFScore and be temporally meaningless |
| **The gate** | 0.85 by default, and it is also `quality_check.min_score` on the SSM and rtf2 trainers. Setting it here means step 4 stops at the bar instead of at an epoch count |

If the floor itself comes back low, stop and go back to step 1 — usually the session key is wrong, and no model will fix that.

## 3. Prepare

Drops, fills, and casts fold into **one** `ra.SQL` projection; directional fills become `ra.Transform` pairs. Full detail and the transform reference table are in [`reference/pipeline.md`](reference/pipeline.md#preprocessing).

The three that matter most, because nothing catches them downstream:

- **Bounded numeric columns** — capped by a constant or another column (`usage` ≤ `capacity`). Train on `ln(p/(1-p))`, invert with `capacity / (1 + exp(-z))`, so the bound holds by construction. Clipping instead piles mass on the boundary that step 6 then reports as a spike the real data lacks. See [bounded numeric columns](reference/pipeline.md#bounded-numeric-columns).
- **Float-coded state machines** — a phase stored as `1.0 / 2.0 / 3.0` trains as continuous and generates `2.37`. Cast to VARCHAR; `config.categorical_cast_columns` lists the candidates.
- **Epoch-numeric timestamps** — `config.timestamp_prep` gives the `to_timestamp_*(CAST(col AS BIGINT))` expression.

## 4. Train

Pick the model first. If you are overriding the recommender, pick by data shape, then by budget.

| Data | Model | Action | Notes |
| --- | --- | --- | --- |
| Time series, **≥ 3 continuous measurements** | RF-Time-GAN | `ra.TrainTimeGAN` | GAN handles floats natively; a transformer tokenizes each float into new vocabulary |
| Time series, many sessions (≥ 50), 4–500 rows each | **SSM** | `ra.TrainTimeSSM` | current premium default for session data |
| Time series, few sessions or very long sessions (> 500 rows) | RF-Time-GAN | `ra.TrainTimeGAN` | or `TrainTimeSSM` with `sub_session_chunk_size` |
| Time series, want an attention model | rtf2 | `ra.TrainTimeTransformerV2` | successor to `TrainTimeTransformer` |
| Tabular, ≥ 1000 rows with real categorical structure | **SSM** | `ra.TrainTabSSM` | current premium tabular default |
| Tabular, small (< 1000 rows) or almost all numeric | RF-Tab-GAN | `ra.TrainTabGAN` | fastest to train |
| Tabular, want an attention model | rtf2 | `ra.TrainTabTransformerV2` | successor to `TrainTabTransformer` |

**`TrainTabTransformer` / `TrainTimeTransformer` (rtf v1) are superseded by the V2 actions.** Prefer V2 in new code; keep v1 only to reproduce an existing model. `dataset_profiler` already maps a `tab_transformer` / `time_transformer` decision onto the V2 actions.

Every train action takes an **encoder config** (which field plays which role, and how each is encoded) and a **model config** (hyperparameters).

```python
train = ra.TrainTimeGAN(ra.TrainTimeGAN.Config(
    encoder=ra.TrainTimeGAN.DatasetConfig(
        timestamp=ra.TrainTimeGAN.TimestampConfig(field="timestamp"),
        metadata=[
            ra.TrainTimeGAN.FieldConfig(field="customer", type="session"),
            ra.TrainTimeGAN.FieldConfig(field="age", type="categorical"),
        ],
        measurements=[
            ra.TrainTimeGAN.FieldConfig(field="amount", type="continuous"),
            ra.TrainTimeGAN.FieldConfig(field="fraud", type="categorical"),
        ],
    ),
    doppelganger=ra.TrainTimeGAN.DGConfig(epoch=100, batch_size=64, sample_len=19),
    model_labels={"source": "finance", "iteration": "1"},
))

builder = rf.WorkflowBuilder()
builder.add_dataset(dataset)
builder.add_action(train, parents=[dataset])
workflow = await builder.start(conn)
await workflow.wait(raise_on_failure=True)

model = await workflow.models().last()
```

Field `type` is `"categorical"`, `"continuous"`, or `"ignore"`; RF-Time-GAN adds `"session"` for the session key. An unspecified type defaults to `"continuous"`.

**Label every model with the iteration it belongs to.** `model_labels` is what makes turn 3 of the loop findable a week later; without it the Model Store is a list of hashes.

The SSM and rtf2 families share one flat config shape — `encoder` / `model` / `train` / `quality_check` (+ `relational` for the time variants) — instead of the per-model nesting the GAN and rtf v1 actions use, and `quality_check` is where the step-2 gate lands:

```python
train = ra.TrainTimeSSM(ra.TrainTimeSSM.Config(
    encoder=encoder,                                    # same DatasetConfig type
    train=ra.TrainTimeSSM.TrainConfig(epochs=12, batch_size=4),
    quality_check=ra.TrainTimeSSM.QualityCheckConfig(min_score=0.85),
    relational=ra.TrainTimeSSM.RelationalConfig(
        output_max_length=4096, sub_session_chunk_size=64,
    ),
))
```

Full per-model config and hyperparameter reference: [`reference/models.md`](reference/models.md).

## 5. Generate

Load the model, add a generate action, and (usually) a `SessionTarget` to drive the output volume.

```python
generate = ra.GenerateTimeGAN(ra.GenerateTimeGAN.Config(
    doppelganger=ra.GenerateTimeGAN.DGConfig()
))
target = ra.SessionTarget(target=30_000)     # target=None → match the training size
save = ra.DatasetSave(name="synthetic")

builder = rf.WorkflowBuilder()
builder.add_model(model)
builder.add_action(generate, parents=[model, target])
builder.add_action(target, parents=[generate])     # feedback edge — not a typo
builder.add_action(save, parents=[generate])
workflow = await builder.start(conn)

syn = await workflow.datasets().concat(conn)
```

`SessionTarget` is a **feedback loop**: `generate` is a parent of `target` *and* `target` is a parent of `generate`. Each cycle it counts what came back and requests the shortfall, stopping at the target or at `max_cycles` (default 20). Without that second edge, generation runs once and stops at the default count.

While iterating, generate only as much as step 6 needs to score — the report card pins its own sample at `row_cap` (240k rows) anyway. Generate the full volume once the loop has converged.

To shape *what* gets generated rather than how much, see [`reference/pipeline.md`](reference/pipeline.md#shaping-the-output): conditional metadata (RF-Time-GAN only), `ra.PostAmplify` / `ra.SQL` filtering for rare events, `ra.Replace` for equalized or constrained fields, and `ra.add_sharded_generate` for parallel SSM generation.

## 6. Evaluate and diagnose

Score against the target from step 2 — same `spec`, same `floor`, so runs are comparable across the whole loop.

```python
from rockfish.labs.report_card import score

card = score(dataset.to_pandas(), syn.to_pandas(), spec, name="iteration-1")
print(card.summary(floor=floor))
card.to_json("iteration-1-card.json")
```

Read it in this order: **guards first, then `meta["not_compared"]`, then the scores.** A number computed over the wrong grouping or a missing column is worse than no number, and the card tells you which happened.

Then map the failure to the step that owns it. Going straight to step 4 and adding epochs is usually the wrong move:

| What the card says | What it means | Go back to |
| --- | --- | --- |
| `not_compared` is non-empty | a column never reached the synthetic frame | **3** — it was dropped; was that deliberate? |
| `order=unrecoverable`, or `dup_ts` high | sequence metrics are not measuring what you think | **5** — set `sampling.sequence_index_column` (SSM time) |
| `n_vacuous` high | columns are near-constant and excluded from the composite | **1** — is the sample representative? |
| One column's `marginal` low | that column's encoding | **3** — wrong encoder type, or needs a cast / logit transform |
| `marginal` low across the board | undertrained, or too little data | **4** — epochs, then more source rows |
| `correlation` low | the joint is not being learned | **4** — wrong family (GANs hold continuous joints better) |
| `association` low | categorical joint | **1/3** — association rules, or a dropped driver column |
| `session_length_score` low | session structure is wrong | **3/4** — chunking and `output_max_length` |
| `autocorr_score` low | within-session dynamics | **4** — family choice; SSM over GAN for long dependencies |
| `transition_score` low / high illegal rate | the state machine is not respected | **3** cast to categorical, **4** `state_field_loss_weights`, **5** `state_constraints` |
| `counter_monotonicity` broken | counters run backwards | **3** — model the delta, not the level |
| Bound violations in the output | the inverse projection is missing | **3/5** — not a model failure at all |
| RFScore fine, TS score bad | marginals right, sequence meaningless | **4** — family choice |
| Score ≈ floor, but floor is low | the data cannot support the ask | **1** — usually the wrong session key |

`RFScore = min(marginal, correlation, association)`, gate **0.85**. `TS score = min(session_length, autocorr, transition)`. Both are minimums so one broken dimension cannot hide behind three good ones.

Then drill down with `rl.metrics` / `rl.vis` per field, `rl.metrics.distance_to_closest_record_score` and `memorization_rate` for privacy, and the `ra.Evaluate*` actions for privacy attacks and downstream utility. Full detail, including what to change when a score is low: [`reference/evaluation.md`](reference/evaluation.md).

## Rules that cause most failures

- **Time-series encoders need all three parts.** `timestamp` + at least one `metadata` field + at least one `measurement` field. `TrainTimeTransformerV2` / `TrainTimeSSM` raise `ActionConfigError` at build time if metadata is empty.
- **At least 2 distinct sessions.** Time models group by the metadata fields; if every row shares the same metadata values there is one session and training fails. Use a tabular model instead.
- **`output_max_length` silently drops sessions.** Any session longer than it is discarded during training. On rtf v1 the SDK catches the common case and raises; on rtf2/SSM set `relational.sub_session_chunk_size` so long sessions are split rather than dropped — leaving it unset puts the trainer in whole-session mode where the drop is silent.
- **`TrainTabGAN.TrainConfig` requires `batch_size` even and divisible by `pac`.** Both are `ValueError`s raised from the constructor, before any workflow exists. `epochs` must also be ≥ 1.
- **`TrainTabSSM` / `TrainTimeSSM` require `hidden * expand % head_dim == 0`.** The defaults (384 × 2 ÷ 64) satisfy it; change one knob and you must check the others.
- **`num_bootstrap` is required on rtf v1** (`TrainTabTransformer.TrainConfig`, `TrainTimeTransformer.TrainConfig`) — there is no default. rtf2/SSM replaced it with `quality_check`.
- **`ra.DatasetSave(name="...")`, not `ra.DatasetSave({"name": ...})`.** Published examples show the dict form; use the keyword.
- **Client-side validation is not universal.** The transformer, rtf2, and SSM train actions define `check_table()` and catch a bad encoder — missing fields, no trainable fields, no metadata, fewer than 2 sessions — as a local `ActionConfigError` before any HTTP call. **`TrainTimeGAN` and `TrainTabGAN` have no `check_table`**: the same mistakes there surface as a remote workflow failure, so check the encoder against `dataset.table.column_names` yourself.

## Gotchas

- **Synthetic time-series output is keyed by `session_key`, not by your original session column.** The generator emits its own `session_key` column; the original high-cardinality key is not reproduced. Group synthetic rows by `session_key` — `report_card` does this by default via `CardSpec.synth_session_key`. Grouping by a generated entity *name* merges unrelated sessions and fabricates transitions.
- **Row order does not survive the save path.** Generated timestamps are just another modeled field — neither monotone nor unique. Any metric that depends on within-session order (transitions, dwell time, autocorrelation) is unverifiable unless you set `sampling.sequence_index_column` on an SSM time generate, which adds an explicit generation-order column.
- **Match Arrow string types before scoring categoricals.** `from_pandas` gives `large_string`, generated data comes back as `string`, and `tv_distance` returns **1.0 — the worst score — silently** across that gap. Cast one side first; `category_coverage` is type-agnostic and will tell you when a 1.0 is an artifact.
- **`marginal_dist_score` needs `other_categorical`.** It classifies fields by dtype and sends non-strings to `ks_distance`, which rejects booleans — so one bool column raises and no score comes back. Name every categorical field (and `metadata=` for time series).
- **Session metrics need table metadata on both sides.** `rf.metrics.session_length` / `interarrivals` / `transitions_within_sessions` need `ds.with_table_metadata(rf.TableMetadata(metadata=[...]))` — on the source use your real session fields, on the synthetic use `["session_key"]`.
- **Default generation counts are capped.** With no `SessionTarget` and no explicit count, models generate `min(1000, source sessions/records)` — `min(23_000, …)` for RF-Tab-GAN. To exceed the source size you need `SessionTarget`, not the config's `sessions` / `records` field.
- **`recommend()` refuses below 100 rows**, and refuses again if every column is dropped. Check `config.decision == "refuse"` and show `config.refusal_reason` rather than letting `build_train_workflow` raise.
- **The recommender's `session_key` guess is often wrong.** Pass what you know: `recommend(profile, session_hints={"session_key": "customer"})`, or `DatasetPropertyExtractor(ds, session_key=..., metadata_fields=..., timestamp=...)`.
- **Bounded numeric columns need a logit transform, not clipping.** A column capped by a constant or by another column (`usage` ≤ `capacity`) will generate out-of-range values, and clipping piles mass on the boundary that evaluation then reports as a spike the real data lacks. Nothing detects these automatically — see [`reference/pipeline.md`](reference/pipeline.md#bounded-numeric-columns).
- **State-machine columns encoded as floats are a trap.** A phase column stored as `1.0 / 2.0 / 3.0` trains as a continuous variable and generates `2.37`. The profiler records low-cardinality numerics in `config.categorical_cast_columns` for a VARCHAR cast — apply it.
- **Legal transitions are enforced at decode, frequency is not.** `sampling.state_constraints` makes illegal transitions unsampleable but does not control how often the model transitions; for that, upweight the field with `train.state_field_loss_weights` (typical weight 10). Both are honored by the SSM time path only — rtf2 warns and ignores.
- **This skill targets rockfish ≥ 0.82.2**, and was verified against it end to end. `dataset_profiler` needs ≥ 0.79.0 (`state_constraints` and `sequence_index_column` land there too) and `report_card` needs ≥ 0.81.0. On an older SDK those names fail at *import*, not at run time.

## Reference

- [`reference/models.md`](reference/models.md) — every model family: selection criteria, complete config classes, verified hyperparameter defaults, and how the families differ in config shape.
- [`reference/pipeline.md`](reference/pipeline.md) — the full API surface: profiling, property extraction, preprocessing, train and generate workflow graphs, the Model Store, and every way to shape the output.
- [`reference/evaluation.md`](reference/evaluation.md) — the report card, per-field fidelity metrics and plots, privacy metrics and attacks, downstream utility, and what to change when a score is low.
- [`reference/train-generate.py`](reference/train-generate.py) — a runnable end-to-end script: analyze → set the target → train → generate → score.
