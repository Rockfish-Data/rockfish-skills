# Rockfish Skills

A collection of [Claude Skills](https://claude.com/skills) for the [Rockfish](https://www.rockfish.ai/) SDK - a Python SDK for generating synthetic data. Install them into Claude Code (or another compatible agent) and it will reach for the right one when you ask it to generate synthetic data, inject time-series scenarios, or analyze Snowflake tables.

| Skill | What it does |
| --- | --- |
| [`generate-from-schema`](skills/generate-from-schema/) | Generate synthetic tabular / time-series data from a schema (columns, state machines, foreign keys, PII-like values). |
| [`inject-scenarios`](skills/inject-scenarios/) | Inject spikes, outages, ramps, and shifts into a baseline time series for ML robustness and anomaly-detection testing. |
| [`snowflake-analyst`](skills/snowflake-analyst/) | Explore and load Snowflake data like local CSVs - list, describe, sample, profile, query, and import - without dragging whole tables over the network. |

---

## Installing

Install as a Claude Code plugin:

```
/plugin marketplace add Rockfish-Data/rockfish-skills
/plugin install rockfish-skills@rockfish-skills
```

See the References below for how plugins and skills work, and other install methods.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). External pull requests are welcome.

## License

Apache 2.0 - see [LICENSE](LICENSE).

## References

- [Rockfish SDK package index](https://packages.rockfish.ai) - find-links source for `rockfish[labs]`.
- [Claude Skills](https://claude.com/skills) - what skills are and how agents use them.
- [Claude Code - Discover and install prebuilt plugins through marketplaces](https://code.claude.com/docs/en/discover-plugins) - find and install plugins from marketplaces to extend Claude Code with new skills, agents, and capabilities.
- [Claude Code - Extend Claude with skills](https://code.claude.com/docs/en/skills) - install locations and management for the CLI.
- [Agent Skills overview](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) - how skills work across Claude Code, the Desktop app, and claude.ai (where personal skills are enabled per account).
- [The Complete Guide to Building Skills for Claude](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf) - Anthropic's official PDF guide.
- [Lessons from building Claude Code: how we use skills](https://claude.com/blog/lessons-from-building-claude-code-how-we-use-skills) - Anthropic engineering blog on real-world skill design.
