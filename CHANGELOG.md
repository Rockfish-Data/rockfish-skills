# Changelog

## 0.3.0

### Added: `generate-from-data`

A skill for the other half of synthetic data: generating it from data you already have,
rather than inventing rows from a schema. It is the counterpart to `generate-from-schema`,
and the pair splits on one question - do you have data, or only a description of it?

It is organized as a **loop**, not a pipeline: analyze -> set the quality target -> prepare
-> train -> generate -> evaluate -> repeat. Setting the target comes before training on
purpose. `noise_floor()` needs only the real data and tells you the ceiling; `CardSpec`
is derived from the same profile that configures training; and the SSM/rtf2 trainers take
the 0.85 gate as `quality_check.min_score`, so the metric is a training input rather than
a postscript. Step 6 carries a diagnosis table mapping each failing score to the step that
owns it - more often step 1 or 3 than "train for longer".

It documents the Rockfish SDK as it actually is, which is ahead of
[docs.rockfish.ai](https://docs.rockfish.ai) in three places:

- **Eight train actions, not four.** Alongside RF-Time-GAN and RF-Tab-GAN, the SDK ships
  the SSM family (`TrainTabSSM` / `TrainTimeSSM`) and rtf2
  (`TrainTabTransformerV2` / `TrainTimeTransformerV2`), which supersedes the REaLTabFormer
  v1 actions. SSM and rtf2 share a flat `encoder` / `model` / `train` / `quality_check`
  config and an RFScore training stop.
- **`rockfish.labs.dataset_profiler`** (0.79.0) - `profile_table` -> `recommend` ->
  `build_train_workflow`, with the drop rules, model routing, chunk planning, and
  state-machine detection written down.
- **`rockfish.labs.report_card`** (0.81.0) - `RFScore = min(marginal, correlation,
  association)` against a noise floor, plus the measurement guards that keep a score
  honest.

It also writes down two things the docs do not cover:

- **Bounded numeric columns** - a column capped by a constant or by another column
  (`usage` <= `capacity`). Clipping the model's output piles mass on the boundary;
  training in logit space and inverting with a sigmoid makes the bound hold by
  construction. Includes endpoint inflation, because `logit(0)` and `logit(1)` are
  infinite and real data hits both.
- **Two silent scoring traps** found while verifying the skill: `tv_distance` returns
  1.0 - the worst possible score - when one side is arrow `large_string` and the other
  `string`, with no error; and `marginal_dist_score` raises on any boolean column unless
  it is named in `other_categorical`.

`reference/train-generate.py` runs end to end and was verified against rockfish 0.82.2
and a live backend. Its first two examples need no credentials and no GPU.

## 0.2.0 — breaking

### Removed: `inject-scenarios`

The `inject-scenarios` skill is gone. It documented `rockfish.labs.scenarios`, which
called the remote `manta` service — and that module **no longer exists in the SDK** as of
rockfish 0.79.0. Code written against it fails at import, not at runtime.

Use [`inject-incidents`](skills/inject-incidents/) instead. It does the same job with
`rockfish.agentfuel`, which runs the injection math locally, with no service.

**Migrating.** Swap the dict `config=` payload for the matching typed config:

| `rockfish.labs.scenarios` | `rockfish.agentfuel` |
| --- | --- |
| `{"type": "spike", ...}` | `InstantaneousSpikeIncidentConfig` |
| `{"type": "outage", ...}` | `DataOutageIncidentConfig` |
| `{"type": "shift", ...}` | `SustainedMagnitudeChangeIncidentConfig` |
| `{"type": "ramp", ...}` | `ValueRampIncidentConfig` |

Field names change too: `measurement` → `impacted_measurement`, and the magnitude field
is `absolute_magnitude` (spike, outage), `delta_magnitude` (sustained change), or
`start_magnitude` / `end_magnitude` (ramp — renamed from `start_value` / `end_value`).
Row filters move from ad-hoc keys to
`impacted_metadata_predicate=[MetadataPredicate(col, value)]`.

Ramp takes at least one endpoint: both fields default to `None`, but omitting *both*
raises `ValueError` from the constructor. Supply one and the other falls back to the
dataset's own first or last value in the window.

For the smallest possible diff, `rockfish.agentfuel.scenarios` stays closer to the old
shape: `SpikeConfig` / `OutageConfig` / `ShiftConfig` / `RampConfig` all keep
`timestamp_column`, `measurement`, and `filter`. `SpikeConfig` is a near drop-in — it
also keeps `timestamp` and `magnitude`. The three range types differ: they take
`start_timestamp` / `end_timestamp` plus their own value field (`outage_value`, `delta`,
or `start_value` / `end_value`). These run on a pandas DataFrame via
`inject_scenario(df, config)` — no upload and no connection — rather than on an
uploaded Rockfish dataset.

Agents that routed to `inject-scenarios` by natural language ("inject an anomaly",
"simulate an outage", "scenario injection") need no change — `inject-incidents` carries
those trigger phrases. Only automation that names the skill `inject-scenarios`
explicitly has to be repointed.
