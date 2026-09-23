"""End-to-end model-based generation with the Rockfish SDK, with real checks.

`models.md`, `pipeline.md` and `evaluation.md` in this directory cover the API
surface. This file is the coding example: what the calls look like in sequence,
which assertions actually hold, and where the shapes change between the source
data and the generated data.

Two examples. Each walks the full six-step loop the skill is built around --
analyze, set the target, prepare, train, generate, evaluate -- and each says out
loud WHY its model was chosen for its data, because that is the decision a
reader most needs to be able to reproduce on their own table.

  1. Tabular (orders)       A bounded ratio and a heavy-tailed counter, so the
                            preparation step carries the logit and log1p
                            transforms. Evaluated with marginal fidelity: a
                            table with no session key has no report card.

  2. Time-series (sessions)  An epoch-numeric timestamp, a float-coded state
                            machine, a gappy measurement and a monotone counter,
                            so preparation carries the cast, the directional
                            fill pair and the increment trick. Evaluated with
                            the full report card against a noise floor measured
                            before training.

Between them they exercise every preprocessing tip the skill documents.

Both train real models on a Rockfish backend and need credentials from
~/.config/rockfish/config.toml or the ROCKFISH_* environment variables. Epoch
counts are deliberately small so a run finishes in minutes: this is a smoke test
of the pipeline, not a fidelity result, and the scores it prints should not be
read as representative.

Exits non-zero if any check fails.

Run:
    python train-generate.py                   # both
    python train-generate.py -e 1              # tabular only
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
    from rockfish.labs.dataset_profiler import decode_constraints
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
EPS = 1e-6


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record a named assertion without aborting the rest of the run."""
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def step(n: int, title: str) -> None:
    print(f"\n  --- step {n}: {title} ---")


def connect(mode: str):
    """Open a Connection from the config file or the ROCKFISH_* env vars."""
    if mode == "env" or (mode == "auto" and os.environ.get("ROCKFISH_API_KEY")):
        return rf.Connection.from_env()
    return rf.Connection.from_config()


def to_dataset(name: str, df: pd.DataFrame):
    # rf.Dataset(...) raises on purpose -- always go through a from_* factory.
    return rf.Dataset.from_pandas(name, df)


async def run(conn, builder, label: str):
    """Start a workflow, wait for it, and return it."""
    workflow = await builder.start(conn)
    print(f"  {label} workflow: {workflow.id()}")
    await workflow.wait(raise_on_failure=True)
    return workflow


# ---------------------------------------------------------------------------
# Source data
#
# Fabricated locally so the script is self-contained, but shaped so every
# preprocessing rule the skill documents has something real to act on: an
# identifier to drop, a constant to drop, nulls to fill, a bounded ratio, a
# heavy-tailed counter, a float-coded state machine and an epoch timestamp.
# ---------------------------------------------------------------------------
def make_orders_source(n: int = 4000, seed: int = 7) -> pd.DataFrame:
    """Tabular: order lines with a bounded ratio and a heavy-tailed counter."""
    rng = np.random.default_rng(seed)
    tier = rng.choice(["basic", "plus", "pro"], n, p=[0.6, 0.3, 0.1])
    base = {"basic": 20.0, "plus": 55.0, "pro": 140.0}
    amount = np.array([rng.lognormal(np.log(base[t]), 0.4) for t in tier])

    # BOUNDED: quota_used can never exceed quota_total, and both endpoints carry
    # real mass -- an untouched account reads 0, an exhausted one reads exactly
    # the quota. Those are the rows logit cannot represent without help.
    quota_total = rng.choice([100.0, 250.0, 500.0], n)
    frac = rng.beta(5, 2, n)
    frac[:120] = 1.0
    frac[120:240] = 0.0

    # HEAVY-TAILED: seven orders of magnitude, 60% exact zeros -- and the zeros
    # are signal (no bytes moved), not missing data.
    bytes_sent = np.where(rng.random(n) < 0.6, 0.0, rng.lognormal(11, 3.2, n).round())

    # NULLS: a numeric column with gaps, to exercise a scalar fill.
    discount = np.where(rng.random(n) < 0.15, np.nan, rng.uniform(0, 30, n).round(2))

    return pd.DataFrame(
        {
            "order_id": [f"ORD-{i:07d}" for i in range(n)],  # D1 bait: near-unique
            "source_system": "billing",                      # D3 bait: constant
            "region": rng.choice(["north", "south", "east", "west"], n,
                                 p=[0.4, 0.3, 0.2, 0.1]),
            "tier": tier,
            "amount": np.round(amount, 2),
            "tax": np.round(amount * 0.08, 2),               # correlated with amount
            "items": rng.integers(1, 9, n),
            "discount": discount,
            "quota_total": quota_total,
            "quota_used": (quota_total * frac).round(3),
            "bytes_sent": bytes_sent,
        }
    )


