"""End-to-end walkthrough of `rockfish.labs.scenarios`.

Inject a synthetic scenario (spike, outage, shift, ramp) into a time-series
dataset, optionally generate Q&A test cases, read the scenario config back
from the injected dataset, and list every scenario derived from a source.

The script talks to a live Rockfish environment (REST API and the `manta`
scenarios service). Configure your endpoint via
`~/.config/rockfish/config.toml` or the `ROCKFISH_*` environment variables.

Plots of the source and injected series are saved as PNG files in
`output/scenarios/` (at the repo root). Each path is printed when the
plot is written, and a summary of all plot paths is printed at the end
of the run.

Run:
    python examples/scenarios.py
    python examples/scenarios.py --help
"""
import argparse
import asyncio
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

import rockfish as rf
from rockfish.labs.scenarios import Client


PLOT_DIR = Path(__file__).resolve().parents[1] / "output" / "scenarios"


async def main() -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    plot_paths: list[Path] = []

    async with rf.Connection.from_config() as conn:
        # === 1. Create a source time-series dataset ===
        # Scenarios operate on a `timestamp_column` and a numeric
        # `measurement`. Build 48 hourly readings of a `cpu_pct` metric
        # for a single host and upload it so the scenarios service has
        # a real `dataset_id` to read.
        timestamps = pd.date_range("2026-01-15T00:00:00", periods=48, freq="h")
        source_df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "host": ["web-1"] * len(timestamps),
                "cpu_pct": [round(20 + 5 * (i % 7), 2) for i in range(len(timestamps))],
            }
        )
        print(source_df.head())

        source = await conn.create_dataset(rf.Dataset.from_pandas("cpu-baseline", source_df))
        source_id = source.id
        print("source dataset id:", source_id)

        # Plot the source series as a baseline.
        plt.figure()
        plt.plot(source_df["timestamp"], source_df["cpu_pct"], label="source")
        plt.legend()
        source_plot = PLOT_DIR / "01-source-series.png"
        plt.savefig(source_plot)
        plt.close()
        plot_paths.append(source_plot)
        print(f"  >> saved plot: {source_plot.resolve()}")

        # === 2. Construct the scenarios client ===
        # `Client` derives the scenarios service URL from the connection's
        # API host by replacing `api` with `manta` (e.g. `api.rockfish.ai`
        # -> `manta.rockfish.ai`). Pass `scenarios_url=...` explicitly if
        # your deployment doesn't follow that convention.
        client = Client(conn)
        print("scenarios url:", client.scenarios_url)

        # === 3. Inject a spike scenario with test-case generation ===
        # A `spike` sets `measurement` to `magnitude` at a single
        # `timestamp`. The `timestamp` must fall on one of the rows
        # in the source data.
        result = await client.inject(
            source_id,
            config={
                "type": "spike",
                "timestamp_column": "timestamp",
                "measurement": "cpu_pct",
                "timestamp": "2026-01-16T00:00:00",
                "magnitude": 99.0,
            },
            generate_tests=True,
            max_cases=10,
            variations_per_question=2,
        )

        print("source dataset id :", result.source_dataset_id)
        print("injected dataset  :", result.dataset.dataset.id)
        print("scenario type     :", result.dataset.scenario_type)
        print("lineage           :", result.dataset.source_dataset_ids)

        for tc in result.test_cases or []:
            print(tc.question, "->", tc.answer)
            for variant in tc.variations:
                print("   variant:", variant)

        # === 4. Inspect the injected data ===
        # Pull the injected dataset back as a pandas frame and confirm
        # the spike landed.
        injected_df = (await conn.query_dataset(result.dataset.dataset.id)).to_pandas()
        print(injected_df[injected_df["cpu_pct"] == injected_df["cpu_pct"].max()])

        # Overlay source and injected so the spike stands out.
        plt.figure()
        plt.plot(source_df["timestamp"], source_df["cpu_pct"], label="source")
        injected_df["timestamp"] = pd.to_datetime(injected_df["timestamp"])
        plt.plot(injected_df["timestamp"], injected_df["cpu_pct"], label="injected (spike)")
        plt.legend()
        spike_plot = PLOT_DIR / "02-spike-overlay.png"
        plt.savefig(spike_plot)
        plt.close()
        plot_paths.append(spike_plot)
        print(f"  >> saved plot: {spike_plot.resolve()}")

        # === 5. Read the scenario config from Arrow schema metadata ===
        # `fetch_config` decodes the `b"scenario_config"` key the server
        # stamps onto the injected dataset's Arrow schema. Returns `None`
        # if the dataset has no scenario metadata.
        print(await result.dataset.fetch_config(conn))

        # === 6. Inject a range-based scenario (outage) ===
        # An `outage` holds `measurement` at `outage_value` across
        # `[start_timestamp, end_timestamp]`.
        outage = await client.inject(
            source_id,
            config={
                "type": "outage",
                "timestamp_column": "timestamp",
                "measurement": "cpu_pct",
                "start_timestamp": "2026-01-15T06:00:00",
                "end_timestamp": "2026-01-15T10:00:00",
                "outage_value": 0,
            },
        )
        print("injected dataset:", outage.dataset.dataset.id, outage.dataset.scenario_type)

        # === 7. List every scenario derived from the source ===
        # `list_for_source` filters on the `source_dataset_ids` lineage
        # label, so it returns both the spike and the outage injected
        # above.
        async for sd in client.list_for_source(source_id):
            print(sd.dataset.id, sd.scenario_type, sd.source_dataset_ids)

    # Summary of generated artifacts.
    print("\n" + "=" * 72)
    print("Plots saved to:")
    for p in plot_paths:
        print(f"  {p.resolve()}")
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    return parser.parse_args()


if __name__ == "__main__":
    parse_args()  # supports --help
    asyncio.run(main())
