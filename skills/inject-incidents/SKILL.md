---
name: inject-incidents
description: Inject synthetic incidents (instantaneous spike, sustained magnitude change, data outage, value ramp) into a time-series dataset and evaluate analytics agents with `rockfish.agentfuel` — incident math and ground-truth generation run locally, no remote service. Covers the whole agentfuel toolkit; typed incident configs with boolean SQL test cases verified via DataFusion, Claude-backed natural-language prompts, variations, and answer grading, plus the pandas-based suite generators (analytics aggregation questions, scenario Q&A templates, stateful event/state analysis). Trigger on phrases like "inject an incident", "incident injection", "evaluate my analytics agent", "generate a test suite from my data", "ground-truth test cases from an anomaly", "event/state questions from an event log", "agentfuel", or mentions of `rockfish.agentfuel`. For the remote `rockfish.labs.scenarios` service, use the `inject-scenarios` skill instead.
---

# Inject incidents

Use `rockfish.agentfuel` to inject typed, reproducible incidents into a time-series dataset and turn each incident into an evaluation suite for an analytics agent: SQL test cases with known boolean answers, verified locally, optionally phrased as natural-language questions and LLM-graded. The same package also generates deterministic question/answer suites straight from data — analytics aggregations, scenario Q&A, and event/state analysis — covered under [Test-suite generators](#test-suite-generators) below.

## When to use this skill

Use when the user has (or will create) a baseline time-series dataset and wants to:

- Inject an **instantaneous spike** (set the measurement to `absolute_magnitude` at one timestamp).
- Inject a **sustained magnitude change** (add `delta_magnitude` across a time range).
- Inject a **data outage** (hold the measurement at `absolute_magnitude` across a time range).
- Inject a **value ramp** (linearly ramp the measurement across a time range).
- Restrict the incident to rows matching metadata filters (e.g. only `site == "store"`).
- Generate **ground-truth boolean test cases** (SQL + expected answer) from an incident and **verify them locally**.
- Phrase test cases as **natural-language questions**, generate intent-preserving **variations**, and **grade** an agent's answers.
- Generate an **analytics question suite** from any tabular dataset (aggregations × filters × time windows × group-bys, with reproducible ground truths).
- Generate **scenario Q&A** (detection, magnitude, duration, ...) from a pandas-level spike/outage/shift/ramp injection.
- Generate **event/state questions** from an entity-structured event log (funnels, sequences, time-between-events, state machines).

If the user doesn't yet have a baseline dataset, use the `generate-from-schema` skill first to produce one. If the user specifically wants the remote `rockfish.labs.scenarios` service (dict configs, server-side injection), use the `inject-scenarios` skill instead — `rockfish.agentfuel` is the local-first successor to that path.

## Concept

An *incident* is a perturbation of a single numeric measurement column, indexed by a timestamp column, optionally restricted to rows matching equality predicates (`MetadataPredicate(column_name, value)`). Configs are typed `attrs` classes, one per pattern:

| Config class | `pattern_type` | Key fields | Effect |
| --- | --- | --- | --- |
| `InstantaneousSpikeIncidentConfig` | `InstantaneousSpike` | `absolute_magnitude`, `timestamp` | set measurement to the magnitude at one timestamp |
| `SustainedMagnitudeChangeIncidentConfig` | `SustainedMagnitudeChange` | `delta_magnitude`, `start_timestamp`, `end_timestamp` | add the delta over the range |
| `DataOutageIncidentConfig` | `DataOutage` | `absolute_magnitude`, `start_timestamp`, `end_timestamp` | hold measurement constant over the range |
| `ValueRampIncidentConfig` | `ValueRamp` | `start_magnitude` and/or `end_magnitude`, `start_timestamp`, `end_timestamp` | linear ramp over the range |

All configs also take `impacted_measurement`, `timestamp_column`, and an optional `impacted_metadata_predicate` list.

Everything except dataset upload/download runs locally:

1. `Client.inject(source_id, config)` downloads the source, validates the config against it, applies the incident locally (sorting by timestamp first), stamps the config JSON into the injected table's Arrow schema metadata under `incident_config`, and uploads the result with lineage labels (`parent_dataset_id`, `has_incident`, `pattern_type`).
2. `Client.list_incidents(source_id)` streams incident datasets derived from a source, filtering on those labels.
3. `Client.generate_test_cases(incident_id)` recovers the config from the labels + schema metadata and builds full-coverage tests (queries that must see the incident) and no-coverage tests (wrong time window, wrong metadata — must not). Each `BooleanTestCase` is a SQL query returning a single boolean, written in the DataFusion dialect against a table named `my_table`.
4. `verify_test_cases(local_dataset, test_cases)` executes every query locally with DataFusion and reports pass/fail — a self-consistency check of the ground truth.
5. The NL layer is Claude-backed: `Client.generate_prompts` phrases each test case as question/answer pairs across user personas, `Client.prompt_variations` rewrites a question while a two-stage check keeps the computation identical, and `Client.evaluate` grades your agent's answer against the expected one on content (numbers, timestamps, yes/no agreement), not wording.

## How to use

Full flow against a Rockfish deployment:

```python
import rockfish as rf
from rockfish.agentfuel import (
    Client,
    InstantaneousSpikeIncidentConfig,
    MetadataPredicate,
    verify_test_cases,
)

async with rf.Connection.from_config() as conn:
    client = Client(conn)  # anthropic_api_key=... to override the env var

    config = InstantaneousSpikeIncidentConfig(
        impacted_measurement="views",
        absolute_magnitude=900,
        timestamp_column="timestamp",
        timestamp="2024-01-04T15:00:00",
        impacted_metadata_predicate=[MetadataPredicate("site", "store")],
    )
    incident = await client.inject(source.id, config)

    async for ds in client.list_incidents(source.id):
        print(ds.id, ds.metadata["labels"]["pattern_type"])

    test_cases = await client.generate_test_cases(incident.id)
    incident_local = await (await conn.get_dataset(incident.id)).to_local(conn)
    results = await verify_test_cases(incident_local, test_cases)

    phrased = await client.generate_prompts(incident.id, n_per_test_case=3)
    for item in phrased:
        for prompt in item.prompts:
            actual = answer_with_your_agent(prompt.nl_question)
            passed = client.evaluate(
                prompt.nl_question, actual, prompt.nl_expected_result
            )
```

The incident math, test-case generation, and verification also work with **no connection at all**, on a `LocalDataset`:

```python
from rockfish.agentfuel import generate_boolean_tests, verify_test_cases
from rockfish.agentfuel.incidents import apply_incident, validate_config_against_dataset

baseline = rf.Dataset.from_pandas("views", df)
validate_config_against_dataset(baseline.table, config)
injected_table = apply_incident(baseline.table, config)
test_cases = generate_boolean_tests(injected_table, config)
results = await verify_test_cases(
    rf.Dataset.from_table("views_spike", injected_table), test_cases
)
```

## Test-suite generators

Besides the incident path, `rockfish.agentfuel` ships three pandas-based suite generators. All are pure-local and deterministic (seeded RNG, template-based questions, ground truths computed from the data — no LLM, no service):

| Subsystem | Input shape | Produces |
| --- | --- | --- |
| `rockfish.agentfuel.analytics` | any tabular DataFrame | aggregation questions with scalar/entity answers |
| `rockfish.agentfuel.scenarios` | time-series DataFrame + a scenario config | scenario Q&A across question categories |
| `rockfish.agentfuel.stateful` | event log (entity, timestamp, event type) | per-entity event/state questions |

**analytics** — `discover_schema(df)` classifies columns by dtype (timestamp / measurement / categorical); `generate_suite(df, schema, timestamp_col=...)` builds a level-based suite (~40 cases) from bare aggregations up through filters, time windows, group-bys, and Highest/Lowest selectors. A `Query` is filters → time window → group-by → aggregation → selector, built from the operators in `rockfish.agentfuel.analytics.operators`, and `execute_query(query, df, timestamp_col)` computes its ground truth directly. Each test case carries an exact-computation manifest (`describe_query`); pass `verification_df=` (an independently loaded copy) to recompute every ground truth and raise on any mismatch — answers that don't describe the delivered data never ship. `column_aliases={"col": ["unit name", ...]}` emits customer-phrased unique-count questions that are never trimmed by `max_queries`.

**scenarios** — the pandas analogue of the incident path: `SpikeConfig` / `OutageConfig` / `ShiftConfig` / `RampConfig` (row filters as plain dicts, e.g. `filter=[{"column": "store", "value": "NYC"}]`), applied by `inject_scenario(df, config)`, which returns a modified copy. `ScenarioTestSuiteGenerator().generate(df, config, include_negative=True)` fills per-type question templates; ground truths come straight from the config or are computed from the injected data (the spike value is *read back from the dataset*, so an answer can never contradict it). Negative tests rephrase boolean detection questions against an unaffected entity and expect `False`.

**stateful** — `discover_event_schema(df, ...)` auto-detects the entity / timestamp / event-type columns and classifies the rest as entity attributes, event attributes, or measurements. `generate_suite(df, schema)` mirrors the analytics levels: bare event operators, plus entity filters, time windows, and state operators over auto-derived (or explicit `StateConfig`) state machines. Or build one `StatefulQuery` by hand — `EventCriteria` says which events matter, an operator (`EventExistence`, `TimeBetweenEvents`, `SequenceMatch`, `DropOff`, `StateDuration`, ...) computes per-entity results, and `execute_stateful_query(query, df)` returns a `StatefulResult` whose `summary_value` is the test-case answer.

**Timezone convention (shared)** — `rockfish.agentfuel.timeutil` puts everything on the UTC timeline: naive timestamps are treated as UTC, and question/answer text matches the dataset's timezone-ness (naive data gets naive wall-clock timestamps, aware data gets ISO 8601 UTC with a `Z` suffix).

## Requirements

- Incident injection, test-case generation, and local verification need only the base `rockfish` package.
- The NL methods (`generate_prompts`, `prompt_variations`, `evaluate`) need the optional extra plus an Anthropic key:

```sh
pip install 'rockfish[agentfuel]'
export ANTHROPIC_API_KEY=...
```

## Reference

Read these when you need detail beyond the tables above:

- [`reference/api-reference.md`](reference/api-reference.md) — every class, field, default, and function contract in `rockfish.agentfuel`: incident configs and validation rules, test-case recipes per pattern, `Client` methods, the NL layer, all analytics/scenarios/stateful operators with their fields, and the timezone conventions.
- [`reference/incidents.py`](reference/incidents.py) — runnable incident path end to end: baseline → spike injection → ground-truth test cases verified locally, then the same flow through `Client` when a Rockfish connection is configured, and NL phrasing/variations/grading when an Anthropic key is set. Exits non-zero if any check fails, so it works as a smoke test.

## Gotchas

- **Spike timestamps must align**: `InstantaneousSpikeIncidentConfig.timestamp` is matched by equality, so it must fall exactly on a row in the source data; otherwise no row is modified.
- **Ranges are inclusive**: rows with `start_timestamp <= t <= end_timestamp` are affected.
- **Column types are validated**: `timestamp_column` must be an Arrow timestamp type and `impacted_measurement` must be int or float; predicate columns must exist and contain the predicate value. Violations raise `ValueError` before anything is modified.
- **`ValueRamp` needs at least one endpoint**: omit `start_magnitude` or `end_magnitude` (not both) and the missing endpoint falls back to the dataset's own first/last value in the window. Integer measurement columns are promoted to float when the ramp produces fractional values.
- **No-coverage tests need contrast**: the "wrong metadata" tests pick a *different* value for each predicate column, so a config with no predicates — or predicates on single-valued columns — yields full-coverage tests only.
- **`generate_test_cases` only works on datasets made by `Client.inject`**: it needs the `pattern_type` label and the `incident_config` schema metadata; anything else raises `ValueError`.
- **Test-case SQL targets `my_table`**: `LocalDataset.sql()` registers the dataset under that name, so queries run as-is. The dialect is DataFusion (e.g. `COUNT(*) > 0` instead of `EXISTS`).
- **Reproducible test cases**: `generate_boolean_tests(..., rng=random.Random(seed))` pins the "wrong metadata" value choices.
- **Bring your own agent**: there is no simulated customer agent — run your agent on each `nl_question` and grade the answer with `Client.evaluate`.
- **Suite questions are templates, not prose**: the analytics/scenarios/stateful generators are deterministic (seeded RNG) and template-phrased. Use `rockfish.agentfuel.nl.variations` to diversify phrasings without changing the computation.
- **`generate_suite` skips failures silently**: analytics queries that error or return empty results are dropped, and only single-value answers are kept (no list answers) — expect fewer cases than queries.
- **Scenario negatives need a detection template and contrast**: negative (`coverage="none"`) cases exist only for scenario types with a boolean detection template (currently outages), and only when the config has an `Equals` filter on a column with more than one distinct value.
- **Import paths off the beaten track**: `inject_scenario` lives in `rockfish.agentfuel.scenarios.injector`, and `discover_event_schema` / the stateful `generate_suite` live in `rockfish.agentfuel.stateful.schema` / `.suite_builder` — they are not re-exported from the subpackage roots.
- **Naive timestamps are graded as UTC** across all suite generators; mixed naive/aware columns are forced onto the UTC timeline.
- **This skill targets rockfish 0.79.0+** — `rockfish.agentfuel` first shipped in 0.79.0. On an older SDK the imports fail immediately; the reference script reports the required version instead of a bare `ImportError`.