def make_sessions_source(sessions: int = 160, seed: int = 11):
    """Time-series: job sessions walking a sticky 4-state lifecycle.

    The states dwell for many rows before moving on, which matters:
    `detect_state_fields` requires a self-transition rate of at least 0.95,
    because a column that changes on most rows is a churning attribute, not a
    lifecycle, and is not detected however state-like it looks.

    Returns the frame and the transition map it was generated from, so the
    report card can be scored against ground truth rather than against whatever
    the sample happened to show.
    """
    rng = np.random.default_rng(seed)
    legal = {"1": ["1", "2"], "2": ["2", "3", "4"], "3": ["3"], "4": ["2", "4"]}
    dwell = {"1": 15, "2": 25, "4": 10}
    exits = {"1": (["2"], [1.0]), "2": (["3", "4"], [0.75, 0.25]), "4": (["2"], [1.0])}

    rows = []
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    for s in range(sessions):
        region = rng.choice(["north", "south", "east", "west"])
        tier = rng.choice(["basic", "plus", "pro"])
        state, held, written = "1", 0, 0.0
        for i in range(int(rng.integers(60, 91))):
            ts = t0 + pd.Timedelta(minutes=15 * i)
            rows.append(
                {
                    "job": f"J{s:05d}",
                    "region": region,
                    "tier": tier,
                    # EPOCH-NUMERIC timestamp: an int64, not a timestamp dtype.
                    "event_time": int(ts.timestamp()),
                    # FLOAT-CODED STATE MACHINE: trains as continuous and
                    # generates 2.37 unless cast to a string first.
                    "status": float(state),
                    "latency_ms": float(rng.lognormal(3.2, 0.5)),
                    # GAPPY: a sensor that drops out, needing a directional fill.
                    "queue_depth": (np.nan if rng.random() < 0.12
                                    else float(rng.integers(0, 40))),
                    # MONOTONE COUNTER: never decreases within a session.
                    "bytes_written": written,
                }
            )
            written += float(rng.integers(0, 5_000_000))
            held += 1
            if state in dwell and held >= rng.poisson(dwell[state]):
                targets, weights = exits[state]
                state = str(rng.choice(targets, p=weights))
                held = 0
    return pd.DataFrame(rows), legal


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def match_string_types(reference, target):
    """Cast `reference`'s string columns to `target`'s string type.

    `rf.Dataset.from_pandas` produces arrow `large_string`; generated data comes
    back from the backend as plain `string`. `tv_distance` compares the two as
    different types and returns 1.0 -- the WORST possible score -- with no error
    and no warning. Measured on this script's own tabular run: one categorical
    scored 1.0 across the type gap and 0.16 once matched, dragging the overall
    marginal_dist_score from 0.32 to 0.62. `ks_distance` at least raises on a
    type mismatch; the categorical path fails silently, so always match types
    before scoring anything categorical.
    """
    table = reference.table
    for field in target.table.schema:
        i = table.schema.get_field_index(field.name)
        if i < 0:
            continue
        if pa.types.is_string(field.type) and table.schema.field(i).type != field.type:
            table = table.set_column(i, field.name, table.column(i).cast(field.type))
    return rf.Dataset.from_table(reference.name(), table)


def explain_routing(profile, config) -> None:
    """Say why the recommender picked this model, and what would change it.

    Model choice is a pure function of measured properties, not a matter of
    taste. Printing the measurement next to the rule makes the mapping
    checkable against your own table.
    """
    numeric = sum(
        1 for c in profile.columns
        if c.kind == "numeric" and (c.distinct_count or 0) > 10
        and c.name not in (config.session_column, config.timestamp_column)
        and c.name not in config.drop_columns
    )
    line = f"  measured  : {profile.row_count} rows, {numeric} continuous measurement(s)"
    if config.session_column:
        stats = profile.session_lengths.get(config.session_column)
        if stats:
            line += (f", {stats.sessions} sessions, avg length {stats.mean:.0f}"
                     f", archetype {stats.archetype}")
    print(line)
    print(f"  decision  : {config.decision}   (rule {config.rule_fired})")

    why = {
        "R0": "3+ continuous measurements -- a GAN models floats natively, where a "
              "token model turns every distinct value into new vocabulary",
        "R1": "very long sessions (avg > 500 rows) -- avoids the token budget a "
              "sequence model would need",
        "R2": "few sessions (< 50) -- not enough sequences for a token model to learn from",
        "R3": "many sessions of moderate length -- the shape a state-space model "
              "handles best, and the current premium default for session data",
        "R4": "small or almost entirely numeric -- the GAN trains fastest and a token "
              "model has little categorical structure to exploit",
        "R5": "enough rows with real categorical structure -- the premium tabular default",
    }.get(config.rule_fired)
    if why:
        print(f"  because   : {why}")

    flip = {
        "R0": "fewer than 3 continuous measurements makes this a session-shape decision",
        "R1": "an average session length under 500 routes to time_ssm instead",
        "R2": "50+ sessions routes to time_ssm instead",
        "R3": "under 50 sessions, or an average length over 500, routes to time_gan instead",
        "R4": "1000+ rows with categorical structure routes to tab_ssm instead",
        "R5": "under 1000 rows, or no categorical columns, routes to tab_gan instead",
    }.get(config.rule_fired)
    if flip:
        print(f"  would flip: {flip}")


