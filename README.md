# Summary

A collection of [Claude Skills](https://claude.com/skills) for the [Rockfish](https://www.rockfish.ai/) SDK — a Python SDK for generating synthetic data.

## Install

```bash
pip install -r requirements.txt
```

That pulls in `rockfish[labs]` from `https://packages.rockfish.ai`, plus any dependencies the example scripts need (e.g. `matplotlib`).

The SDK alone:

```bash
pip install -U 'rockfish[labs]' -f 'https://packages.rockfish.ai'
```

You'll also need a Rockfish config at `~/.config/rockfish/config.toml` (or the `ROCKFISH_*` env vars) so the examples can talk to the backend.

## Quickstart

```bash
python examples/entity-gen.py --help    # generate synthetic data from a schema
python examples/scenarios.py --help     # inject scenarios into a time-series dataset
```

## What's here

- **`examples/`** — runnable Python scripts demonstrating Rockfish SDK features end-to-end. Each is a self-contained walkthrough.
- **`skills/`** — [Claude Skills](https://claude.com/skills) for working with the Rockfish SDK. Each skill is a directory containing a `SKILL.md` that an agent (Claude Code, Claude Desktop, etc.) loads to understand when and how to use the underlying SDK feature. Skills point at the corresponding example scripts as worked references.
- **`output/`** — gitignored; example scripts write artifacts (plots, datasets) here.

## How skills work

A skill is a directory under `skills/` containing a `SKILL.md` file with:

- YAML frontmatter (`name`, `description`) — the `description` is the matching signal an agent uses to decide whether to load the skill.
- A markdown body explaining when to use the skill and how to invoke the underlying SDK feature.
- (Optional) Additional reference files, scripts, or templates loaded on demand.

When a compatible agent has these skills installed, it surfaces the most relevant one based on the user's request. The `SKILL.md` is loaded first; companion files are pulled in only when needed.

See the skills in [`skills/`](skills/) for working examples. CONTRIBUTING.md describes how to add a new one.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). External pull requests are welcome.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## References

- [Rockfish SDK package index](https://packages.rockfish.ai) — `pip` find-links source for `rockfish[labs]`.
- [The Complete Guide to Building Skills for Claude](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf) — Anthropic's official PDF guide.
- [Lessons from building Claude Code: how we use skills](https://claude.com/blog/lessons-from-building-claude-code-how-we-use-skills) — Anthropic engineering blog on real-world skill design.
