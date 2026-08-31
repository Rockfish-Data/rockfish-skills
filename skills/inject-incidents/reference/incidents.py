"""Walkthrough of `rockfish.agentfuel` incident injection.

Inject a synthetic incident into a time-series dataset, generate ground-truth
boolean test cases, and verify them locally with DataFusion — no service. Three
layers, each skipped cleanly when its prerequisites are missing:

1. Local (always runs): baseline -> spike -> test cases -> verify.
2. Remote (needs a Rockfish connection: ~/.config/rockfish/config.toml or
   ROCKFISH_* env vars): the same flow through `Client.inject`, with lineage.
3. Natural language (needs ANTHROPIC_API_KEY and `pip install
   'rockfish[agentfuel]'`): phrase, vary, and grade test cases with Claude.

Verifications print PASS/FAIL lines and the script exits non-zero on any
failure, so it works as a smoke test. Requires rockfish >= 0.79.0.

Run:
    python skills/inject-incidents/reference/incidents.py
"""
import argparse
import asyncio
import os
import random
import sys

import numpy as np
import pandas as pd

# Guard every rockfish import: a missing package raises from the very first
# import, which would sail past a guard placed any later.
MIN_SDK = "0.79.0"

try:
    import rockfish as rf
    from rockfish.agentfuel import Client
    from rockfish.agentfuel import InstantaneousSpikeIncidentConfig
    from rockfish.agentfuel import MetadataPredicate
    from rockfish.agentfuel import generate_boolean_tests
    from rockfish.agentfuel import verify_test_cases
    from rockfish.agentfuel.incidents import apply_incident
    from rockfish.agentfuel.incidents import validate_config_against_dataset
except ImportError as exc:
    import importlib.metadata as _md

    try:
        _have = _md.version("rockfish")
    except _md.PackageNotFoundError:
        _have = "not installed"
    verb = "needs" if _have == "not installed" else "needs at least"
    raise SystemExit(
        f"This example {verb} rockfish {MIN_SDK} (found: {_have}).\n"
        f"  {exc}\n"
        "Install or upgrade with:\n"
        "  uv pip install --find-links https://packages.rockfish.ai "
        "--upgrade 'rockfish[labs]'"
    ) from exc


FAILURES: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    """Record and print a pass/fail line; failures set the process exit code."""
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{suffix}")
    if not passed:
        FAILURES.append(f"{label}{suffix}")


def build_baseline() -> pd.DataFrame:
    """A week of hourly page views for two sites; we'll spike one of them."""
    rng = np.random.default_rng(7)
    timestamps = pd.date_range("2024-01-01", periods=24 * 7, freq="h")
    return pd.concat(
        [
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "site": site,
                    "views": rng.poisson(lam, size=len(timestamps)),
                }
            )
            for site, lam in [("store", 120), ("blog", 40)]
        ],
        ignore_index=True,
    )


def spike_config() -> InstantaneousSpikeIncidentConfig:
    """Shared by the local and remote walkthroughs. The timestamp must fall
    exactly on a row; the predicate limits the change to site == "store"."""
    return InstantaneousSpikeIncidentConfig(
        impacted_measurement="views",
        absolute_magnitude=900,
        timestamp_column="timestamp",
        timestamp="2024-01-04T15:00:00",
        impacted_metadata_predicate=[MetadataPredicate("site", "store")],
    )


def print_results(label: str, results) -> None:
    """Every ground-truth query must pass against the injected data."""
    for r in results:
        print(f"  {r.test_case.coverage_type.value:>4}  expected="
              f"{r.test_case.expected_result!s:5}  {r.test_case.description}")
    check(
        f"{label}: all ground-truth queries verify",
        all(r.passed for r in results),
        f"{sum(r.passed for r in results)}/{len(results)} passed",
    )


async def local_walkthrough(source_df: pd.DataFrame):
    print("\n=== Local walkthrough (no credentials needed) ===")

    # Validate, then apply the incident. The other three types (sustained
    # change, outage, ramp) work the same way with range timestamps — see the
    # skill's api-reference.md.
    baseline = rf.Dataset.from_pandas("views", source_df)
    spike = spike_config()
    validate_config_against_dataset(baseline.table, spike)
    spike_table = apply_incident(baseline.table, spike)

    # Ground-truth test cases: full-coverage queries must see the incident,
    # no-coverage queries (wrong window, wrong site) must not. Each is
    # DataFusion SQL against a table named `my_table` — `LocalDataset.sql()`
    # registers the dataset under that name. The seeded rng pins the "wrong
    # metadata" value choices.
    spike_tests = generate_boolean_tests(spike_table, spike, rng=random.Random(0))
    spike_dataset = rf.Dataset.from_table("views_spike", spike_table)
    print_results("local spike", await verify_test_cases(spike_dataset, spike_tests))

    return spike_tests


async def remote_walkthrough(source_df: pd.DataFrame) -> None:
    # `Client.inject` downloads the source, applies the incident locally,
    # stamps the config into Arrow schema metadata (key `incident_config`),
    # and uploads with lineage labels (parent_dataset_id, has_incident,
    # pattern_type).
    try:
        conn = rf.Connection.from_config()
    except Exception as exc:
        print(f"\n=== Remote walkthrough skipped (no connection: {exc!r}) ===")
        return

    print("\n=== Remote walkthrough (Client) ===")
    async with conn:
        client = Client(conn)

        source = await conn.create_dataset(rf.Dataset.from_pandas("views", source_df))
        incident = await client.inject(source.id, spike_config())
        print("source:", source.id, "-> incident:", incident.id)

        # Incident datasets derived from a source are discoverable by label.
        async for ds in client.list_incidents(source.id):
            print(ds.id, ds.metadata.get("labels", {}).get("pattern_type"))

        # The config is recovered from the labels + schema metadata, so test
        # cases can be generated from just the incident dataset id.
        test_cases = await client.generate_test_cases(incident.id)
        incident_local = await (await conn.get_dataset(incident.id)).to_local(conn)
        print_results("remote spike", await verify_test_cases(incident_local, test_cases))


def nl_walkthrough(spike_tests) -> None:
    # Claude-backed. With a Connection, the same steps are available as
    # Client.generate_prompts / prompt_variations / evaluate.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n=== Natural-language walkthrough skipped (no ANTHROPIC_API_KEY) ===")
        return

    from rockfish.agentfuel import nl

    print("\n=== Natural-language walkthrough (Claude) ===")
    prompts = nl.phrase_test_cases(spike_tests[0], 3)
    for prompt in prompts:
        print("Q:", prompt.nl_question)
        print("A:", prompt.nl_expected_result)

    print("variations:", nl.variations(prompts[0].nl_question, num_variations=3))

    # Grade an answer your own agent produced: content match (numbers,
    # timestamps, yes/no agreement), not wording.
    actual = "There was a spike of 900 views on the store site on Jan 4 at 3pm."
    print(
        "graded:",
        nl.grade(prompts[0].nl_question, actual, prompts[0].nl_expected_result),
    )


async def main() -> None:
    source_df = build_baseline()
    spike_tests = await local_walkthrough(source_df)
    await remote_walkthrough(source_df)
    nl_walkthrough(spike_tests)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    ).parse_args()  # supports --help
    asyncio.run(main())