def train_action_for(decision: str, encoder, *, labels=None, relational=None):
    """Map a recommender decision onto a configured train action.

    Written out rather than calling dataset_profiler.train_action() so the
    reader can see which config shape belongs to which family: the GANs nest
    under `doppelganger` / `tabular_gan`, while SSM and rtf2 share one flat
    encoder / model / train / quality_check schema.
    """
    labels = labels or {}
    if decision == "tab_gan":
        return ra.TrainTabGAN(ra.TrainTabGAN.Config(
            tabular_gan=ra.TrainTabGAN.TrainConfig(epochs=10, batch_size=500),
            encoder=encoder, model_labels=labels))
    if decision == "tab_ssm":
        return ra.TrainTabSSM(ra.TrainTabSSM.Config(
            encoder=encoder,
            train=ra.TrainTabSSM.TrainConfig(epochs=5, batch_size=16),
            model_labels=labels))
    if decision in ("tab_rtf2", "tab_transformer"):
        return ra.TrainTabTransformerV2(ra.TrainTabTransformerV2.Config(
            encoder=encoder,
            train=ra.TrainTabTransformerV2.TrainConfig(epochs=5, batch_size=16),
            model_labels=labels))
    if decision == "time_gan":
        return ra.TrainTimeGAN(ra.TrainTimeGAN.Config(
            encoder=encoder,
            doppelganger=ra.TrainTimeGAN.DGConfig(epoch=20, batch_size=64, sample_len=1),
            model_labels=labels))
    if decision == "time_ssm":
        return ra.TrainTimeSSM(ra.TrainTimeSSM.Config(
            encoder=encoder,
            train=ra.TrainTimeSSM.TrainConfig(epochs=3),
            relational=relational or ra.TrainTimeSSM.RelationalConfig(),
            model_labels=labels))
    if decision in ("time_rtf2", "time_transformer"):
        return ra.TrainTimeTransformerV2(ra.TrainTimeTransformerV2.Config(
            encoder=encoder,
            train=ra.TrainTimeTransformerV2.TrainConfig(epochs=3),
            relational=relational or ra.TrainTimeTransformerV2.RelationalConfig(),
            model_labels=labels))
    raise ValueError(f"no train action wired for decision {decision!r}")


def generate_action_for(decision: str):
    """Generate action matching the trained family."""
    if decision == "tab_gan":
        return ra.GenerateTabGAN(ra.GenerateTabGAN.Config(
            tabular_gan=ra.GenerateTabGAN.GenerateConfig(clip_in_range=True)))
    if decision == "tab_ssm":
        # GenerateTimeSSM lowers gen_batch to 32 by default; GenerateTabSSM does
        # NOT -- it keeps the base 256. On a CPU worker (no CUDA kernels) the
        # naive Mamba-2 path materialises a huge per-step intermediate at that
        # batch size, which is why the time variant lowers it. One run on a
        # shared cpu worker took 2h38m for 4000 records -- n=1, contention
        # uncontrolled, but reason enough to set the knob.
        return ra.GenerateTabSSM(ra.GenerateTabSSM.Config(
            sampling=ra.GenerateTabSSM.SamplingConfig(gen_batch=32)))
    if decision in ("tab_rtf2", "tab_transformer"):
        return ra.GenerateTabTransformerV2(ra.GenerateTabTransformerV2.Config())
    if decision == "time_gan":
        return ra.GenerateTimeGAN(ra.GenerateTimeGAN.Config(
            doppelganger=ra.GenerateTimeGAN.DGConfig()))
    if decision == "time_ssm":
        return ra.GenerateTimeSSM(ra.GenerateTimeSSM.Config())
    if decision in ("time_rtf2", "time_transformer"):
        return ra.GenerateTimeTransformerV2(ra.GenerateTimeTransformerV2.Config())
    raise ValueError(f"no generate action wired for decision {decision!r}")


def field_configs(action_cls, names, kind):
    return [action_cls.FieldConfig(field=n, type=kind) for n in names]


