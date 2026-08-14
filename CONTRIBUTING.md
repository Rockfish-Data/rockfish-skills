# Contributing to rockfish-skills

Thanks for your interest in contributing.

## Setting up

```bash
git clone https://github.com/Rockfish-Data/rockfish-skills.git
cd rockfish-skills
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You'll need a Rockfish account and a config file at `~/.config/rockfish/config.toml` to run examples that talk to the backend.

## Adding a new example

Examples live under `examples/` and are runnable Python scripts (not notebooks). Conventions:

- One feature per file. Self-contained.
- Use `async with rf.Connection.from_config() as conn:` for SDK connections so the underlying HTTP session is closed cleanly.
- Wrap the entry point in `argparse` so `--help` works, even if the script accepts no flags.
- Write artifacts under `output/<script-name>/` (gitignored).
- Print artifact paths so users can find them.

## Adding a new skill

Skills live under `skills/<skill-name>/`. Use an existing directory in [`skills/`](skills/) as a model.

A `SKILL.md` needs:

- YAML frontmatter with `name` and `description`. The `description` is the matching signal an agent uses to decide whether the skill is relevant; phrase it as *when to trigger this skill*, not just a summary of what it does. Include keyword phrases a user is likely to say.
- A focused body: when to use, key concepts, gotchas, link to a worked example under `examples/`.

Keep `SKILL.md` short. Push detail into companion files or the linked example script.

A new `skills/<skill-name>/` directory is picked up automatically by every install method — the plugin (it bundles everything under `skills/`) and `scripts/install-skills.sh` (it installs every directory containing a `SKILL.md`). No manifest to edit. Bump the `version` in [`.claude-plugin/plugin.json`](.claude-plugin/plugin.json) and [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) when you ship a change so plugin users get the update prompt.

After adding or editing a skill, install it locally and start a fresh session to sanity-check that the agent surfaces it:

```bash
scripts/install-skills.sh
```

## Pull requests

- Open a PR against `main`.
- Maintainers can merge their own PRs directly. External PRs require maintainer review and merge.
- Branch protections block force-pushes and branch deletion on `main`.

## License

By contributing, you agree your contributions will be licensed under the Apache License 2.0.
