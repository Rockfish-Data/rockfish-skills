# Rockfish Skills

A collection of [Claude Skills](https://claude.com/skills) for the [Rockfish](https://www.rockfish.ai/) SDK — a Python SDK for generating synthetic data. Install them into Claude Code (or another compatible agent) and it will reach for the right one when you ask it to generate synthetic data, inject time-series scenarios, or analyze Snowflake tables.

| Skill | What it does |
| --- | --- |
| [`generate-from-schema`](skills/generate-from-schema/) | Generate synthetic tabular / time-series data from a schema (columns, state machines, foreign keys, PII-like values). |
| [`inject-scenarios`](skills/inject-scenarios/) | Inject spikes, outages, ramps, and shifts into a baseline time series for ML robustness and anomaly-detection testing. |
| [`snowflake-analyst`](skills/snowflake-analyst/) | Explore and load Snowflake data like local CSVs — list, describe, sample, profile, query, and import — without dragging whole tables over the network. |

---

## Install the skills

Pick one method. **Option A (plugin)** is the simplest for Claude Code and upgrades natively; **Option B (manual)** installs the skill directories yourself, following the official skills docs.

Each skill is a self-contained directory under [`skills/`](skills/) — both methods install from that same source.

### Option A — Claude Code plugin (recommended)

Run these inside an interactive Claude Code session:

```
/plugin marketplace add Rockfish-Data/rockfish-skills
/plugin install rockfish-skills@rockfish-skills
```

The first command registers this repo as a plugin marketplace; the second installs the `rockfish-skills` plugin (which bundles all the skills). Start a new session — or the same one — and the skills are available.

For CI or scripted setup, the non-interactive form is:

```bash
claude plugin marketplace add Rockfish-Data/rockfish-skills
claude plugin install rockfish-skills@rockfish-skills --scope user -y
```

> Exact `claude plugin` flags can vary by Claude Code version — run `claude plugin --help` if a subcommand differs.

### Option B — Install manually

Skills are just directories, so you can install them yourself without the plugin. Clone this repo:

```bash
git clone https://github.com/Rockfish-Data/rockfish-skills.git
```

Then make each directory under [`skills/`](skills/) visible to your agent — as a **personal** skill (available everywhere) or a **project** skill (scoped to one repo, and the only kind cloud sessions can see). The exact install locations, how skills are discovered, and how to remove one vary by environment and version, so follow the official guide rather than paths hardcoded here:

- **[Claude Code — Extend Claude with skills](https://code.claude.com/docs/en/skills)** — install locations and management for the CLI.
- **[Agent Skills overview](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)** — how skills work across Claude Code, the Desktop app, and claude.ai (where personal skills are enabled per account).

If you symlink the directories from your clone, a later `git pull` upgrades the installed skills in place; if you copy them, re-copy to upgrade.

---

## Upgrading

| Installed via | Upgrade |
| --- | --- |
| Option A — plugin | `/plugin marketplace update rockfish-skills` (in a session), or `claude plugin marketplace update rockfish-skills` |
| Option B — manual | `git pull` in your clone — symlinked skills update in place; re-copy if you copied them |

---

## Run the examples (Rockfish SDK)

The [`examples/`](examples/) scripts are runnable, self-contained walkthroughs of the SDK features the skills describe. To run them you need the SDK installed and a Rockfish config. Using [uv](https://docs.astral.sh/uv/):

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

That pulls in `rockfish[labs]` from `https://packages.rockfish.ai`, plus what the example scripts need (e.g. `matplotlib`). To install just the SDK:

```bash
uv pip install -U 'rockfish[labs]' -f 'https://packages.rockfish.ai'
```

You'll also need a Rockfish config at `~/.config/rockfish/config.toml` (or the `ROCKFISH_*` env vars) so the examples can talk to the backend. Then:

```bash
python examples/entity-gen.py --help    # generate synthetic data from a schema
python examples/scenarios.py --help     # inject scenarios into a time-series dataset
```

Example scripts write artifacts (plots, datasets) to `output/` (gitignored).

---

## How skills work

A skill is a directory under [`skills/`](skills/) containing a `SKILL.md` file with:

- YAML frontmatter (`name`, `description`) — the `description` is the matching signal an agent uses to decide whether to load the skill.
- A markdown body explaining when to use the skill and how to invoke the underlying SDK feature.
- (Optional) companion reference files or scripts, loaded on demand — e.g. `snowflake-analyst` ships a `scripts/` helper.

When a compatible agent has these skills installed, it surfaces the most relevant one based on your request. The `SKILL.md` is loaded first; companion files are pulled in only when needed. The skills point at the matching [`examples/`](examples/) scripts as worked references.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). External pull requests are welcome.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## References

- [Rockfish SDK package index](https://packages.rockfish.ai) — `pip` find-links source for `rockfish[labs]`.
- [Claude Skills](https://claude.com/skills) — what skills are and how agents use them.
- [The Complete Guide to Building Skills for Claude](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf) — Anthropic's official PDF guide.
- [Lessons from building Claude Code: how we use skills](https://claude.com/blog/lessons-from-building-claude-code-how-we-use-skills) — Anthropic engineering blog on real-world skill design.