# ---------------------------------------------------------------------------
# Example 1 -- tabular: bounded ratio + heavy-tailed counter
# ---------------------------------------------------------------------------
# Forward: ratio -> logit, plus a bucket column recording which rows sat exactly
# on an endpoint. The clamp keeps logit finite; the bucket is what lets the
# inverse put the endpoints back, because sigmoid never reaches 0 or 1.
ORDERS_ENCODE_SQL = f"""
    SELECT region, tier, amount, tax, items, "quota_total", bytes_sent,
           COALESCE(discount, 0.0) AS discount,
           ln(ratio / (1 - ratio))  AS quota_logit,
           bucket                   AS quota_bucket
    FROM (
      SELECT *,
             least(greatest("quota_used" / "quota_total", {EPS}), 1 - {EPS}) AS ratio,
             CASE WHEN "quota_used" <= 0               THEN 'zero'
                  WHEN "quota_used" >= "quota_total"   THEN 'one'
                  ELSE 'interior' END                  AS bucket
      FROM my_table
    )
"""

# Inverse: quota_total * sigmoid(z) for the interior, the recorded endpoint
# otherwise. The bound holds for any value the model could possibly emit.
ORDERS_DECODE_SQL = """
    SELECT region, tier, amount, tax, items, discount, "quota_total", bytes_sent,
           CASE WHEN quota_bucket = 'zero' THEN 0.0
                WHEN quota_bucket = 'one'  THEN "quota_total"
                ELSE "quota_total" / (1 + exp(-quota_logit)) END AS "quota_used"
    FROM my_table
"""


def _collapse(series: pd.Series, threshold: float = 0.01) -> float:
    """Share of min-max normalised values under `threshold`, ignoring exact zeros.

    The zeros are excluded on purpose. bytes_sent is ~60% exact zeros, and a
    zero is *supposed* to normalise to 0.0 -- counting the point mass as
    "collapsed" swamps the measurement and makes log1p look like it did
    nothing (97.6% -> 59.7%, which is just the zero fraction reasserting
    itself). What the transform is there to fix is the resolution of the
    values that actually span a range: among those, the same column goes
    94.1% -> 0.1%.
    """
    values = series[series > 0]
    if len(values) == 0:
        return 1.0
    lo, hi = float(values.min()), float(values.max())
    if hi == lo:
        return 1.0
    return float((((values - lo) / (hi - lo)) < threshold).mean())


