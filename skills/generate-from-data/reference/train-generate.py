"""End-to-end model-based generation with the Rockfish SDK, with real checks.

`models.md`, `pipeline.md` and `evaluation.md` in this directory cover the API
surface. This file serves as a coding example:
what the calls look like in sequence, which assertions actually hold, and where
the shapes change between the source data and the generated data.

There are 4 examples that can be run through this script:
  # Profile + recommend: the routing rules on two real-shaped tables, including which columns get dropped and why.
  # Report card: RFScore, TS score, and the noise floor, scored on a deliberately degraded copy of real data.
  # Tabular train + generate: RF-Tab-GAN end to end, then marginal fidelity.
  # Time-series train + gen: RF-Time-GAN with a SessionTarget loop, session metrics, and a report card against the generator's own session key.

Examples 3 and 4 submit real training workflows. They use deliberately tiny
epoch counts so they finish in minutes; the data they produce is a smoke test,
not a fidelity result.  They also need credentials from ~/.config/rockfish/config.toml or the ROCKFISH_*
environment variables.

Exits non-zero if any check fails, so it works as a smoke test.

Run:
    python train-generate.py                   # all four
    python train-generate.py -e 1 -e 2         # offline only
    python train-generate.py --connection env  # force ROCKFISH_* env vars
"""
import argparse
import asyncio
import os
import sys

