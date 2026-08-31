# `rockfish.agentfuel` API reference

> Field-level reference for the agentfuel package, written for this skill. There is no
> published doc page for `rockfish.agentfuel` yet, so everything here was extracted from
> the SDK source and docstrings. Defaults were machine-checked against `attrs.fields()`.
> Regenerate from the SDK when the package changes.

Defaults are shown as `= value`; fields with no default are required. Everything is
importable from `rockfish.agentfuel` unless a deeper module path is shown.

## Incidents (`rockfish.agentfuel.incidents`)

`MetadataPredicate(column_name, value)` — equality predicate selecting impacted rows.

All four configs share `impacted_measurement: str`, `timestamp_column: str`, and
`impacted_metadata_predicate: list[MetadataPredicate] = []`. Timestamps may be ISO
strings or `datetime`; ranges are inclusive.

| Config | `pattern_type` | Own fields | Effect |
| --- | --- | --- | --- |
| `InstantaneousSpikeIncidentConfig` | `InstantaneousSpike` | `absolute_magnitude`, `timestamp` | set measurement to the magnitude at one timestamp (equality match) |
| `SustainedMagnitudeChangeIncidentConfig` | `SustainedMagnitudeChange` | `delta_magnitude`, `start_timestamp`, `end_timestamp` | add the delta over the range |
| `DataOutageIncidentConfig` | `DataOutage` | `absolute_magnitude`, `start_timestamp`, `end_timestamp` | hold measurement constant over the range |
| `ValueRampIncidentConfig` | `ValueRamp` | `start_timestamp`, `end_timestamp`, `start_magnitude = None`, `end_magnitude = None` | linear ramp over the range |

`ValueRampIncidentConfig` raises `ValueError` when both magnitudes are `None`; an omitted
endpoint falls back to the dataset's own first/last value in the window, and integer
measurement columns are promoted to float when the ramp is fractional.

- `validate_config_against_dataset(table, config)` — `ValueError` when the timestamp
  column is missing/not an Arrow timestamp, the measurement is missing/not numeric, or a
  predicate column/value is absent.
- `apply_incident(table, config) -> pa.Table` — sorts by the timestamp column, then
  applies; rows must match every predicate AND the time constraint.
- `config_to_metadata_bytes` / `config_from_metadata_bytes(raw, pattern_type)` — the JSON
  round-trip stored under Arrow schema metadata key `INCIDENT_CONFIG_METADATA_KEY`
  (`b"incident_config"`).

## Boolean test cases and local execution

`BooleanTestCase(sql_query, expected_result, coverage_type, description)` — DataFusion
SQL against a table named `my_table`, returning one boolean; `coverage_type` ∈
`full`/`partial`/`none`.

`generate_boolean_tests(table, config, *, rng=None)` — `table` must already contain the
incident; pass a seeded `random.Random` for reproducibility. Builds per pattern:
spike → 4 full (`>= magnitude` in ±15min/30min/1h/2h windows) + 1 no-coverage;
sustained change → 1 full (window average within 0.001 of the impacted-rows mean) + 1;
outage → 1 full (`COUNT(*) = 0` of rows `!= magnitude` in the window) + 1;
ramp → 3 full (start/end/change values within 0.001) + 3. No-coverage tests substitute a
*different* value for each predicate (skipped for single-valued columns; none survive →
no test) and a wrong time window ~10% of the dataset span away from the incident.

`execute_test_case(dataset, test_case) -> bool` — runs the SQL via `LocalDataset.sql`
(which registers the table as `my_table`); `ValueError` unless the result is a single
boolean scalar; empty/NULL counts as `False`. `verify_test_cases(dataset, test_cases)`
returns `TestCaseResult`s with a `passed` property.

## `Client` and the NL layer

`Client(conn, *, anthropic_api_key=None, nl_model=nl.DEFAULT_MODEL)`:

| Method | Contract |
| --- | --- |
| `await inject(source_dataset_id, config)` | download → validate → `apply_incident` → stamp config metadata → upload with labels `parent_dataset_id`, `has_incident="True"`, `pattern_type` |
| `list_incidents(source_dataset_id, *, limit=10)` | streams incident datasets by those labels |
| `await generate_test_cases(incident_dataset_id)` | recovers the config from label + metadata; `ValueError` if either is missing (not made by `inject`) |
| `await generate_prompts(incident_dataset_id, *, n_per_test_case=5)` | test cases → `PhrasedTestCase`s via Claude |
| `evaluate(prompt, actual_result, expected_result) -> bool` | LLM-grades content match (numbers, timestamps, yes/no), not tone |
| `prompt_variations(prompt, num_variations=5)` | intent-preserving rewrites |

`rockfish.agentfuel.nl` (needs `pip install 'rockfish[agentfuel]'` + an Anthropic key;
`DEFAULT_MODEL = "claude-fable-5"`): `phrase_test_cases(test_case, n_nl_test_cases=10)`
→ `NLTestCase(nl_question, nl_expected_result)` pairs across personas;
`grade(prompt, actual, expected) -> bool`; `variations(prompt, num_variations=5,
verify_intent=True)` — 1–20 variations, verified to keep every verbatim
timestamp/number and the same computation.

## Analytics suites (`rockfish.agentfuel.analytics`)

`Query(filters=None, time_window=None, group_by=None, aggregation, selector=None)` —
executed in that order by `execute_query(query, df, timestamp_col=None)`, returning
`GroundTruth(value, winning_entity=None, group_values=None)`.

