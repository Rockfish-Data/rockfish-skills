# Rockfish Skills

A collection of [Claude Skills](https://claude.com/skills) for the [Rockfish](https://www.rockfish.ai/) SDK — a Python SDK for generating synthetic data. Install them into Claude Code (or another compatible agent) and it will reach for the right one when you ask it to generate synthetic data, inject time-series scenarios, or analyze Snowflake tables.

| Skill | What it does |
| --- | --- |
| [`generate-from-schema`](skills/generate-from-schema/) | Generate synthetic tabular / time-series data from a schema (columns, state machines, foreign keys, PII-like values). |
| [`inject-scenarios`](skills/inject-scenarios/) | Inject spikes, outages, ramps, and shifts into a baseline time series for ML robustness and anomaly-detection testing. |
| [`snowflake-analyst`](skills/snowflake-analyst/) | Explore and load Snowflake data like local CSVs — list, describe, sample, profile, query, and import — without dragging whole tables over the network. |

---

## Install the skills

Pick one method. **Option A (plugin)** is the simplest for Claude Code and upgrades natively; **Option B (symlink)** works for any agent that reads `~/.claude/skills` and upgrades with `git pull`.

Each skill is a self-contained directory under [`skills/`](skills/) — all three methods install from that same source.

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

### Option B — Symlink into your personal skills directory

Clone the repo, then symlink each skill into `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR/skills`). Symlinks mean a later `git pull` upgrades every installed skill in place — nothing to re-run.

```bash
git clone https://github.com/Rockfish-Data/rockfish-skills.git
cd rockfish-skills
mkdir -p ~/.claude/skills
for skill in skills/*/; do
  ln -s "$PWD/$skill" ~/.claude/skills/"$(basename "$skill")"
done
```

`ln -s` refuses to overwrite an existing `~/.claude/skills/<name>`, so it won't clobber a skill you already have — remove or rename the conflict first if you hit one. Restart your agent (or start a new session) to pick up the change.

To remove them, delete the symlinks (this only removes the links, never your clone):

```bash
rm ~/.claude/skills/generate-from-schema \
   ~/.claude/skills/inject-scenarios \
   ~/.claude/skills/snowflake-analyst
```

### Option C — Project skills (commit into a repo)

To make the skills available to everyone working in a specific repository — including cloud sessions (claude.ai / Cowork / routines), which can't see your local `~/.claude/skills` — copy them into that repo's `.claude/skills/` and commit them:

```bash
# from inside the target repo, with rockfish-skills cloned alongside it
mkdir -p .claude/skills
cp -R ../rockfish-skills/skills/* .claude/skills/
git add .claude/skills && git commit -m "Add Rockfish skills"
```

Copy (don't symlink) here so the skills travel with the repo. Note: the `generate-from-schema` and `inject-scenarios` skills link to `../../examples/...` as worked references; from a committed copy those resolve to `.claude/examples/`, so copy [`examples/`](examples/) there too if you want those references to open. To upgrade later, `git pull` in the clone and re-run the `cp -R` above.

### Claude Desktop and claude.ai

The same `SKILL.md` skills work in the Claude Desktop app and on claude.ai/code, but the install path differs by surface:

- **Project skills** committed to a repo's `.claude/skills/` (Option C) are picked up automatically when that repo is opened in the Desktop Code tab, the web Code sandbox, or a cloud session.
- **Personal skills** must be enabled for your claude.ai account (via **Customize / Skills** in the sidebar) to appear in Desktop and cloud sessions. A local `~/.claude/skills` symlink (Option B) only affects the Claude Code CLI on that machine.

---

## Upgrading

| Installed via | Upgrade command |
| --- | --- |
| Option A — plugin | `/plugin marketplace update rockfish-skills` (in a session), or `claude plugin marketplace update rockfish-skills` |
| Option B — symlink | `git pull` in your clone — symlinks point at the repo, so skills update in place |
| Option C — copied into a repo | `git pull` in the clone, then re-run the `cp -R` to refresh the copies |

---

## Run the examples (Rockfish SDK)

The [`examples/`](examples/) scripts are runnable, self-contained walkthroughs of the SDK features the skills describe. To run them you need the SDK installed and a Rockfish config.

```bash
pip install -r requirements.txt
```

That pulls in `rockfish[labs]` from `https://packages.rockfish.ai`, plus what the example scripts need (e.g. `matplotlib`). To install just the SDK:

```bash
pip install -U 'rockfish[labs]' -f 'https://packages.rockfish.ai'
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