async def example_tabular(conn) -> None:
    print("\n=== 1. tabular: orders ===")
    raw = make_orders_source()

    # ---- step 1: analyze -------------------------------------------------
    step(1, "analyze")
    profile = profile_table(pa.Table.from_pandas(raw, preserve_index=False), name="orders")
    config = recommend(profile)
    explain_routing(profile, config)
    print(f"  dropped   : {config.drop_columns}")
    for note in config.notes:
        print(f"              {note}")
    check("near-unique id dropped (D1)", "order_id" in config.drop_columns)
    check("constant column dropped (D3)", "source_system" in config.drop_columns)
    check("recommender produced a trainable decision", config.decision != "refuse",
          config.refusal_reason or config.decision)

    # Bounded and heavy-tailed columns are NOT auto-detected -- this is the
    # judgement the profiler cannot make for you, and the reason to look at the
    # data before preparing it.
    collapse_before = _collapse(raw["bytes_sent"])
    print(f"  bytes_sent: {collapse_before:.2%} of its non-zero values fall below 0.01"
          f" once normalised -- a model would see almost all of them as one number")
    print("  quota_used is bounded by quota_total; both endpoints carry real mass")

    # ---- step 2: set the target -----------------------------------------
    step(2, "set the target")
    # A tabular frame has no session key, so there is no report card and no
    # noise floor. The target is stated in terms of what a tabular run can be
    # judged on: marginal fidelity, and the constraint holding exactly.
    categorical = ["region", "tier", "quota_bucket"]
    print("  target    : marginal fidelity on all fields, and ZERO quota_used >"
          " quota_total rows -- the bound is structural, not statistical")

    # ---- step 3: prepare -------------------------------------------------
    step(3, "prepare")
    # One SQL projection does the drops, the scalar fill and the logit
    # transform; LogEncode applies log1p to the heavy-tailed counter.
    prep = rf.WorkflowBuilder()
    prep.add_path(
        to_dataset("orders", raw),
        ra.SQL(query=ORDERS_ENCODE_SQL),
        ra.LogEncode(field="bytes_sent"),      # log1p, NOT log: keeps the zeros
        ra.DatasetSave(name="orders-prepared"),
    )
    wf = await run(conn, prep, "prepare")
    prepared = await wf.datasets().concat(conn)
    prep_df = prepared.to_pandas()

    check("logit is finite for every row", bool(np.isfinite(prep_df["quota_logit"]).all()),
          "the clamp is what prevents +/-inf at the endpoints")
    check("endpoint buckets were recorded",
          {"zero", "one"} <= set(prep_df["quota_bucket"].unique()),
          str(prep_df["quota_bucket"].value_counts().to_dict()))
    collapse_after = _collapse(prep_df["bytes_sent"])
    check("log1p restored resolution", collapse_after < collapse_before / 10,
          f"{collapse_before:.2%} -> {collapse_after:.2%} below 0.01")
    check("scalar fill removed the nulls", int(prep_df["discount"].isna().sum()) == 0)

    # ---- step 4: train ---------------------------------------------------
    step(4, "train")
    # Re-profile the PREPARED table: the encoder has to describe the columns the
    # trainer will actually see, which are not the columns we started with.
    prep_profile = profile_table(prepared.table, name="orders-prepared")
    prep_config = recommend(prep_profile)
    explain_routing(prep_profile, prep_config)

    continuous = [c for c in prep_df.columns if c not in categorical]
    action_cls = {"tab_gan": ra.TrainTabGAN, "tab_ssm": ra.TrainTabSSM}.get(
        prep_config.decision, ra.TrainTabTransformerV2)
    encoder = action_cls.DatasetConfig(
        metadata=(field_configs(action_cls, categorical, "categorical")
                  + field_configs(action_cls, continuous, "continuous")))
    train = train_action_for(prep_config.decision, encoder,
                             labels={"skill": "generate-from-data", "example": "tabular"})
    builder = rf.WorkflowBuilder()
    builder.add_dataset(prepared)
    builder.add_action(train, parents=[prepared])
    wf = await run(conn, builder, "train")
    model = await wf.models().last()
    check("training produced a model", model is not None)

    # ---- step 5: generate ------------------------------------------------
    step(5, "generate")
    generate = generate_action_for(prep_config.decision)
    target = ra.SessionTarget(target=len(raw))
    builder = rf.WorkflowBuilder()
    builder.add_model(model)
    # The two edges between generate and target are the feedback loop: target
    # counts what arrived and asks generate for the shortfall.
    builder.add_action(generate, parents=[model, target])
    builder.add_action(target, parents=[generate])
    builder.add_action(ra.DatasetSave(name="orders-synthetic-encoded"), parents=[generate])
    wf = await run(conn, builder, "generate")
    syn_encoded = await wf.datasets().concat(conn)
    print(f"  generated {syn_encoded.table.num_rows} rows in transformed space")

    # Undo the transforms, in the reverse order they were applied.
    post = rf.WorkflowBuilder()
    post.add_path(
        syn_encoded,
        ra.LogDecode(field="bytes_sent"),       # expm1, casts back to the source dtype
        ra.SQL(query=ORDERS_DECODE_SQL),
        ra.DatasetSave(name="orders-synthetic"),
    )
    wf = await run(conn, post, "decode")
    syn = await wf.datasets().concat(conn)
    syn_df = syn.to_pandas()

    # ---- step 6: evaluate ------------------------------------------------
    step(6, "evaluate")
    violations = int(((syn_df["quota_used"] < 0)
                      | (syn_df["quota_used"] > syn_df["quota_total"])).sum())
    check("quota_used <= quota_total holds by construction", violations == 0,
          f"{violations} of {len(syn_df)} rows -- sigmoid cannot leave (0, 1)")
    check("bytes_sent came back on the original scale", syn_df["bytes_sent"].max() > 1e6,
          f"max {syn_df['bytes_sent'].max():.0f}")

    # Match arrow string types first, or tv_distance silently returns 1.0.
    real_for_scoring = match_string_types(
        to_dataset("orders", raw[[c for c in raw.columns if c in syn_df.columns]]), syn)
    shared_cat = [c for c in ("region", "tier") if c in syn_df.columns]
    fidelity = rl.metrics.marginal_dist_score(real_for_scoring, syn,
                                              other_categorical=shared_cat)
    print(f"  marginal fidelity: {fidelity:.4f}")
    check("marginal fidelity is a real score", 0.0 <= fidelity <= 1.0, f"{fidelity:.4f}")
    for f in shared_cat:
        print(f"  tv_distance({f}) = {rl.metrics.tv_distance(real_for_scoring, syn, f):.4f}")
    print("  (small epoch counts: treat these as a pipeline smoke test, not fidelity)")


