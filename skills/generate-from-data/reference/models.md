# Rockfish models

Every field, type, and default on this page was read from the Rockfish SDK source at 0.83.0.dev1. Where the published documentation disagrees, the SDK wins and the difference is called out.

## Contents

- [The families](#the-families)
- [Choosing a model](#choosing-a-model)
- [The encoder config (shared by every model)](#the-encoder-config-shared-by-every-model)
- [RF-Time-GAN — DoppelGANger](#rf-time-gan--doppelganger)
- [RF-Tab-GAN — CTGAN](#rf-tab-gan--ctgan)
- [SSM and rtf2 — the flat-config families](#ssm-and-rtf2--the-flat-config-families)
- [rtf v1 — REaLTabFormer (legacy)](#rtf-v1--realtabformer-legacy)
- [Corrections to the published hyperparameter table](#corrections-to-the-published-hyperparameter-table)

## The families

| Family | Train action | Generate action | Architecture |
| --- | --- | --- | --- |
| RF-Time-GAN | `TrainTimeGAN` | `GenerateTimeGAN` | DoppelGANger (GAN) |
| RF-Tab-GAN | `TrainTabGAN` | `GenerateTabGAN` | CTGAN (GAN) |
| SSM (time) | `TrainTimeSSM` | `GenerateTimeSSM` | Mamba-2 state-space, prefix-conditioned relational |
| SSM (tabular) | `TrainTabSSM` | `GenerateTabSSM` | Mamba-2 state-space |
| rtf2 (time) | `TrainTimeTransformerV2` | `GenerateTimeTransformerV2` | GPT-2, fixed-vocab tokenization |
| rtf2 (tabular) | `TrainTabTransformerV2` | `GenerateTabTransformerV2` | GPT-2, fixed-vocab tokenization |
| rtf v1 (time) | `TrainTimeTransformer` | `GenerateTimeTransformer` | REaLTabFormer parent/child |
| rtf v1 (tabular) | `TrainTabTransformer` | `GenerateTabTransformer` | REaLTabFormer |

Plus `ChunkTimeSSM`, a standalone CPU pre-chunker for long-session SSM training.

**rtf v1 is superseded by rtf2.** rtf2 replaced v1's bootstrap-sensitivity critic with an RFScore quality stop, shrank the default GPT-2 (v1's defaults were oversized for typical tabular data), and enabled a train/eval split by default (v1 defaulted `train_size=1.0`, silently disabling both eval and early stopping). `rockfish.labs.dataset_profiler` maps a `tab_transformer` / `time_transformer` decision onto the **V2** actions. Use v1 only to reproduce an existing model.

## Choosing a model

The `dataset_profiler` recommender routes with these rules. They are a good manual checklist too — `config.rule_fired` tells you which one fired.

Refusal comes first: **fewer than 100 rows → refuse**, and **no trainable columns after drops → refuse**.

A dataset takes the time path only if it has both a usable session key and a timestamp column.

| Rule | Condition | Decision |
| --- | --- | --- |
| R0 | session + timestamp, **≥ 3 continuous measurements** | `time_gan` |
| R1 | session + timestamp, ≥ 5 sessions, **avg length > 500** | `time_gan` |
| R2 | session + timestamp, **< 50 sessions**, avg length 4–500 | `time_gan` |
| R3 | session + timestamp, **≥ 50 sessions**, avg length 4–500 | `time_ssm` |
| R4 | tabular, **< 1000 rows** or (≤ 3 numeric and 0 categorical) | `tab_gan` |
| R5 | tabular, otherwise | `tab_ssm` |

Two opt-in overrides on `recommend()`:

- `prefer_ssm_for_long_sessions=True` reroutes R1-shaped data (avg session length > 500) from `time_gan` to `time_ssm`, deriving `sub_session_chunk_size` and `output_max_length` from a token budget.
- `prefer_ssm=True` prefers the SSM family generally.

The reasoning behind R0 is worth internalizing: **continuous floats are a GAN's strength and a transformer's weakness.** A token-based model turns every distinct float into new vocabulary, which is unbounded and learns nothing. Conversely, categorical structure and long-range within-session dependencies are what the SSM and transformer families are good at.

Training cost, roughly ascending: RF-Tab-GAN < RF-Time-GAN < SSM ≈ rtf2 < rtf v1.

## The encoder config (shared by every model)

All eight train actions take the same `DatasetConfig` type, re-exported on each action class (`ra.TrainTimeGAN.DatasetConfig`, `ra.TrainTabSSM.DatasetConfig`, … — all the same class from `rockfish.actions.dg`).

```python
ra.TrainTimeGAN.DatasetConfig(
    name="default",
    timestamp=ra.TrainTimeGAN.TimestampConfig(field="timestamp"),
    metadata=[ra.TrainTimeGAN.FieldConfig(field="customer", type="session"), ...],
    measurements=[ra.TrainTimeGAN.FieldConfig(field="amount", type="continuous"), ...],
    privacy=None,        # PrivacyConfig(fields=[...])
    embedding=None,      # EmbeddingConfig(type=, size=5, window=5, fields=, delimiter=",")
    group_by=None,
)
```

`FieldConfig(field, type, semantic_type=None)`. Valid `type` values:

| `type` | Meaning |
| --- | --- |
| `"continuous"` | numeric; **the default when `type` is omitted** |
| `"categorical"` | discrete value set |
| `"ignore"` | excluded from training entirely |
| `"session"` / `"session-key"` | RF-Time-GAN only — a high-cardinality session identifier whose *values* are not learned, only its role as a session boundary |

Rules of thumb for assigning `type` (this is what `dataset_profiler._encoder_type` does): numeric with > 20 distinct values → continuous; timestamp with > 20 distinct values → continuous; everything else, including strings, booleans, and low-cardinality numerics → categorical. Never encode a high-cardinality datetime as categorical — it is a vocabulary explosion of roughly one token per row.

**Time models** populate `timestamp`, `metadata` (session-level attributes, roughly constant within a session), and `measurements` (per-row values).

**Tabular models** have no role split. The published examples and tutorial notebooks put every field in `metadata` with `measurements` empty; `dataset_profiler.build_encoder` does the opposite (`metadata=[]`, everything in `measurements`). Both reach the trainer as one field list. Pick one convention and hold it within a project.

`measurements` was once called `timeseries`; that name still structures but emits a `DeprecationWarning` and is merged into `measurements`.

## RF-Time-GAN — DoppelGANger

Best for time series with several continuous measurements, and the only model supporting conditional generation on metadata values.

```python
train = ra.TrainTimeGAN(ra.TrainTimeGAN.Config(
    encoder=encoder,                         # DatasetConfig
    doppelganger=ra.TrainTimeGAN.DGConfig(epoch=100, batch_size=64, sample_len=19),
    model_ids=None,                          # continue training existing models
    model_labels={},                         # labels stamped on the trained Model
))
```

`Config.doppelganger` has a default factory, so `ra.TrainTimeGAN.Config(encoder=...)` alone is valid.

### `DGConfig` — used for both training and generation

| Field | Default | Notes |
| --- | --- | --- |
| `epoch` | `400` | training iterations |
| `batch_size` | `100` | must be **smaller than the number of sessions**; larger reduces training time and improves fidelity at the cost of memory |
| `sample_len` | `1` | records emitted per step within a session. Typical setting: `avg_session_len / 50`. Raising it shortens the effective sequence, cutting training time and memory |
| `activate_normalization_per_sample` | `True` | per-session normalization of continuous fields. Improves fidelity; can emit out-of-range values, which `clip_per_sample` at generation clips |
| `generator_attribute_num_layers` | `5` | |
| `generator_feature_num_layers` | `1` | |
| `g_lr` | `1e-4` | generator learning rate |
| `d_lr` | `1e-4` | discriminator learning rate |
| `attr_d_beta1` | `0.5` | Adam beta1 for the metadata discriminator |
| `sessions` | `None` | generation only — number of sessions to emit |
| `epoch_checkpoint_freq` | `None` | **deprecated**; checkpointing is automatic |

`DGConfig` accepts extra keys and forwards them to the worker, so generation-only parameters that have no SDK field still work:

| Generation parameter | Default | Notes |
| --- | --- | --- |
| `given_metadata` | `None` | `{field: [values]}` — conditional generation. If `None`, all metadata combinations are generated. **RF-Time-GAN only.** |
| `clip_per_sample` | `True` | keeps generated continuous values in range; only meaningful when `activate_normalization_per_sample=True` at training |

Because extras are forwarded rather than validated, a misspelled key is accepted silently. Check spelling.

```python
generate = ra.GenerateTimeGAN(ra.GenerateTimeGAN.Config(
    doppelganger=ra.GenerateTimeGAN.DGConfig(
        given_metadata={"age": ["2", "3"], "gender": ["F"]},
        clip_per_sample=True,
        sessions=500,
    )
))
```

## RF-Tab-GAN — CTGAN

Fastest model. Good for small tables and numeric-heavy tables.

```python
train = ra.TrainTabGAN(ra.TrainTabGAN.Config(
    tabular_gan=ra.TrainTabGAN.TrainConfig(epochs=100),   # REQUIRED — no default factory
    encoder=encoder,
    model_labels={},
))
```

### `TrainTabGAN.TrainConfig`

| Field | Default | Notes |
| --- | --- | --- |
| `epochs` | `10` | must be ≥ 1 |
| `batch_size` | `500` | must be **even** and a multiple of `pac` |
| `pac` | `1` | samples grouped per discriminator pass |
| `min_max` | `None` | **deprecated** — use `clip_in_range` at generation |

`batch_size` is validated at construction: an odd value raises `ValueError: batch_size must be even`, and one that is not a multiple of `pac` raises `ValueError: batch_size must be a multiple of pac`. `epochs=0` raises too. The module contains adjustment logic that would round `batch_size` into shape, but the raising validator wins, so these are hard failures rather than silent rewrites.

### `GenerateTabGAN.GenerateConfig`

| Field | Default | Notes |
| --- | --- | --- |
| `records` | `None` | rows to emit; `None` → `min(23_000, source rows)` |
| `clip_in_range` | `True` | clips continuous values to the training range. Can pile mass on the boundaries and distort the tails |

```python
generate = ra.GenerateTabGAN(ra.GenerateTabGAN.Config(
    tabular_gan=ra.GenerateTabGAN.GenerateConfig(records=1000, clip_in_range=True)
))
```

`Config.tabular_gan` is required on both the train and generate configs — pass an empty `GenerateConfig()` if you want the defaults.

## SSM and rtf2 — the flat-config families

These two families share `TrainConfig`, `QualityCheckConfig`, `SamplingConfig`, and `RelationalConfig` (defined in `rockfish.actions.transformer_v2` and re-exported from `rockfish.actions.ssm`). Only `ModelConfig` differs — Mamba-2 knobs vs GPT-2 knobs. Everything else below applies to all four train actions.

```python
train = ra.TrainTimeSSM(ra.TrainTimeSSM.Config(
    encoder=encoder,                                      # required
    model=ra.TrainTimeSSM.ModelConfig(),
    train=ra.TrainTimeSSM.TrainConfig(epochs=12, batch_size=4),
    quality_check=ra.TrainTimeSSM.QualityCheckConfig(),
    relational=ra.TrainTimeSSM.RelationalConfig(output_max_length=4096),
    model_labels={},
))
```

Tabular variants (`TrainTabSSM`, `TrainTabTransformerV2`) take the same config minus `relational`.

### `ModelConfig` — rtf2 (GPT-2)

| Field | Default |
| --- | --- |
| `n_layers` | `6` |
| `n_heads` | `8` |
| `hidden` | `384` |

### `ModelConfig` — SSM (Mamba-2)

| Field | Default | Notes |
| --- | --- | --- |
| `n_layers` | `12` | |
| `hidden` | `384` | |
| `expand` | `2` | |
| `state_size` | `16` | |
| `head_dim` | `64` | |
| `n_groups` | `1` | |
| `chunk_size` | `64` | |

**Constraint: `hidden * expand` must be divisible by `head_dim`.** The defaults satisfy it (384 × 2 = 768 = 12 × 64) and `num_heads` is derived server-side. `check_table` raises `ActionConfigError` if you break it.

### `TrainConfig` — both families

| Field | Default | Notes |
| --- | --- | --- |
| `epochs` | `50` | |
| `batch_size` | `16` | there is no `group_by_length`: every batch pads to its longest member, so match batch size to **length variance**, not table size |
| `learning_rate` | `5e-4` | |
| `gradient_accumulation_steps` | `4` | effective batch = `batch_size × this` |
| `weight_decay` | `0.0` | |
| `gradient_checkpointing` | `False` | trades compute for memory |
| `train_size` | `0.9` | 90/10 split; enables eval and early stopping |
| `mask_rate` | `0.0` | |
| `dataloader_num_workers` | `None` | auto-resolved per world size |
| `bf16` | `False` | disables HF Trainer's fp16 path; preferred on H100 |
| `state_field_loss_weights` | `None` | `{field: weight}` upweighting for state-machine fields. A state field is one token in a wide row, so it receives a sliver of the gradient and the model churns it far above the real rate. Weight the **whole field** (weighting transitions only teaches the model to transition *more*). Typical weight 10. **SSM relational time path only** |

`TrainTimeSSM` overrides the defaults to `batch_size=1, gradient_checkpointing=True` — per-session sequences OOM an H100 80GB at `hidden=384` with larger batches.

Profiler-tuned starting points, by archetype:

| Path | epochs | batch_size | Notes |
| --- | --- | --- | --- |
| tabular (SSM / rtf2) | 30 | 16 | `learning_rate=5e-4`, `gradient_accumulation_steps=4` |
| time, `panel` archetype (max/p50 < 2) | 12 | 64 | fixed-length sessions, no padding waste |
| time, `alarm` archetype (max/p50 ≥ 10) | 12 | 32 | skewed lengths punish large batches |
| time, `mixed` | 12 | 32 | |

All time rows also set `gradient_checkpointing=True`. The profiler then caps `batch_size` at `32_768 // output_max_length` — `batch_size × output_max_length` is the real memory knob (17,723 tokens measured 8.2 GB at batch 1). Measured on the RAN engagement: 24 epochs scored 0.9373 against 12 epochs' 0.9371, so **12 is the honest default** for time models.

### `QualityCheckConfig` — the RFScore training stop

Replaces rtf v1's bootstrap-sensitivity critic. Composite score is `min(marginal, correlation, association, efficacy)`. The check runs every `interval_epochs` after `warmup_epochs`; once `min_score` is crossed once, a patience clock starts and training stops after `patience` non-improving rounds.

| Field | Default |
| --- | --- |
| `enabled` | `True` |
| `interval_epochs` | `5` |
| `warmup_epochs` | `1` |
| `patience` | `3` |
| `min_score` | `0.85` |
| `sample_size` | `500` |
| `eval_parent_n` | `32` |
| `token_budget` | `50_000` |
| `min_sample_size` | `50` |

`token_budget` caps tokens generated per check so wide tables stay affordable; the effective sample is `min(sample_size, token_budget / seq_len)`, floored at `min_sample_size`.

Note that `min_score=0.85` is the same 0.85 gate the [report card](evaluation.md) uses — training stops at the quality bar you later score against.

### `RelationalConfig` — time variants only

| Field | Default | Notes |
| --- | --- | --- |
| `output_max_length` | `2048` | max tokens per session |
| `freeze_parent` | `True` | |
| `sub_session_chunk_size` | `None` | **set this for long sessions.** Left unset, the trainer runs in whole-session mode and silently drops every session longer than `output_max_length` |
| `sub_session_memory_rows` | `8` | rows carried from the previous chunk as continuation context |
| `max_chunks` | `32` | caps inference-time chunked generation; raise it when the token budget produces more chunks than this |

A chunk's training sequence is `[SS, parent, EOS, CHUNK_BREAK, memory rows, BOS, chunk rows, CHUNK_BREAK/EOS]` — the parent prefix (allow ~64 tokens) and the memory rows share `output_max_length` with the chunk body. Budget for them, or every chunk after the first is dropped.

### `SamplingConfig` — generation

| Field | Default | Notes |
| --- | --- | --- |
| `gen_batch` | `256`, but `32` for `GenerateTimeSSM` | **`GenerateTabSSM` does not get the lower default** and keeps 256. Mamba-2 without the CUDA kernels materialises a large per-step intermediate, so on a CPU worker a tabular SSM generate at 256 does not finish — observed on a 4,000-row model sitting in `Start generating samples...` for hours after logging `CUDA not available`. Set `gen_batch=32` explicitly for tabular SSM, or make sure the job lands on a GPU worker set |
| `top_k` | `None` | |
| `top_p` | `None` | |
| `temperature` | `1.0` | |
| `seed` | `None` | per-call sampling seed; overrides the model's baked-in `random_state` for this generation only |
| `bias_dict` | `None` | `{field: {value: bias}}` additive logits bias on categorical columns. A soft nudge toward (positive) or away from (negative) a value; ~30 is a near-hard pin. Avoid ±inf — it does not survive config JSON |
| `state_constraints` | `None` | `{field: {value: [allowed next values]}}`. Masks each row's decode to values legally reachable from the previous row's value, so illegal transitions are unsampleable while the model still chooses *when* to transition. Width-1 categorical fields only; unknown fields and values are ignored. **SSM time path only** — rtf2 warns and ignores |
| `sequence_index_column` | `None` | adds an integer column holding each row's position within its session **in generation order**. Row order does not survive the save path and generated timestamps are neither monotone nor unique, so without this no consumer can verify transitions, dwell, or autocorrelation. **SSM time path only** |

Values in `state_constraints` are the post-cast strings the encoder sees — `"2.0"` for a float-cast state column, not `2`. Produce them with `rockfish.labs.dataset_profiler.state.decode_constraints()` and have the user confirm: a sampled graph can miss rare legal edges.

### Generate configs

```python
ra.GenerateTimeSSM.Config(sessions=None, sampling=SamplingConfig(), quantize=None)
ra.GenerateTabSSM.Config(records=None,  sampling=SamplingConfig(), quantize=None)
ra.GenerateTimeTransformerV2.Config(sessions=None, sampling=..., quantize=None)
ra.GenerateTabTransformerV2.Config(records=None,  sampling=..., quantize=None)
```

`sessions` / `records` must be positive integers if set; `None` defaults to the training-set count. `quantize` is not wired through end to end — it is ignored with a warning.

### SSM extras

`TrainTimeSSM.Config` carries two fields the other families lack:

- `chunked_dataset_id` — train directly from a pre-built chunked-dataset artifact, skipping the inline chunker. Also picked up automatically when the input table carries a `chunked_dataset_id` column (from a `ChunkTimeSSM` parent or a `ChunkedDatasetLoad` source). In this path `encoder` and `relational` come from the artifact's metadata and the values you pass are only validated against it.
- `resume_from_checkpoint_model_id` — resume from a checkpoint image produced by a *previous* workflow. The SSM time trainer checkpoints every ~10 minutes and resumes automatically within a job (pod restart, suspend, crash), but that pointer lives in worker job state and does not survive losing the cluster. The phase and step are read from the image, so a child-phase checkpoint does not retrain the parent. An unreadable id logs a warning and trains from scratch rather than failing.

### `ChunkTimeSSM` — standalone pre-chunker

Runs the relational sub-session chunker once and persists a reusable artifact, moving GIL-heavy CPU work off the GPU trainer and onto a cheaper CPU worker set.

```python
chunk = ra.ChunkTimeSSM(ra.ChunkTimeSSM.Config(
    encoder=encoder,                     # same as TrainTimeSSM.encoder
    relational=relational_config,        # same as TrainTimeSSM.relational
    mask_rate=0.0,
    n_workers=1,                         # >1 parallelizes on a CPU pod; None = all CPUs
    labels={},
))
builder.add_path(dataset, chunk, train)
```

It emits a one-row table with a `chunked_dataset_id` column that a downstream `TrainTimeSSM` reads automatically.

### Sharded generation

`GenerateTimeSSM` runs on a single GPU, so the generate-time analogue of DDP is fanning out shards:

```python
model_load = ra.ModelLoad(ra.ModelLoad.Config(model_id=mid))
builder.add_action(model_load)
shards = ra.add_sharded_generate(
    builder, model_load, shards=4, sessions=200,
    config=ra.GenerateTimeSSM.Config(
        sampling=ra.GenerateTimeSSM.SamplingConfig(gen_batch=32),
    ),
)
builder.add_action(ra.DatasetSave(name="syn"), parents=shards)
```

Each shard gets a distinct seed (`seed_base + i`, default base 1029) — **this is load-bearing.** The trained model bakes in a fixed `random_state`, so without per-shard seeds every shard samples the same parents and you get N duplicate copies instead of a partition. `DatasetSave`'s default `concat_tables=True` concatenates the shards and offsets each shard's `session_key` into a contiguous range, so the output is indistinguishable from a single-pod run. `shards=1` reproduces ordinary single-pod behavior exactly.

This replaces a `SessionTarget` feedback loop: each shard generates a fixed count rather than gating on a running total.

## rtf v1 — REaLTabFormer (legacy)

Use only to reproduce an existing model.

```python
train = ra.TrainTabTransformer(ra.TrainTabTransformer.Config(
    rtf=ra.TrainTabTransformer.TrainConfig(num_bootstrap=100, epochs=100),
    encoder=encoder,
))
```

### `TrainTabTransformer.TrainConfig`

| Field | Default |
| --- | --- |
| `num_bootstrap` | **required — no default** |
| `epochs` | `10` |
| `batch_size` | `8` |
| `gradient_accumulation_steps` | `4` |
| `learning_rate` | `5e-5` |
| `weight_decay` | `0` |
| `n_critic` | `5` |
| `n_critic_stop` | `2` |
| `gpt2_config` | `GPT2Config(layer=12, head=12, embed=768)` |

### `TrainTimeTransformer.TrainConfig`

| Field | Default |
| --- | --- |
| `num_bootstrap` | **required — no default** |
| `n_critic` | `5` |
| `n_critic_stop` | `2` |
| `parent` | `ParentConfig(epochs=10, batch_size=8, gradient_accumulation_steps=4, learning_rate=5e-5, weight_decay=0, gpt2_config=GPT2Config())` |
| `child` | `ChildConfig(epochs=10, batch_size=8, gradient_accumulation_steps=4, learning_rate=5e-5, weight_decay=0, output_max_length=1000)` |

`GPT2Config(layer=12, head=12, embed=768)` — `embed` should be a multiple of `head`. The defaults work for most cases.

`TrainTimeTransformer.check_table` refuses upfront when average rows per session exceeds `child.output_max_length`, because every session would be filtered out and training would fail on an empty dataset. It suggests `output_max_length = avg_rows_per_session * 2`.

Generate configs are minimal: `GenerateTabTransformer.Config(records=None)` and `GenerateTimeTransformer.Config(sessions=None)`. `None` means the training-set count, capped at 1000.

## Corrections to the published hyperparameter table

`docs.rockfish.ai/models.html` and `model-train.html` disagree with the SDK in these places:

| Published | Actual |
| --- | --- |
| "four Gen AI models" | eight train actions across four families (GAN, SSM, rtf2, rtf v1) |
| `train_kwargs.gradient_accumulation_step` | `gradient_accumulation_steps` — plural, and flat on `TrainConfig` / `ParentConfig` / `ChildConfig`, not under a `train_kwargs` namespace |
| `transformer.gpt2_config.layer` | `gpt2_config.layer` — no `transformer` namespace |
| RF-Tab-Transformer `epochs` default 100 | `10` |
| RF-Time-Transformer `epochs` default 100 | `10`, on both `parent` and `child` |
| RF-Time-Transformer `child.output_max_length` default 512 | `1000` |
| RF-Tab-Transformer `output_max_length` | **no such field** on `TrainTabConfig`; only the time variant's `ChildConfig` has one |
| `num_bootstrap` unmentioned | required on both rtf v1 train configs |
| `ra.DatasetSave({"name": "synthetic"})` | `ra.DatasetSave(name="synthetic")` |
| `builder.add(model)` | `builder.add_model(model)` |
| `from rockfish.labs.recommender import ModelType` | `from rockfish.labs.steps import ModelType` — there is no `labs.recommender` module |
| `rl.metrics.assotication_score` | `rl.metrics.association_score` |
| RF-Time-Transformer example uses `ra.TrainTabTransformer.DatasetConfig` | should be `ra.TrainTimeTransformer.DatasetConfig` (same class, but the example also contains full-width `，` characters that are syntax errors) |
