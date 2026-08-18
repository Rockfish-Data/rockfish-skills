---
name: inject-scenarios
description: Inject synthetic scenarios (spike, outage, ramp, shift) into a time-series dataset using the Rockfish labs scenarios service. Use when a user wants to perturb a baseline time series for ML robustness testing, root-cause-analysis drills, or anomaly-detection evaluation — and optionally generate Q&A test cases from the perturbation. Trigger on phrases like "inject an anomaly", "add a spike to this series", "simulate an outage", "scenario injection", or mentions of `rockfish.labs.scenarios`.
---

# Inject scenarios

Use `rockfish.labs.scenarios.Client` to inject synthetic scenarios into a time-series dataset and optionally generate Q&A test cases from the perturbation.

## When to use this skill

Use when the user has (or will create) a baseline time-series dataset and wants to:

- Inject a **spike** at a single timestamp (set `measurement = magnitude`).
- Inject an **outage** across a time range (hold `measurement = outage_value` between `start_timestamp` and `end_timestamp`).
- Inject a **ramp** or **shift** (gradual or step change).
- Generate Q&A test cases describing the injected scenario.
- List every scenario derived from a source dataset.

If the user doesn't yet have a baseline dataset, use the `generate-from-schema` skill first to produce one.

## Concept

A *scenario* is a perturbation applied to a single `measurement` column in a time-series dataset, indexed by a `timestamp_column`. The scenarios service:

1. Reads the source dataset by `dataset_id`.
2. Applies the perturbation per the `config` payload.
3. Writes a new dataset containing the perturbed series, with lineage labels (`source_dataset_ids`) and scenario metadata (`scenario_config`) stamped onto the Arrow schema.

## How to use

```python
from rockfish.labs.scenarios import Client

async with rf.Connection.from_config() as conn:
    client = Client(conn)

    result = await client.inject(
        source_id,
        config={
            "type": "spike",                       # or outage, ramp, shift
            "timestamp_column": "timestamp",
            "measurement": "cpu_pct",
            "timestamp": "2026-01-16T00:00:00",    # spike-specific
            "magnitude": 99.0,                     # spike-specific
        },
        generate_tests=True,                       # optional
        max_cases=10,
        variations_per_question=2,
    )

    # result.dataset.dataset.id is the new perturbed dataset
    # result.test_cases is the Q&A list (when generate_tests=True)
```

To read the scenario config back from an injected dataset:

```python
config = await result.dataset.fetch_config(conn)
```

To list every scenario derived from a source:

```python
async for sd in client.list_for_source(source_id):
    print(sd.dataset.id, sd.scenario_type)
```

## URL derivation

`Client` derives the scenarios service URL from the connection's API host by replacing `api` with `manta` (e.g. `api.rockfish.ai` → `manta.rockfish.ai`). Pass `scenarios_url=...` explicitly if your deployment doesn't follow that convention.

## Reference implementation

See [`reference/scenarios.py`](reference/scenarios.py) — an end-to-end walkthrough that creates a baseline series, injects spike and outage scenarios, reads the scenario config back, and lists every derived dataset.

## Gotchas

- **Spike timestamps must align**: the `timestamp` in a spike config must fall on one of the rows in the source data; otherwise the spike has no row to modify.
- **Outage range is inclusive**: rows whose `timestamp` is in `[start_timestamp, end_timestamp]` are held at `outage_value`.
- **Test-case generation costs an LLM call per case**: keep `max_cases` modest while iterating.
- **Source must be a Rockfish dataset**: scenarios operate on uploaded datasets, not local pandas frames. Upload first via `conn.create_dataset(rf.Dataset.from_pandas(...))`.