# ---------------------------------------------------------------------------
# Example 2 -- time-series: epoch timestamp, state machine, counter, gaps
# ---------------------------------------------------------------------------
# Epoch seconds -> a real timestamp; the float state -> VARCHAR so it encodes as
# a state and not as a number; the counter -> its per-step increment, in log1p
# space, because the level is unbounded and monotone while the delta is neither.
SESSIONS_ENCODE_SQL = """
    SELECT job, region, tier, ts, status, latency_ms, bytes_delta,
           COALESCE(qd_fwd, qd_bwd) AS queue_depth
    FROM (
      SELECT job, region, tier,
             to_timestamp_seconds(CAST("event_time" AS BIGINT))  AS ts,
             CAST(CAST("status" AS INT) AS VARCHAR)              AS status,
             latency_ms,
             LAST_VALUE("queue_depth" IGNORE NULLS) OVER (
               PARTITION BY job ORDER BY "event_time"
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)  AS qd_fwd,
             FIRST_VALUE("queue_depth" IGNORE NULLS) OVER (
               PARTITION BY job ORDER BY "event_time"
               ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)  AS qd_bwd,
             ln(1 + GREATEST("bytes_written" - LAG("bytes_written", 1, 0.0) OVER (
               PARTITION BY job ORDER BY "event_time"), 0.0))      AS bytes_delta
      FROM my_table
    )
"""


# Inverse of the counter step: expm1 the per-step increment and accumulate it
# back into a level, within each generated session, in GENERATION order. `seq`
# is why sequence_index_column is worth setting -- generated timestamps are just
# another modelled field, neither monotone nor unique, so ordering by them would
# reconstruct the counter in the wrong order.
SESSIONS_DECODE_SQL = """
    SELECT *,
           SUM(exp(bytes_delta) - 1) OVER (
             PARTITION BY session_key ORDER BY seq
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS bytes_written
    FROM my_table
"""