Operators (`analytics.operators`): aggregations `Avg/Sum/Max/Min/Std/NUnique(column)`,
`Count(column="*")`; filters `Equals(column, value)`, `In(column, values)`,
`GreaterThan/LessThan(column, value)`, `Between(column, low, high, inclusive=True)`;
time windows `FullRange()`, `FirstN/LastN(n, unit)` (unit `"hours"/"days"/"weeks"/"months"`),
`SingleDay(date)`, `TimeBetween(start, end)`; selectors `Highest()`, `Lowest()`,
`TopK/BottomK(k)`, `AboveThreshold/BelowThreshold(threshold)`.

- `discover_schema(df, max_unique_values=50)` — datetime dtype → timestamp, numeric →
  measurement, else categorical.
- `generate_suite(df, schema, max_queries=None, timestamp_col=None, column_aliases=None,
  verification_df=None)` → `(list[TestCase], coverage)` — level-based (~40 cases), seeded
  internally, dedupes by operator signature, keeps only single-value answers, silently
  skips failing/empty queries. Each `TestCase.calculation` is the exact-computation
  manifest (`suite_builder.describe_query`). `verification_df` recomputes every answer
  against a fresh copy and raises on any mismatch. `column_aliases={"col": [...]}` emits
  customer-phrased unique-count questions never trimmed by `max_queries`.

## Scenario suites (`rockfish.agentfuel.scenarios`)

Configs share `timestamp_column`, `measurement`, `filter: list[dict] = []` (parsed by
`parse_filters`: `{"column","value"}`→Equals, `{"column","values"}`→In,
`{"column","low","high"}`→Between, `{"column","gt"|"lt"}`→Greater/LessThan):
`SpikeConfig(timestamp, magnitude)`, `OutageConfig(start_timestamp, end_timestamp,
outage_value=0.0)`, `ShiftConfig(start_timestamp, end_timestamp, delta)`,
`RampConfig(start_timestamp, end_timestamp, start_value, end_value)`.

- `inject_scenario(df, config)` (in `scenarios.injector` — **not** re-exported) — returns
  a modified copy; mask = filters ∧ time constraint, on the UTC timeline.
- `ScenarioTestSuiteGenerator().generate(df, config, include_negative=True,
  max_cases=None)` → `ScenarioTestCase(question, answer, answer_type, category,
  ground_truth, coverage)`. Questions come from per-type templates across categories
  (detection, characterization, localization, magnitude, duration, impact, context,
  projection); answers come from the config (`GroundTruthSource.CONFIG`) or are computed
  from the injected data, so they can never contradict it. Questions whose required data
  is missing or whose answer is `None` are skipped. Negative (`coverage="none"`) cases
  rephrase boolean *detection* templates against an unaffected value of the first
  `Equals` filter's column and expect `False` — they need such a filter, >1 distinct
  value, and a type with a detection template (currently outage only).

## Stateful suites (`rockfish.agentfuel.stateful`)

- `EventCriteria(event_type=None, event_type_column, conditions=[], negation=False)` —
  which events matter, using the analytics filter operators; `event_type=None` = any.
- `StateConfig(state_name, entry_criteria, exit_criteria=[], timeout_duration=None)` —
  durations are strings like `"30 days"` (seconds→months; month = 30 days).
- `StatefulQuery(entity_column, timestamp_column, event_type_column, entity_filter=None,
  time_window=None, operator)` — filter/time-window slots reuse the analytics operators.
- Every operator returns `StatefulResult(...)`; `summary_value` is the test-case answer.

Event operators (`stateful.operators`): `EventExistence(criteria)`;
`TimeBetweenEvents(start_criteria, end_criteria, within_duration=None, metric="avg")`;
`SequenceMatch(steps, allow_gaps=True, max_step_duration=None)`;
`CountAfterTrigger(trigger_criteria, target_criteria, within_duration=None)`;
`DropOff(required_criteria, absent_criteria, within_duration=None)`;
`HasButNot(required_criteria, absent_criteria)`;
`InterRowDiff(column, metric="cv", convert_timedelta=True, min_rows=2)`;
`ColumnTransitions(column, deduplicate_consecutive=True, max_path_length=20)`.
State operators all take `state_configs`: `StateReached(target_state)`,
`StateDuration(target_state, metric="avg")`, `StateTransitions()`,
`EventsInState(target_state, target_criteria)`, `StateOccupancyOp()`.

- `discover_event_schema(df, entity_column=None, timestamp_column=None,
  event_type_column=None)` (in `stateful.schema` — **not** re-exported) — auto-detects by
  dtype, cardinality, and name hints; raises `ValueError` asking for the column when
  detection fails. Remaining columns: numeric → measurement, constant-per-entity →
  entity attribute, else event attribute.
- `execute_stateful_query(query, df)` (in `stateful.executor`) — entity filter → time
  window → operator; empty frame → `summary_value=None`.
- `generate_suite(df, schema, max_queries=None, state_configs=None)` (in
  `stateful.suite_builder`) — levels from bare event ops to combined state ops; state
  configs auto-derived when not given; empty/failing queries are skipped.

## Timezone conventions (`rockfish.agentfuel.timeutil`)

Everything is placed on the UTC timeline and **naive values are treated as UTC**.
Question/answer text matches the dataset's timezone-ness: fully naive data renders naive
wall-clock timestamps; aware data renders ISO 8601 UTC with a `Z` suffix. A column with
*any* aware value counts as aware.