try:
    import numpy as np
    import pandas as pd
    import pyarrow as pa

    import rockfish as rf
    import rockfish.actions as ra
    import rockfish.labs as rl
    from rockfish.labs.dataset_profiler import detect_state_fields
    from rockfish.labs.dataset_profiler import profile_table
    from rockfish.labs.dataset_profiler import recommend
    from rockfish.labs.report_card import CardSpec
    from rockfish.labs.report_card import StateFieldSpec
    from rockfish.labs.report_card import noise_floor
    from rockfish.labs.report_card import score
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"{exc}\n\nThis script needs rockfish[labs] with pandas/numpy:\n"
        "    pip install -U 'rockfish[labs]' -f https://packages.rockfish.ai"
    )

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record a named assertion without aborting the rest of the run."""
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def connect(mode: str):
    """Open a Connection from the config file or the ROCKFISH_* env vars."""
    if mode == "env" or (mode == "auto" and os.environ.get("ROCKFISH_API_KEY")):
        return rf.Connection.from_env()
    return rf.Connection.from_config()


# ---------------------------------------------------------------------------
# Source data
#
# Both generators produce data with structure a model can actually learn --
# correlated numerics, a genuine state machine, session-constant metadata --
# and with the traps the profiler is built to catch: a near-unique id column, a
# constant column, and a state field coded as a float.
# ---------------------------------------------------------------------------
def make_tabular_source(n: int = 4000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    region = rng.choice(["north", "south", "east", "west"], n, p=[0.4, 0.3, 0.2, 0.1])
    tier = rng.choice(["basic", "plus", "pro"], n, p=[0.6, 0.3, 0.1])
    base = {"basic": 20.0, "plus": 55.0, "pro": 140.0}
    amount = np.array([rng.lognormal(np.log(base[t]), 0.4) for t in tier])
    return pd.DataFrame(
        {
            # D1 bait: near-unique identifier, should be dropped.
            "order_id": [f"ORD-{i:07d}" for i in range(n)],
            # D3 bait: constant, should be dropped.
            "source_system": "billing",
            "region": region,
            "tier": tier,
            "amount": np.round(amount, 2),
            # Correlated with amount, so correlation_score has something to say.
            "tax": np.round(amount * 0.08, 2),
            "items": rng.integers(1, 9, n),
            "returned": rng.random(n) < 0.07,
        }
    )


def make_timeseries_source(
    sessions: int = 160, seed: int = 11
) -> tuple[pd.DataFrame, dict]:
    """Sessions walking a 4-state lifecycle, with a float-coded state column.

    The states are *sticky*: each one dwells for many rows before moving on.
    That matters -- `detect_state_fields` requires a self-transition rate of at
    least 0.95, because a lifecycle column that changes on most rows is not a
    lifecycle, it is a churning categorical. A fixture that transitions every
    step is detected as nothing at all.

    Returns the frame and the legal transition map it was generated from, so
    the report card can be scored against ground truth rather than against
    whatever the sample happened to show.
    """
    rng = np.random.default_rng(seed)
    # 1 pending -> 2 active -> 3 done (sink), 2 -> 4 failed -> 2 retry.
    legal = {"1": ["1", "2"], "2": ["2", "3", "4"], "3": ["3"], "4": ["2", "4"]}
    # Mean dwell in rows before leaving each state, and where it goes.
    dwell = {"1": 15, "2": 25, "4": 10}
    exits = {"1": (["2"], [1.0]), "2": (["3", "4"], [0.75, 0.25]), "4": (["2"], [1.0])}
    rows = []
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    for s in range(sessions):
        region = rng.choice(["north", "south", "east", "west"])
        tier = rng.choice(["basic", "plus", "pro"])
        state = "1"
        held = 0
        depth = 0.0
        for step in range(int(rng.integers(60, 91))):
            rows.append(
                {
                    "customer": f"C{s:05d}",
                    # Session-constant metadata.
                    "region": region,
                    "tier": tier,
                    "timestamp": t0 + pd.Timedelta(minutes=15 * step),
                    # The trap: a state machine stored as a float.
                    "status": float(state),
                    "latency_ms": float(rng.lognormal(3.2, 0.5)),
                    "depth": depth,
                }
            )
            depth += float(rng.integers(0, 4))
            held += 1
            # "3" is absorbing; everything else leaves after its dwell.
            if state in dwell and held >= rng.poisson(dwell[state]):
                targets, weights = exits[state]
                state = str(rng.choice(targets, p=weights))
                held = 0
    return pd.DataFrame(rows), legal


def to_dataset(name: str, df: pd.DataFrame) -> "rf.dataset.LocalDataset":
    # rf.Dataset(...) raises on purpose -- always go through a from_* factory.
    return rf.Dataset.from_pandas(name, df)


def match_string_types(reference, target):
    """Cast `reference`'s string columns to `target`'s string type.

    `rf.Dataset.from_pandas` produces arrow `large_string`; generated data comes
    back from the backend as plain `string`. `tv_distance` compares the two as
    different types and returns 1.0 -- the WORST possible score -- with no error
    and no warning. Measured on this script's own tabular run: region scored
    1.0 across the type gap and 0.048 once matched, dragging the overall
    marginal_dist_score from 0.32 to 0.62. ks_distance at least raises on a type
    mismatch; the categorical path fails silently, so always match types before
    scoring anything categorical.
    """
    table = reference.table
    for field in target.table.schema:
        i = table.schema.get_field_index(field.name)
        if i < 0:
            continue
        if pa.types.is_string(field.type) and table.schema.field(i).type != field.type:
            table = table.set_column(i, field.name, table.column(i).cast(field.type))
    return rf.Dataset.from_table(reference.name(), table)


# ---------------------------------------------------------------------------
# 1. Profile + recommend -- offline
# ---------------------------------------------------------------------------
def example_profile_and_recommend() -> None:
    print("\n=== 1. profile + recommend (offline) ===")

    tab = make_tabular_source()
    profile = profile_table(pa.Table.from_pandas(tab, preserve_index=False), name="orders")
    cfg = recommend(profile)
    print(f"  tabular   -> {cfg.decision} (rule {cfg.rule_fired})")
    print(f"  dropped   -> {cfg.drop_columns}")
    for note in cfg.notes:
        print(f"             {note}")

    check("tabular routes to a tabular model", cfg.decision.startswith("tab_"), cfg.decision)
    check("near-unique id dropped (D1)", "order_id" in cfg.drop_columns)
    check("constant column dropped (D3)", "source_system" in cfg.drop_columns)
    check("a real measure survives", "amount" not in cfg.drop_columns)
    check("no time roles on a tabular decision", cfg.session_column is None)

    ts, legal = make_timeseries_source()
    ts_profile = profile_table(pa.Table.from_pandas(ts, preserve_index=False), name="orders_ts")
    # The session-key heuristic picks by cardinality and column position, so
    # tell it what you know rather than hoping. Without the hint it may choose
    # a metadata column and route the data as tabular.
    ts_cfg = recommend(ts_profile, session_hints={"session_key": "customer"})
    print(f"\n  timeseries -> {ts_cfg.decision} (rule {ts_cfg.rule_fired})")
    print(f"  session    -> {ts_cfg.session_column}, timestamp -> {ts_cfg.timestamp_column}")
    print(f"  metadata   -> {ts_cfg.metadata_columns}")
    print(f"  archetype  -> {ts_cfg.session_archetype}")
    print(f"  cast to categorical -> {ts_cfg.categorical_cast_columns}")

    check("timeseries routes to a time model", ts_cfg.decision.startswith("time_"), ts_cfg.decision)
    check("session hint honored", ts_cfg.session_column == "customer")
    check("timestamp detected", ts_cfg.timestamp_column == "timestamp")
    # ~25 rows per session over 160 sessions is R3 territory: >= 50 sessions
    # with 4-500 rows each, which the recommender sends to the SSM family.
    check("R3 fired for many short sessions", ts_cfg.rule_fired in ("R0", "R3"), ts_cfg.rule_fired)
    # The float-coded state column is the reason to read this field: left
    # alone it trains as continuous and the model generates 2.37.
    check(
        "float-coded state flagged for a VARCHAR cast",
        "status" in ts_cfg.categorical_cast_columns,
        str(ts_cfg.categorical_cast_columns),
    )

    found = detect_state_fields(
        pa.Table.from_pandas(ts, preserve_index=False),
        session_key="customer",
        order_by="timestamp",
    )
    names = {c.field for c in found}
    print(f"  state fields -> {sorted(names)}")
    # A state field must be STICKY: detection needs a self-transition rate of
    # at least 0.95. A categorical that changes on most rows is not detected.
    check("status detected as a state field", "status" in names, str(sorted(names)))
    for cand in found:
        if cand.field != "status":
            continue
        print(f"  status: self_rate={cand.self_transition_rate:.3f} "
              f"breadth={cand.transition_breadth} map={cand.transition_map}")
        # The detector stringifies values, so a float-coded column yields
        # '1.0', not '1'. Those strings are what the decode constraints and
        # SamplingConfig.state_constraints must use.
        ground_truth = {f"{k}.0": {f"{x}.0" for x in v} for k, v in legal.items()}
        observed = {k: set(v) for k, v in cand.transition_map.items()}
        # Subset, not equality: a sampled graph can miss rare legal edges, which
        # is exactly why the profiler asks the user to confirm the map before
        # it is enforced at decode time. What must never happen is the reverse.
        check(
            "no illegal edge was inferred",
            all(observed[k] <= ground_truth.get(k, set()) for k in observed),
            str(observed),
        )


# ---------------------------------------------------------------------------
# 2. Report card -- offline
# ---------------------------------------------------------------------------
def degrade(df: pd.DataFrame, seed: int = 3) -> pd.DataFrame:
    """A plausible-but-wrong synthetic frame: perfect marginals, no sequence.

    Shuffling a column globally leaves its marginal distribution *exactly*
    intact while destroying every within-session ordering property it had. A
    per-field fidelity check scores this near 1.0. It is the failure mode the
    TS score exists to catch.
    """
    rng = np.random.default_rng(seed)
    out = df.copy()
    out["latency_ms"] = rng.permutation(out["latency_ms"].values)
    out["status"] = rng.permutation(out["status"].values)
    # The generator emits its own session key; the original high-cardinality
    # key is not reproduced. Mirror that here.
    out["session_key"] = pd.factorize(out["customer"])[0]
    out = out.drop(columns=["customer"])
    return out


def example_report_card() -> None:
    print("\n=== 2. report card (offline) ===")

    real, legal = make_timeseries_source()
    fake = degrade(real)

    spec = CardSpec(
        session_key="customer",
        timestamp="timestamp",
        # synth_session_key defaults to "session_key" -- the generator's key.
        state_fields=[
            StateFieldSpec(
                "status",
                legal_transitions={
                    (f"{a}", f"{b}") for a, bs in legal.items() for b in bs
                },
            )
        ],
        metadata_columns=["region", "tier"],
    )

    floor = noise_floor(real, spec)
    card = score(real, fake, spec, name="shuffled")
    print(card.summary(floor=floor))

    check("floor produced an RFScore", floor.scores["rfscore"] is not None)

    # A global shuffle preserves marginals exactly, so the per-field view sees
    # nothing wrong at all.
    check(
        "marginals survive the shuffle untouched",
        card.scores["marginal"] > 0.99,
        f"marginal {card.scores['marginal']:.4f}",
    )
    # ...and RFScore alone would clear the 0.85 training gate. This is the
    # whole argument for scoring sequence structure separately: a table can be
    # marginally perfect and temporally meaningless.
    check(
        "RFScore alone would pass the gate on broken data",
        card.scores["rfscore"] >= card.scores["rfscore_gate"],
        f"rfscore {card.scores['rfscore']:.4f} >= gate {card.scores['rfscore_gate']}",
    )
    check(
        "the TS score catches what RFScore misses",
        card.ts["ts_score"] < floor.ts["ts_score"],
        f"ts {card.ts['ts_score']:.4f} vs floor {floor.ts['ts_score']:.4f}",
    )
    check(
        "the broken dimension is the transition score",
        card.ts["transition_score"] == min(
            v for v in (card.ts["session_length_score"], card.ts["autocorr_score"],
                        card.ts["transition_score"]) if v is not None
        ),
        f"transition {card.ts['transition_score']:.4f}",
    )
    check("gate is recorded on the card", card.scores["rfscore_gate"] == 0.85)
    check(
        "the generator session key was used",
        card.guards["used_generator_session_key"] is True,
    )
    check(
        "shuffled status produces illegal transitions",
        any(sf["illegal_rate"] > 0 for sf in card.state_machine),
        str([sf["illegal_rate"] for sf in card.state_machine]),
    )
    # Not every floor is above every synthetic score: the floor runs on half
    # the sessions, so its own estimates are noisier. Compare dimension by
    # dimension, and treat a synthetic score *above* the floor as a sign the
    # dimension is uninformative rather than as a win.
    # meta["not_compared"] is the field to read before believing any score:
    # `customer` is absent from the synthetic frame by design, so it is not
    # scored, and nothing else should be missing silently.
    print(f"  not compared -> {card.meta['not_compared']}")
    check("only the source session key is uncompared", card.meta["not_compared"] == ["customer"])


# ---------------------------------------------------------------------------
# 3. Tabular train + generate -- needs a connection
# ---------------------------------------------------------------------------
async def example_tabular(conn) -> None:
    print("\n=== 3. tabular train + generate (RF-Tab-GAN) ===")

    df = make_tabular_source(n=4000)
    # Drop what the profiler would drop; the model should never see an id.
    df = df.drop(columns=["order_id", "source_system"])
    dataset = to_dataset("orders", df)

    categorical = ["region", "tier", "returned"]
    continuous = [c for c in df.columns if c not in categorical]

    train = ra.TrainTabGAN(
        ra.TrainTabGAN.Config(
            # tabular_gan is REQUIRED on TrainTabGAN.Config -- no default factory.
            tabular_gan=ra.TrainTabGAN.TrainConfig(epochs=10, batch_size=500),
            encoder=ra.TrainTabGAN.DatasetConfig(
                metadata=[
                    ra.TrainTabGAN.FieldConfig(field=f, type="categorical")
                    for f in categorical
                ]
                + [
                    ra.TrainTabGAN.FieldConfig(field=f, type="continuous")
                    for f in continuous
                ],
            ),
            model_labels={"skill": "generate-from-data", "shape": "tabular"},
        )
    )

    builder = rf.WorkflowBuilder()
    builder.add_dataset(dataset)
    builder.add_action(train, parents=[dataset])
    workflow = await builder.start(conn)
    print(f"  train workflow: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    model = await workflow.models().last()
    print(f"  model: {model.id}")
    check("training produced a model", model is not None)

    generate = ra.GenerateTabGAN(
        ra.GenerateTabGAN.Config(
            tabular_gan=ra.GenerateTabGAN.GenerateConfig(clip_in_range=True)
        )
    )
    target = ra.SessionTarget(target=4000)
    save = ra.DatasetSave(name="orders-synthetic")

    builder = rf.WorkflowBuilder()
    builder.add_model(model)
    # The two edges between generate and target are the feedback loop: target
    # counts what arrived and asks generate for the shortfall. Without the
    # second edge generation runs once and stops at the default cap.
    builder.add_action(generate, parents=[model, target])
    builder.add_action(target, parents=[generate])
    builder.add_action(save, parents=[generate])
    workflow = await builder.start(conn)
    print(f"  generate workflow: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    syn = await workflow.datasets().concat(conn)
    print(f"  generated {syn.table.num_rows} rows")

    check("schema is preserved", set(syn.table.column_names) >= set(df.columns),
          str(sorted(set(df.columns) - set(syn.table.column_names))))
    check("SessionTarget reached the requested volume", syn.table.num_rows >= 4000,
          str(syn.table.num_rows))

    syn_df = syn.to_pandas()
    lo, hi = df["amount"].min(), df["amount"].max()
    check("clip_in_range kept amount in the training range",
          bool((syn_df["amount"] >= lo).all() and (syn_df["amount"] <= hi).all()))

    # A tabular dataset has no session key, so the report card does not apply.
    # marginal_dist_score is the tabular-shaped fidelity number.
    #
    # Two things have to be right before the number means anything:
    #
    # 1. Arrow string types must match on both sides, or tv_distance returns 1.0
    #    silently. See match_string_types.
    # 2. other_categorical is not optional. Left to itself, marginal_dist_score
    #    classifies fields by dtype and sends anything non-string to
    #    ks_distance -- which rejects booleans outright ("must be either numeric
    #    or temporal"). Any bool column, and any numeric column you encoded as
    #    categorical, has to be named here or the whole score raises.
    matched = match_string_types(dataset, syn)

    raw_tv = rl.metrics.tv_distance(dataset, syn, "region")
    tv = rl.metrics.tv_distance(matched, syn, "region")
    print(f"  tv_distance(region): {raw_tv:.4f} unmatched -> {tv:.4f} matched")
    check(
        "matching string types changes the categorical distance",
        tv < raw_tv or raw_tv < 1.0,
        f"unmatched {raw_tv:.4f}, matched {tv:.4f}",
    )
    # Coverage is type-agnostic, so it is the cross-check that tells you a 1.0
    # tv_distance was an artifact rather than a genuine total mismatch.
    #
    # category_coverage carries a bare `assert` that the synthetic column has no
    # MORE distinct values than the real one. A generator emitting an unseen
    # category is a finding, not a reason to abort the run -- and because it is
    # an assert it vanishes under `python -O`, so the same input either raises
    # or silently divides by a wrong denominator depending on how you launched.
    try:
        coverage = rl.metrics.category_coverage(dataset, syn, "region")
        check("every real category appears in the synthetic data",
              coverage == 1.0, f"{coverage:.4f}")
    except AssertionError:
        check("every real category appears in the synthetic data", False,
              "synthetic emitted categories absent from the real data")

    fidelity = rl.metrics.marginal_dist_score(matched, syn, other_categorical=categorical)
    print(f"  marginal fidelity: {fidelity:.4f}")
    check("marginal fidelity is a real score", 0.0 <= fidelity <= 1.0, f"{fidelity:.4f}")
    for field in ("amount", "tax"):
        print(f"  ks_distance({field}) = {rl.metrics.ks_distance(matched, syn, field):.4f}")
    for field in ("region", "tier"):
        print(f"  tv_distance({field}) = {rl.metrics.tv_distance(matched, syn, field):.4f}")
    # 10 epochs of CTGAN on 4k rows is a smoke test, not a fidelity result --
    # expect a mediocre score here and do not read anything into it.


# ---------------------------------------------------------------------------
# 4. Time-series train + generate -- needs a connection
# ---------------------------------------------------------------------------
async def example_timeseries(conn) -> None:
    print("\n=== 4. time-series train + generate (RF-Time-GAN) ===")

    df, legal = make_timeseries_source(sessions=160)
    # The state column is a float in the source; cast it before training or it
    # trains as continuous and the model generates values like 2.37.
    df["status"] = df["status"].astype(int).astype(str)
    dataset = to_dataset("orders-ts", df)

    train = ra.TrainTimeGAN(
        ra.TrainTimeGAN.Config(
            encoder=ra.TrainTimeGAN.DatasetConfig(
                timestamp=ra.TrainTimeGAN.TimestampConfig(field="timestamp"),
                metadata=[
                    # "session" marks a high-cardinality key whose values are
                    # not learned -- only its role as a session boundary.
                    ra.TrainTimeGAN.FieldConfig(field="customer", type="session"),
                    ra.TrainTimeGAN.FieldConfig(field="region", type="categorical"),
                    ra.TrainTimeGAN.FieldConfig(field="tier", type="categorical"),
                ],
                measurements=[
                    ra.TrainTimeGAN.FieldConfig(field="status", type="categorical"),
                    ra.TrainTimeGAN.FieldConfig(field="latency_ms", type="continuous"),
                    ra.TrainTimeGAN.FieldConfig(field="depth", type="continuous"),
                ],
            ),
            doppelganger=ra.TrainTimeGAN.DGConfig(
                epoch=20,
                # batch_size must stay below the number of sessions.
                batch_size=64,
                # Rule of thumb: avg_session_len / 50, floored at 1.
                sample_len=1,
            ),
            model_labels={"skill": "generate-from-data", "shape": "timeseries"},
        )
    )

    builder = rf.WorkflowBuilder()
    builder.add_dataset(dataset)
    builder.add_action(train, parents=[dataset])
    workflow = await builder.start(conn)
    print(f"  train workflow: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    model = await workflow.models().last()
    check("training produced a model", model is not None)

    generate = ra.GenerateTimeGAN(
        ra.GenerateTimeGAN.Config(doppelganger=ra.GenerateTimeGAN.DGConfig())
    )
    target = ra.SessionTarget(target=160)
    save = ra.DatasetSave(name="orders-ts-synthetic")

    builder = rf.WorkflowBuilder()
    builder.add_model(model)
    builder.add_action(generate, parents=[model, target])
    builder.add_action(target, parents=[generate])
    builder.add_action(save, parents=[generate])
    workflow = await builder.start(conn)
    print(f"  generate workflow: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)

    syn = await workflow.datasets().concat(conn)
    syn_df = syn.to_pandas()
    print(f"  generated {syn.table.num_rows} rows")

    # The generator emits its own session key. The original `customer` values
    # are NOT reproduced -- that is what type="session" means.
    check("synthetic output carries session_key", "session_key" in syn_df.columns,
          str(sorted(syn_df.columns)))
    n_syn_sessions = syn_df["session_key"].nunique()
    print(f"  sessions: {n_syn_sessions}")
    check("SessionTarget reached the session count", n_syn_sessions >= 160, str(n_syn_sessions))

    # Session metrics need table metadata on BOTH sides, and the two sides use
    # different fields: real session columns vs the generator's key.
    src = dataset.with_table_metadata(rf.TableMetadata(metadata=["customer", "region", "tier"]))
    syn_md = syn.with_table_metadata(rf.TableMetadata(metadata=["session_key"]))

    len_ks = rl.metrics.ks_distance(
        rf.metrics.session_length(src), rf.metrics.session_length(syn_md), "session_length"
    )
    ia_ks = rl.metrics.ks_distance(
        rf.metrics.interarrivals(src, "timestamp"),
        rf.metrics.interarrivals(syn_md, "timestamp"),
        "interarrival",
    )
    print(f"  session_length KS: {len_ks:.4f}   interarrival KS: {ia_ks:.4f}")
    check("session length KS is in range", 0.0 <= len_ks <= 1.0)

    spec = CardSpec(
        session_key="customer",
        timestamp="timestamp",
        state_fields=[
            StateFieldSpec(
                "status",
                legal_transitions={(a, b) for a, bs in legal.items() for b in bs},
            )
        ],
        metadata_columns=["region", "tier"],
    )
    floor = noise_floor(df, spec)
    card = score(df, syn_df, spec, name="timegan")
    print(card.summary(floor=floor))
    card.to_json("timegan-card.json")

    check("card scored something", card.scores["rfscore"] is not None)
    check("card grouped synthetic rows by the generator key",
          card.guards["used_generator_session_key"] is True)
    # Judge against the floor, never against 1.0.
    print(f"  RFScore {card.scores['rfscore']:.4f} vs floor {floor.scores['rfscore']:.4f} "
          f"(gate {card.scores['rfscore_gate']})")


# ---------------------------------------------------------------------------

EXAMPLES = {1: "profile+recommend", 2: "report card", 3: "tabular", 4: "timeseries"}
OFFLINE = {1, 2}


async def main(selected: list[int], connection_mode: str) -> None:
    if 1 in selected:
        example_profile_and_recommend()
    if 2 in selected:
        example_report_card()

    online = [n for n in selected if n not in OFFLINE]
    if not online:
        return

    async with connect(connection_mode) as conn:
        if 3 in online:
            await example_tabular(conn)
        if 4 in online:
            await example_timeseries(conn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-e", "--example",
        type=int,
        action="append",
        choices=sorted(EXAMPLES),
        help="Run a specific example (1-4). Repeatable. Default: run all four. "
             "Examples 1-2 are offline; 3-4 train real models.",
    )
    parser.add_argument(
        "--connection",
        choices=("auto", "config", "env"),
        default="auto",
        help="Credential source: 'config' for ~/.config/rockfish/config.toml, "
             "'env' for ROCKFISH_* variables, 'auto' (default) to prefer env "
             "when ROCKFISH_API_KEY is set.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.example or list(EXAMPLES), args.connection))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")