async def example_timeseries(conn) -> None:
    print("\n=== 2. time-series: job sessions ===")
    raw, legal = make_sessions_source()

    # ---- step 1: analyze -------------------------------------------------
    step(1, "analyze")
    profile = profile_table(pa.Table.from_pandas(raw, preserve_index=False), name="jobs")
    # The session-key heuristic picks by cardinality and column position, so
    # tell it what you know rather than hoping.
    config = recommend(profile, session_hints={"session_key": "job"})
    explain_routing(profile, config)
    print(f"  timestamp : {config.timestamp_column}  (prep: {config.timestamp_prep})")
    print(f"  cast      : {config.categorical_cast_columns}")
    print(f"  dropped   : {config.drop_columns}")
    for note in config.notes:
        print(f"              {note}")
    # The recommender is a starting point, not an oracle. D1 drops columns that
    # are >95% distinct as identifiers -- but a float sensor reading is
    # near-unique BY NATURE, so latency_ms and the bytes_written counter get
    # caught by a rule meant for IDs. We keep them: the prepare step below
    # selects columns explicitly rather than applying config.drop_columns.
    # Worth knowing that this also moved the routing: those two columns are
    # what the continuous-measurement count is made of, so dropping them is
    # part of why R3 fired instead of R0.
    caught = [c for c in ("latency_ms", "bytes_written") if c in config.drop_columns]
    if caught:
        print(f"  KEEPING   : {caught} -- near-unique floats are measurements, not IDs")
    check("session hint honored", config.session_column == "job")
    check("float-coded state flagged for a VARCHAR cast",
          "status" in config.categorical_cast_columns,
          str(config.categorical_cast_columns))

    found = detect_state_fields(pa.Table.from_pandas(raw, preserve_index=False),
                                session_key="job", order_by="event_time")
    names = {c.field for c in found}
    check("status detected as a state field", "status" in names, str(sorted(names)))
    # NOTE the values here are '1.0'/'2.0' -- the raw float column stringified.
    # They are NOT what the encoder will see after step 3 casts to VARCHAR of
    # the int, so the real constraint map is derived there, not here.
    print(f"  transitions (pre-cast): {decode_constraints(found).get('status')}")

    # ---- step 2: set the target -----------------------------------------
    step(2, "set the target")
    prepared_real = _prepare_sessions_locally(raw)
    spec = CardSpec(
        session_key="job",
        timestamp="ts",
        state_fields=[StateFieldSpec(
            "status", legal_transitions={(a, b) for a, bs in legal.items() for b in bs})],
        metadata_columns=["region", "tier"],
        # The counter is the RECONSTRUCTED level, not the delta the model saw.
        # CardSpec.counters means per-session MONOTONE columns and
        # counter_monotonicity scores (diff >= 0); bytes_delta is log1p of an
        # increment, non-negative but not monotone, and declaring it here scores
        # 0.51 on real data whose true counter scores 1.0.
        counters=["bytes_written"],
    )
    floor = noise_floor(prepared_real, spec)
    print(floor.summary())
    print(f"  target    : beat nothing below the floor -- RFScore {floor.scores['rfscore']:.4f},"
          f" TS {floor.ts['ts_score']:.4f}. Gate {floor.scores['rfscore_gate']}.")
    check("noise floor produced a score", floor.scores["rfscore"] is not None)

    # ---- step 3: prepare -------------------------------------------------
    step(3, "prepare")
    # One SQL projection does all of it: epoch -> timestamp, float state ->
    # VARCHAR, counter -> log1p of its per-step increment, and the directional
    # fill PAIR for the gappy sensor.
    #
    # The fill is a window function PARTITIONed BY job on purpose.
    # ra.Transform(FillNullForward(...)) calls pyarrow's fill_null_forward over
    # the WHOLE column with no notion of sessions, so a session whose first row
    # is null inherits the previous session's last value. Measured on this
    # fixture: 27 of 160 sessions start null and 30 rows differ from a
    # per-session fill. dataset_profiler.preprocess_actions emits those same
    # session-blind Transforms, so this applies to the recommended path too.
    #
    # Forward alone cannot fill a leading null run, hence COALESCE with a
    # backward pass; a surviving null reaches the encoder as NaT, which is not
    # in the vocabulary.
    prep = rf.WorkflowBuilder()
    prep.add_path(
        to_dataset("jobs", raw),
        ra.SQL(query=SESSIONS_ENCODE_SQL),
        ra.DatasetSave(name="jobs-prepared"),
    )
    wf = await run(conn, prep, "prepare")
    prepared = await wf.datasets().concat(conn)
    prep_df = prepared.to_pandas()

    check("epoch seconds became a real timestamp",
          str(prepared.table.schema.field("ts").type).startswith("timestamp"),
          str(prepared.table.schema.field("ts").type))
    check("state column is now categorical (string)",
          pa.types.is_string(prepared.table.schema.field("status").type)
          or pa.types.is_large_string(prepared.table.schema.field("status").type))
    check("directional fill pair removed every gap",
          int(prep_df["queue_depth"].isna().sum()) == 0)
    check("counter became a non-negative increment",
          bool((prep_df["bytes_delta"] >= 0).all()),
          "the level is monotone; the delta is what the model should learn")

    # Decode constraints must use the POST-CAST values. Derived from the raw
    # float column they would read '1.0'/'2.0'; the encoder sees '1'/'2', and a
    # key that does not match is silently ignored rather than raising -- the
    # mask then covers nothing and the model transitions freely.
    prepared_states = detect_state_fields(prepared.table, session_key="job", order_by="ts")
    constraints = decode_constraints([c for c in prepared_states if c.field == "status"])
    print(f"  transitions (post-cast): {constraints.get('status')}")
    check("constraint keys match the values the encoder will see",
          set(constraints.get("status", {})) == set(prep_df["status"].astype(str).unique()),
          f"{sorted(constraints.get('status', {}))} vs "
          f"{sorted(prep_df['status'].astype(str).unique())}")

    # ---- step 4: train ---------------------------------------------------
    step(4, "train")
    prep_profile = profile_table(prepared.table, name="jobs-prepared")
    prep_config = recommend(prep_profile, session_hints={"session_key": "job"})
    explain_routing(prep_profile, prep_config)

    action_cls = {"time_gan": ra.TrainTimeGAN, "time_ssm": ra.TrainTimeSSM}.get(
        prep_config.decision, ra.TrainTimeTransformerV2)
    session_type = "session" if prep_config.decision == "time_gan" else "categorical"
    encoder = action_cls.DatasetConfig(
        timestamp=action_cls.TimestampConfig(field="ts"),
        metadata=[action_cls.FieldConfig(field="job", type=session_type),
                  action_cls.FieldConfig(field="region", type="categorical"),
                  action_cls.FieldConfig(field="tier", type="categorical")],
        measurements=[action_cls.FieldConfig(field="status", type="categorical"),
                      action_cls.FieldConfig(field="latency_ms", type="continuous"),
                      action_cls.FieldConfig(field="queue_depth", type="continuous"),
                      action_cls.FieldConfig(field="bytes_delta", type="continuous")],
    )
    train = train_action_for(prep_config.decision, encoder,
                             labels={"skill": "generate-from-data", "example": "timeseries"})
    builder = rf.WorkflowBuilder()
    builder.add_dataset(prepared)
    builder.add_action(train, parents=[prepared])
    wf = await run(conn, builder, "train")
    model = await wf.models().last()
    check("training produced a model", model is not None)

    # ---- step 5: generate ------------------------------------------------
    step(5, "generate")
    generate = generate_action_for(prep_config.decision)
    # The SSM time path is the only one that honours decode constraints and an
    # explicit generation-order column; set them only where they take effect.
    if prep_config.decision == "time_ssm":
        generate.config().sampling.state_constraints = constraints
        generate.config().sampling.sequence_index_column = "seq"
        print("  SSM path: state_constraints + sequence_index_column enabled")

    target = ra.SessionTarget(target=raw["job"].nunique())
    builder = rf.WorkflowBuilder()
    builder.add_model(model)
    builder.add_action(generate, parents=[model, target])
    builder.add_action(target, parents=[generate])
    builder.add_action(ra.DatasetSave(name="jobs-synthetic"), parents=[generate])
    wf = await run(conn, builder, "generate")
    syn_encoded = await wf.datasets().concat(conn)
    print(f"  generated {syn_encoded.table.num_rows} rows in transformed space")

    # Undo the counter transform. Without this the output carries bytes_delta --
    # a log1p increment -- where the source had a monotone level, so the
    # synthetic table has a different schema from the real one and the counter
    # cannot be scored at all.
    if "seq" in syn_encoded.table.column_names:
        post = rf.WorkflowBuilder()
        post.add_path(syn_encoded, ra.SQL(query=SESSIONS_DECODE_SQL),
                      ra.DatasetSave(name="jobs-synthetic"))
        wf = await run(conn, post, "decode")
        syn = await wf.datasets().concat(conn)
    else:
        # No generation-order column (non-SSM path): reconstructing the counter
        # would have to guess an order, and a wrong order is worse than no
        # counter, so leave it encoded and say so.
        print("  no `seq` column -- counter left as bytes_delta, not reconstructed")
        syn = syn_encoded
    syn_df = syn.to_pandas()

    # ---- step 6: evaluate ------------------------------------------------
    step(6, "evaluate")
    # The generator emits its own session key; the original `job` values are not
    # reproduced. Report card groups by it via CardSpec.synth_session_key.
    check("synthetic output carries session_key", "session_key" in syn_df.columns,
          str(sorted(syn_df.columns)))
    if "seq" in syn_df.columns:
        spec.sequence_index = "seq"

    card = score(prepared_real, syn_df, spec, name="run1")
    print(card.summary(floor=floor))
    card.to_json("timeseries-card.json")
    check("card scored something", card.scores["rfscore"] is not None)
    check("card grouped synthetic rows by the generator key",
          card.guards["used_generator_session_key"] is True)
    # With decode constraints correctly applied this holds BY CONSTRUCTION --
    # an illegal transition is unsampleable, whatever the training quality. A
    # high rate here means the constraint keys did not match, not that the
    # model trained badly.
    if prep_config.decision == "time_ssm" and card.state_machine:
        illegal = max(sf["illegal_rate"] for sf in card.state_machine)
        check("decode constraints made illegal transitions unsampleable", illegal < 0.02,
              f"illegal rate {illegal} -- if high, check the constraint keys match "
              f"the post-cast encoder values")
    print(f"  not compared: {card.meta['not_compared']}"
          "   <- read this before believing any score")
    print("  (small epoch counts: treat these as a pipeline smoke test, not fidelity)")


