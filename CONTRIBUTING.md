# Contributing to rockfish-skills

Thanks for your interest in contributing.

## Setting up

This repo uses [uv](https://docs.astral.sh/uv/) to manage the environment. [Install it](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/Rockfish-Data/rockfish-skills.git
cd rockfish-skills
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

`uv pip install` reads the `--find-links` line in `requirements.txt`, so `rockfish[labs]` resolves from the Rockfish package index automatically. You'll need a Rockfish account and a config file at `~/.config/rockfish/config.toml` to run examples that talk to the backend.

## Adding a new skill

Skills live under `skills/<skill-name>/`. Use an existing directory in [`skills/`](skills/) as a model. For more information, see Claude's documentation, [Extend Claude with skills](https://code.claude.com/docs/en/skills).

A skill can bundle a reference implementation under `skills/<skill-name>/reference/` — worked SDK code the agent reads (not runs) when it generates code.

> **Tip:** A skill that bundles a worked reference implementation under `reference/` produces noticeably higher-quality generated code. Include one whenever the feature's API shape is non-obvious.

After adding or editing a skill, install and test it locally.

## Pull requests

- Open a PR against `main`.
- Maintainers can choose to merge their own PRs directly. External PRs require maintainer review and merge.
- Branch protections block force-pushes and branch deletion on `main`.

## License

By contributing, you agree your contributions will be licensed under the Apache License 2.0.
