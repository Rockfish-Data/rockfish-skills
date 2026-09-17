# Evaluating synthetic data

Three questions, in the order that matters:

1. **Fidelity** — does it reproduce the statistical shape of the real data?
2. **Privacy** — does it leak individual records?
3. **Downstream utility** — does a model trained on it work as well as one trained on the real data?

Lead with the [report card](#the-report-card) for fidelity. It gives one comparable number per run plus the caveats that number depends on, and it is the same 0.85 gate the SSM and rtf2 trainers use as their quality stop.

## Contents

- [The report card](#the-report-card)
- [How each score is computed](#how-each-score-is-computed)
- [The measurement guards](#the-measurement-guards)
- [Per-field fidelity metrics](#per-field-fidelity-metrics)
- [Session metrics — time series only](#session-metrics--time-series-only)
- [Plots](#plots)
- [Privacy](#privacy)
- [Downstream utility](#downstream-utility)
- [Bounded columns](#bounded-columns)
- [Improving a low score](#improving-a-low-score)

## The report card

`rockfish.labs.report_card` — needs rockfish ≥ 0.81.0. Operates on pandas DataFrames, locally.

```python
from rockfish.labs.report_card import CardSpec, StateFieldSpec, noise_floor, score

spec = CardSpec(
    session_key="customer",
    timestamp="timestamp",
    state_fields=[StateFieldSpec("status")],
    metadata_columns=["age", "gender"],
)

real, syn_df = dataset.to_pandas(), syn.to_pandas()
floor = noise_floor(real, spec)               # real half vs real half
card = score(real, syn_df, spec, name="run1")
print(card.summary(floor=floor))
card.to_json("run1-card.json")
```

```
== run1 (score) ==
  RFScore        0.9142   (floor 0.9631)
    marginal     0.9530   (floor 0.9812)
    correlation  0.9142   (floor 0.9631)
    association  0.9388   (floor 0.9744)
  TS score       0.8410   (floor 0.9302)
    session_len  0.9120   (floor 0.9511)
    autocorr     0.8410   (floor 0.9302)
    transition   0.9701   (floor 0.9880)
  state status: illegal 0.0021 (14/6612) via cross_distinct_ts
  guards: order=recoverable dup_ts=0.031 vacuous_excluded=2 row_cap=240000
```

### The two composites

`RFScore = min(marginal, correlation, association)`, gate **0.85**.
`TS score = min(session_length, lag1_autocorr, transition)`.

Both are **minimums, not averages** — one broken dimension is not allowed to hide behind three good ones. The conventions are fixed so cards stay comparable across runs and surfaces; do not recompute them differently.

Autocorrelation participates only when generation order is trustworthy. Transition participates whenever the cross-distinct-timestamp method was used, which is order-safe by construction. `ts["ts_components_used"]` tells you how many actually counted.

### How each score is computed

Read from `report_card/metrics.py` and `card.py`. A reader checking a published number against a formula needs these, and two of them are easy to get wrong.

| Card field | Formula |
| --- | --- |
| `scores.per_column[c].score` | `1 − TVD(real, syn)` for a categorical column; `1 − KS(real, syn)` for a continuous one (`scipy.stats.ks_2samp`, asymptotic) |
| `scores.marginal` | mean of those per-column scores over **eligible** columns |
| `scores.correlation` | `exp(−‖Δ‖_F / n)`, where Δ is the difference of the upper triangles of the two **Pearson** correlation matrices over eligible continuous columns and `n` is their count |
| `scores.association` | the same Frobenius score over **Cramér's V** matrices for eligible categorical columns |
| `scores.rfscore` | `min` over whichever of marginal / correlation / association are present |
| `ts.session_length_score` | `1 − KS` between the rows-per-session distributions, on full frames |
| `ts.autocorr_score` | `1 − mean(|ac_real − ac_syn|) / 2` across columns where both sides are defined |
| `ts.transition_score` | mean of the per-state-field transition scores |
| `ts.ts_score` | `min` over whichever TS components are present |

"Eligible" means not **vacuous** — a column whose dominant value holds ≥ `vacuous_dominant_frac` (0.99) of the mass is excluded from all three RFScore components before they are computed.

Three things that surprise people:

- **Correlation and association are matrix-distance scores, not correlations.** `exp(−‖Δ‖_F / n)` decays with the Frobenius norm of the difference between the real and synthetic association matrices. A value of 0.82 is not "82% of the correlation preserved".
- **A component that cannot be computed is absent, not zero.** `correlation` needs **≥ 2** eligible continuous columns and `association` needs **≥ 2** eligible categorical columns; below that the key is missing and `rfscore` is the min over what remains — possibly `marginal` alone. Check `scores` for which keys are actually present before comparing two cards.
- **The card's Cramér's V is *not* bias-corrected.** It is plain `sqrt(χ²/n/min(r−1, k−1))` with Yates' continuity correction off. This is a **different function** from [`rl.metrics.cramer_v`](#per-field-fidelity-metrics), which defaults to `correction=True` and applies the Bergsma bias correction. Do not describe the card's `association` as bias-corrected, and do not expect the two to agree.

`ts.autocorr_score` has its own conditions. Per column it is the lag-1 **within-session** Pearson autocorrelation — each value against its predecessor in the same session, after order recovery — over the `autocorr_columns` (8) highest-variance continuous columns in the real data. A column is skipped (`None`, and excluded from the mean) when it has **fewer than 100 usable adjacent pairs** or zero variance on either side, so a short or flat column silently drops out of the score rather than dragging it down. `ts.autocorr_detail` lists the per-column real/synthetic pair.

### The noise floor

**Perfect is not 1.0.** `noise_floor()` splits the *real* data in half by a deterministic session hash and runs the identical pipeline — real vs real. That is the ceiling any synthetic run could reach. Judge every score against the floor; a 0.91 against a 0.96 floor is a good run, and a 0.91 against a 0.99 floor is not.

Requires at least 2 sessions and raises if the hash split produces an empty half.

### `CardSpec`

| Field | Default | Meaning |
| --- | --- | --- |
| `session_key` | required | session identifier **in the real data** |
| `timestamp` | `None` | anchors sequence metrics |
| `synth_session_key` | `"session_key"` | the generator's own key. Synthetic frames are grouped by this when present |
| `sequence_index` | `None` | explicit generation-order column in the synthetic data; beats every order heuristic |
| `state_fields` | `[]` | `StateFieldSpec(name, legal_transitions=None)`. `None` derives the legal set from the real data at scoring time |
| `counters` | `[]` | per-session monotone columns; empty = auto-detect |
| `metadata_columns` | `[]` | session-constant columns whose constancy and session-level joint are checked |
| `exclude_columns` | `[]` | |
| `row_cap` | `240_000` | pinned scoring sample for marginal/correlation/association — these move with sample size, so the cap is pinned and recorded. Session-level metrics always use full frames |
| `cat_max_cardinality` | `20` | ≤ this many distinct values = categorical |
| `vacuous_dominant_frac` | `0.99` | dominant-value fraction at or above which a column is excluded from composites |
| `autocorr_columns` | `8` | how many highest-variance continuous columns to test |
| `seed` | `42` | |

Everything but `session_key` is optional, and metrics degrade gracefully: no timestamp or order source means sequence metrics are skipped, no state fields means state-machine metrics are skipped. **Every omission is recorded in the card**, so a missing metric is visible rather than silently absent.

Derive a spec straight from a profile:

```python
from rockfish.labs.dataset_profiler import profile_table
spec = CardSpec.from_profile(profile_table(real), session_key="pod", timestamp="timestamp")
```

It picks up detected state fields (excluding monotone counters and censored accumulators, which are not lifecycles) and detected counters, so the profiler's findings flow into evaluation without re-derivation.

**It does not carry the transition maps, despite the docstring.** `from_profile` reads `cand.transitions`, but `StateFieldCandidate` defines the attribute as `transition_map` — so `legal_transitions` comes back `None` every time, verified on rockfish 0.82.2. The card then derives the legal set from the real data at scoring time, which is a sane fallback but is *not* the map the user confirmed: any rare legal edge the sample missed will be scored as an illegal transition. Set them explicitly when you have a confirmed map:

```python
from rockfish.labs.dataset_profiler import decode_constraints, detect_state_fields

cands = detect_state_fields(real_table, session_key="customer", order_by="timestamp")
confirmed = decode_constraints(cands)          # {field: {value: [allowed next]}} — confirm with the user
spec.state_fields = [
    StateFieldSpec(f, legal_transitions={(a, b) for a, bs in m.items() for b in bs})
    for f, m in confirmed.items()
]
```

### What the card contains

| Section | Contents |
| --- | --- |
| `scores` | `rfscore`, `marginal`, `correlation`, `association`, `n_scored`, `n_vacuous`, `rfscore_gate`, and `per_column` (type, score, null rates, vacuous flag, dominant fraction, unseen mass) |
| `ts` | `ts_score`, `session_length_score` + length quantiles, `autocorr_score` + per-column detail, `transition_score`, `counter_monotonicity`, `ts_components_used` |
| `state_machine` | per state field: illegal-transition rate, counts, and the method used |
| `null_structure` | per-column real vs synthetic null rates, plus state-conditioned MAE |
| `session_structure` | metadata constancy, real vs synthetic entity counts |
| `guards` | order confidence, duplicate-timestamp fractions, whether the generator key was used, vacuous exclusions, row cap, seed, counters evaluated |
| `meta` | row counts, columns compared, columns **not** compared |

Check `meta["not_compared"]` — a column missing from the synthetic frame scores nothing and would otherwise pass unnoticed.

### CLI

```bash
python -m rockfish.labs.report_card floor --real real.parquet --spec spec.json
python -m rockfish.labs.report_card score --real real.parquet --synth syn.parquet \
    --spec spec.json --floor floor-card.json --name run1 --out run1-card.json
```

`spec.json` mirrors `CardSpec`:

```json
{"session_key": "k8s_pod_name", "timestamp": "timestamp",
 "state_fields": [{"name": "k8s_pod_phase"}],
 "metadata_columns": ["k8s_namespace_name", "k8s_node_name"]}
```

Reads `.parquet` / `.pq` / `.csv`.

## The measurement guards

Each guard exists because its absence once produced a badly wrong number. They are enforced in code, not offered as advice, and their verdicts are embedded in every card so a number can never outrun its caveats.

- **Synthetic rows are never grouped by generated entity names.** Generated names collide across sessions (35 unique pod names observed for 198 sessions); grouping by name merges sessions and fabricates transitions. The card groups by `synth_session_key` — the generator's own key — whenever it is present.
- **Sequence metrics are refused, not mis-scored, when generation order is unrecoverable.** `guards.synth_order_confidence` reports `index` / `recoverable` / worse; autocorrelation only participates in the TS score when order is trustworthy.
- **Categorical values are canonicalized across cast engines.** `"2.0"` and `"2"` are the same value — Snowflake's `TO_VARCHAR(1.0)` gives `'1'` and DataFusion gives `'1.0'`. Non-numeric strings pass through.
- **Vacuous columns are excluded from composites.** A column where one value holds ≥ 99% of the mass scores near-perfectly for free and would inflate the composite. The count is reported as `n_vacuous`.
- **The scoring sample size is pinned and recorded.** Marginal, correlation, and association scores move with sample size, so they are computed at `row_cap` rows.
- **Transitions use a cross-distinct-timestamp test** so duplicate timestamps do not manufacture self-transitions. `synth_dup_ts_fraction` reports how common they are.

## Per-field fidelity metrics

`rockfish.labs.metrics` (`rl.metrics`). All take two `LocalDataset`s.

| Metric | Direction | Use on |
| --- | --- | --- |
| `marginal_dist_score(dataset, syn, metadata=[], other_categorical=[], weights={})` | 1 best | overall weighted fidelity |
| `ks_distance(ds1, ds2, field)` | 0 best | one continuous field |
| `tv_distance(ds1, ds2, field)` | 0 best | one categorical field |
| `jsd(ds1, ds2, fields)` | 0 best | categorical distributions |
| `emd(ds1, ds2, fields)` | 0 best, ∞ worst | numerical distributions (Wasserstein) |
| `range_coverage(ds1, ds2, field)` | 1 best | does synthetic span the real range |
| `category_coverage(ds1, ds2, field)` | 1 best | share of real categories that appear |
| `range_adherence_score(dataset, syn, fields)` | 1 best | share of synthetic values inside the real range (numeric or temporal) |
| `correlation_score(dataset, syn, fields)` | 1 best | numeric pairwise structure |
| `association_score(dataset, syn, fields)` | 1 best | categorical pairwise structure |
| `pearsonr(dataset, x, y)` | −1…1 | linear correlation of two numeric fields, with p-value |
| `cramer_v(dataset, x, y, correction=True)` | 0…1 | association of two categorical fields; bias-corrected by default. **Not** the function behind the report card's `association` — see [How each score is computed](#how-each-score-is-computed) |

The **overall fidelity score** (`marginal_dist_score`) is a weighted average over marginal distributions: total-variation distance for categorical fields, Kolmogorov–Smirnov for continuous. Tabular datasets score all fields; time-series datasets score metadata, measurements, session length, and interarrival time. Default weight 1 each; `weights={"amount": 3}` reweights.

**Name your categorical fields in `other_categorical`.** Left to itself, `marginal_dist_score` classifies fields by dtype and routes anything non-string to `ks_distance`, which accepts only numeric and temporal types — so a single **boolean** column raises `ValueError: Field 'x' must be either numeric or temporal with matching types in both datasets` and takes the whole score down with it. The same applies to any numeric column you encoded as categorical. For a time-series dataset, pass `metadata=` as well.

```python
rl.metrics.marginal_dist_score(dataset, syn, other_categorical=["region", "tier", "returned"])
```

**Match Arrow string types before comparing categoricals.** `rf.Dataset.from_pandas` produces `large_string`; data generated by the backend comes back as plain `string`. `tv_distance` treats those as disjoint and returns **1.0 — the worst possible score — with no error and no warning**. Measured on a real run: `region` scored `1.0` across the type gap and `0.048` once matched, and the overall `marginal_dist_score` moved from 0.32 to 0.62 on the cast alone. `ks_distance` at least raises on mismatched types; the categorical path fails silently.

```python
import pyarrow as pa

table = real.table
for f in syn.table.schema:
    i = table.schema.get_field_index(f.name)
    if i >= 0 and pa.types.is_string(f.type) and table.schema.field(i).type != f.type:
        table = table.set_column(i, f.name, table.column(i).cast(f.type))
real = rf.Dataset.from_table(real.name(), table)
```

`category_coverage` is type-agnostic, so it is the cross-check: a `tv_distance` of 1.0 alongside a `category_coverage` of 1.0 means the type gap, not a real mismatch.

`rockfish.labs.SDA` wraps this for time series — it extracts properties for both datasets, drops the timestamp and session key, and calls `marginal_dist_score`:

```python
score = rl.SDA(source, syn).get_score()
```

## Session metrics — time series only

`rockfish.metrics` (`rf.metrics`). **Both datasets need table metadata first**, or these cannot define a session:

```python
dataset = dataset.with_table_metadata(rf.TableMetadata(metadata=["customer", "age", "gender"]))
syn = syn.with_table_metadata(rf.TableMetadata(metadata=["session_key"]))
```

Note the asymmetry: the source uses your real session fields, the synthetic uses the generator's `session_key`.

```python
source_sess = rf.metrics.session_length(dataset)          # rows per session
syn_sess = rf.metrics.session_length(syn)
rl.metrics.ks_distance(source_sess, syn_sess, "session_length")

source_ia = rf.metrics.interarrivals(dataset, "timestamp")  # gaps between consecutive rows
syn_ia = rf.metrics.interarrivals(syn, "timestamp")
rl.metrics.ks_distance(source_ia, syn_ia, "interarrival")

t_src = rf.metrics.transitions_within_sessions(dataset, field="status")
t_syn = rf.metrics.transitions_within_sessions(syn, field="status")
rl.metrics.tv_distance(t_src, t_syn, "status_transitions")

rf.metrics.count_all(dataset, "category", nlargest=10)
```

The output field names are fixed: `session_length`, `interarrival`, `<field>_transitions`.

Transitions can be counted three ways. For `Session 1: A→B→B` and `Session 2: A→B→B→C`:

| Method | Session 1 | Session 2 |
| --- | --- | --- |
| k-gram uncollapsed (k=2) | `A→B`, `B→B` | `A→B`, `B→B`, `B→C` |
| k-gram collapsed (k=2) | `A→B` | `A→B`, `B→C` |
| full collapsed | `A→B` | `A→B→C` |

## Plots

`rockfish.labs.vis` (`rl.vis`). Each takes a list of datasets so real and synthetic overlay.

```python
rl.vis.plot_kde([dataset, syn], "amount")                  # continuous
rl.vis.plot_hist([dataset, syn], "amount")
rl.vis.plot_cdf([dataset, syn], "amount")
rl.vis.plot_bar([source_agg, syn_agg], "category", "category_count")   # categorical
rl.vis.plot_correlation([dataset, syn], "SBP", "DBP", alpha=0.5)
rl.vis.plot_correlation_heatmap([dataset, syn], numeric_fields, annot=True, fmt=".2f")
rl.vis.plot_association_heatmap([dataset, syn], categorical_fields)
rl.vis.plot_kde([source_ia, syn_ia], "interarrival", duration_unit="s")
```

Also `plot_distribution`, `plot_scatter`, `custom_plot`. `plot_bar` takes `orient="horizontal"` for long transition labels.

## Privacy

### Local metrics

```python
rl.metrics.memorization_rate(dataset, syn)       # 0 best, 1 = pure copying
```

Proportion of synthetic records that are exact duplicates of real records. High means the generator memorized instead of learning. Both datasets must have identical field names in identical order.

```python
rl.metrics.distance_to_closest_record_score(train, test, syn,
                                            subset_length=None, subset_seed=None,
                                            transform=None)
```

DCR compares the Gower-distance-to-nearest-real-record distribution for `(train, syn)` against `(train, test)`. The closer those are, the more private the synthetic data. **`test` must be sampled from the same distribution as `train` and must not have been used to train the generator** — without a genuine holdout the score is meaningless.

| Quality | Raw score | With `transform="sigmoid"` |
| --- | --- | --- |
| Low | 0 – 0.75 | 0 – 0.36 |
| Medium | 0.75 – 1.0 | 0.36 – 0.46 |
| High | ≥ 1.0 | 0.46 – 1.0 |

### Attack simulations

Both run as workflow actions and need one table carrying `ori`, `syn`, and `control` rows distinguished by a label column. Build it with `ra.SQL`:

```python
syn_remote = await syn.to_remote(conn)
control_remote = await test_dataset.to_remote(conn)
concat = ra.SQL(
    query="""
        select *, 'ori'     as label from ori
        union all
        select *, 'syn'     as label from syn
        union all
        select *, 'control' as label from control
    """,
    table_name="ori",
    dataset_name_to_id={"syn": syn_remote.id, "control": control_remote.id},
)

evaluate = ra.EvaluateLinkability({
    "n_attacks": 500, "n_trials": 3,
    "aux_cols_a": ["Age", "Gender"],
    "aux_cols_b": ["Zip Code", "Medical Condition"],
    "label": "label", "n_neighbors": 1,
})

builder = rf.WorkflowBuilder()
builder.add_path(train_dataset, concat, evaluate, ra.DatasetSave(name="linkability"))
workflow = await builder.start(conn)
await workflow.wait(raise_on_failure=True)
result = await workflow.datasets().concat(conn)
```

`EvaluateLinkability` — can an attacker holding two disjoint attribute sets link them back to one record? Config: `n_attacks=500`, `n_trials=3`, `label="label"`, `rng=None`, `aux_cols_a=[]`, `aux_cols_b=[]`, `n_neighbors=1`.

`EvaluateInference` — can an attacker infer a secret attribute from known ones? Config: `n_attacks=500`, `n_trials=3`, `label="label"`, `rng=None`, `aux_cols=[]`, `secret="secret"`.

Both emit a single score 0–1; **higher is better protection**.

## Downstream utility

The TSTR question: does a model trained on synthetic data perform like one trained on real data, both judged on the same real holdout?

Build a table with a `split` column (`train` from the dataset under test, `test` from a real control holdout), run the evaluator, and compare AUCs.

```python
from rockfish.actions.txtr import EvaluateRandomForest

concat = ra.SQL(
    query="select *, 'train' as split from ori union all select *, 'test' as split from control",
    table_name="ori",
    dataset_name_to_id={"control": control_remote.id},
)
config = {
    "features": ["BBS Score", "Body Temperature", "Heart Rate"],
    "target": "Sex",
    "pos_label": "F",
}

builder = rf.WorkflowBuilder()
builder.add_path(ori, concat, EvaluateRandomForest(config), ra.DatasetSave(name="auc"))
workflow = await builder.start(conn)
await workflow.wait(raise_on_failure=True)
auc_real = (await workflow.datasets().concat(conn)).table.columns[0].to_pylist()[0]

# repeat with `syn` in place of `ori` -> auc_syn
rl.metrics.txtr_score(auc_real, auc_syn, 0.5)
```

`txtr_score(real, syn, lower=0.5)` returns `min(1, (syn - lower) / (real - lower))`, and `NaN` if either AUC is at or below `lower` — a classifier no better than chance gives no signal about the data.

| Action | For |
| --- | --- |
| `ra.EvaluateLogisticRegression` | binary classification; `features`, `target`, `pos_label`, `table_split_col_name="split"`, plus the sklearn `LogisticRegression` knobs |
| `ra.EvaluateRandomForest` | binary classification; same shape, plus the sklearn `RandomForestClassifier` knobs |
| `ra.EvaluateForecast` | time-series forecasting |

`target` must have exactly two unique values. `pos_label` defaults to `1` when the target set is `{0,1}` or `{-1,1}`. `ra.SampleLabel` can generate the split column instead of hand-written SQL.

## Bounded columns

When a column was trained through the logit transform described in [`pipeline.md`](pipeline.md#bounded-numeric-columns), the evaluation pass changes in three ways.

**Check the constraint holds — it should be structurally impossible to break.**

```python
df = syn.to_pandas()
violations = ((df["usage"] < 0) | (df["usage"] > df["capacity"])).sum()
assert violations == 0, f"{violations} rows violate usage <= capacity"
```

Any violation at all means the inverse projection was not applied, or was applied to the wrong column — not that the model behaved badly. `rl.metrics.range_adherence_score(dataset, syn, ["usage"])` is the built-in version for constant caps; it returns the share of synthetic values inside the real range, so anything below 1.0 is the same finding.

**Compare boundary frequency explicitly.** The exact-0 and exact-max rates are the thing the inflation model is responsible for, and no composite score isolates them:

```python
for name, frame in (("real", real_df), ("syn", df)):
    zero = (frame["usage"] == 0).mean()
    full = (frame["usage"] == frame["capacity"]).mean()
    print(f"{name}: exact-0 {zero:.4f}  exact-cap {full:.4f}")
```

**Score the distribution in logit space, not raw space.** KS or Wasserstein on the raw ratio is dominated by the pile-up at the walls and will hide a mismatch across the whole interior. Transform both sides first, then compare:

```python
import numpy as np

def interior_logit(frame):
    p = (frame["usage"] / frame["capacity"]).clip(1e-6, 1 - 1e-6)
    return p[(frame["usage"] > 0) & (frame["usage"] < frame["capacity"])].pipe(
        lambda x: np.log(x / (1 - x))
    )

rl.metrics.ks_distance(
    rf.Dataset.from_pandas("real", interior_logit(real_df).to_frame("usage_logit")),
    rf.Dataset.from_pandas("syn", interior_logit(df).to_frame("usage_logit")),
    "usage_logit",
)
```

The report card needs no special handling: score the **reconstructed** column, after the inverse projection, so the card describes the data the user actually receives.

## Improving a low score

Work down this list; the cheap structural fixes usually dominate hyperparameter tuning.

1. **Check the floor first.** If `noise_floor` is 0.88, a synthetic 0.86 is close to the ceiling and no amount of training will reach 0.99.
2. **Check `meta["not_compared"]` and `guards`.** A dropped column, an unrecoverable generation order, or a high duplicate-timestamp fraction means the number is measuring less than you think.
3. **More training data.** The single biggest lever. More examples make distributions and correlations easier to learn.
4. **Clean the source.** Fix anomalies, type mismatches, and nulls before training, not after.
5. **Fix the encoder.** A high-cardinality datetime encoded as categorical explodes the vocabulary; a float-coded state column (`1.0 / 2.0 / 3.0`) trains as continuous and generates `2.37`. Cast it to VARCHAR — `config.categorical_cast_columns` lists the candidates.
6. **Split or join sources.** Multiple datasets can be trained separately, or joined on shared columns and trained as one.
7. **Handle out-of-range values.** `clip_in_range=True` (RF-Tab-GAN) or `clip_per_sample=True` (RF-Time-GAN) keeps values in range but piles mass on the boundaries; filtering afterwards with `dataset.sync_sql(...)` avoids that distortion.
8. **Change model family.** Many continuous measurements → GAN. Categorical structure and long-range within-session dependency → SSM or rtf2.
9. **Then tune hyperparameters.** Start from the defaults in [`models.md`](models.md). For SSM/rtf2 time models, `epochs` past ~12 buys very little (24 epochs scored 0.9373 against 12 epochs' 0.9371 on identical data) — spend the effort on `batch_size` matched to session-length variance and on `output_max_length` / `sub_session_chunk_size` instead.
10. **If a state field churns**, constraints are not the fix for frequency. `sampling.state_constraints` makes illegal transitions unsampleable; `train.state_field_loss_weights={"phase": 10}` is what changes how often the model transitions.