def _prepare_sessions_locally(raw: pd.DataFrame) -> pd.DataFrame:
    """The same preparation as step 3, in pandas, for the pre-training floor.

    The floor has to be measured on the shape the model will produce, and step 3
    has not run yet at that point in the loop.
    """
    df = raw.sort_values(["job", "event_time"], kind="stable").copy()
    df["ts"] = pd.to_datetime(df["event_time"], unit="s", utc=True)
    df["status"] = df["status"].astype(int).astype(str)
    df["queue_depth"] = (df.groupby("job")["queue_depth"].ffill()
                         .groupby(df["job"]).bfill())
    delta = df.groupby("job")["bytes_written"].diff().fillna(0.0).clip(lower=0.0)
    df["bytes_delta"] = np.log1p(delta)
    # bytes_written stays so the card has a real counter to compare the
    # reconstructed synthetic one against. The model never sees this column --
    # it trains on bytes_delta -- but the level is what the source actually had
    # and what the output is supposed to look like again after decoding.
    return df[["job", "region", "tier", "ts", "status",
               "latency_ms", "queue_depth", "bytes_delta", "bytes_written"]]


# ---------------------------------------------------------------------------

EXAMPLES = {1: "tabular", 2: "timeseries"}


async def main(selected: list[int], connection_mode: str) -> None:
    async with connect(connection_mode) as conn:
        if 1 in selected:
            await example_tabular(conn)
        if 2 in selected:
            await example_timeseries(conn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-e", "--example", type=int, action="append",
                        choices=sorted(EXAMPLES),
                        help="Run one example (1=tabular, 2=timeseries). Repeatable. "
                             "Default: both. Both train real models on a backend.")
    parser.add_argument("--connection", choices=("auto", "config", "env"), default="auto",
                        help="Credential source: 'config' for "
                             "~/.config/rockfish/config.toml, 'env' for ROCKFISH_* "
                             "variables, 'auto' (default) to prefer env when "
                             "ROCKFISH_API_KEY is set.")
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
