#!/usr/bin/env python3
"""Validate the YAML frontmatter of every skill in skills/.

A skill whose frontmatter does not parse is not a skill with a bug — it is a
skill the host never loads at all, and nothing else in the repo notices. Link
checks pass, example scripts pass, and the skill is silently absent.

That has happened once already: an unquoted `description:` containing ": "
("Covers the whole loop: profiling …") is a YAML parse error, because a plain
scalar may not contain a colon followed by a space. This check exists so it
cannot happen twice.

Checks, per skills/<dir>/SKILL.md:
  1. A frontmatter block delimited by --- ... --- is present at the top.
  2. It parses as YAML and yields a mapping.
  3. `name` and `description` are present and are non-empty strings.
  4. `name` equals the directory name, which is how the skill is invoked.

Run:
    python scripts/check_skills.py          # from the repo root
Exits non-zero on the first failing skill, listing every problem found.
"""
from __future__ import annotations

import pathlib
import re
import sys

try:
    import yaml
except ModuleNotFoundError:
    sys.exit("PyYAML is required:  pip install pyyaml")

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# A plain (unquoted) YAML scalar cannot contain ": " — the single most likely
# way to break a long one-line description.
COLON_SPACE = re.compile(r":\s")


def colon_space_hint(block: str) -> str | None:
    """Point at the ": " that most likely broke an unquoted scalar."""
    for line in block.splitlines():
        key, sep, value = line.partition(": ")
        if not sep or not key or key.startswith((" ", "-", "#")):
            continue
        if COLON_SPACE.search(value):
            offender = COLON_SPACE.search(value)
            start = max(offender.start() - 40, 0)
            return (
                f"`{key}` looks like an unquoted scalar containing ': ', which YAML "
                f"cannot parse.\n"
                f"           near: ...{value[start:offender.end() + 40]}...\n"
                f"           fix:  replace the colon (an em dash reads well), or quote "
                f"the whole value."
            )
    return None


def check_skill(skill_dir: pathlib.Path) -> list[str]:
    problems: list[str] = []
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return [f"{skill_dir.name}: no SKILL.md"]

    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return [
            f"{skill_dir.name}: no YAML frontmatter — SKILL.md must begin with a "
            f"'---' line, the fields, then a closing '---' line"
        ]

    block = match.group(1)
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0]
        hint = colon_space_hint(block)
        problems.append(f"{skill_dir.name}: frontmatter is not valid YAML — {detail}")
        if hint:
            problems.append(f"{' ' * len(skill_dir.name)}  hint: {hint}")
        return problems

    if not isinstance(data, dict):
        return [
            f"{skill_dir.name}: frontmatter parsed as {type(data).__name__}, "
            f"expected a mapping of fields"
        ]

    for field in ("name", "description"):
        value = data.get(field)
        if value is None:
            problems.append(f"{skill_dir.name}: frontmatter is missing '{field}'")
        elif not isinstance(value, str) or not value.strip():
            problems.append(
                f"{skill_dir.name}: '{field}' must be a non-empty string, "
                f"got {value!r}"
            )

    name = data.get("name")
    if isinstance(name, str) and name != skill_dir.name:
        problems.append(
            f"{skill_dir.name}: frontmatter name is '{name}' but the directory is "
            f"'{skill_dir.name}' — the directory name is how the skill is invoked, "
            f"so the two must match"
        )

    return problems


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        print(f"no skills/ directory under {root}", file=sys.stderr)
        return 1

    skill_dirs = sorted(d for d in skills_dir.iterdir() if d.is_dir())
    if not skill_dirs:
        print("no skills found under skills/", file=sys.stderr)
        return 1

    all_problems: list[str] = []
    for skill_dir in skill_dirs:
        problems = check_skill(skill_dir)
        status = "FAIL" if problems else "ok"
        print(f"[{status:>4}] {skill_dir.name}")
        all_problems.extend(problems)

    if all_problems:
        print(f"\n{len(all_problems)} problem(s):", file=sys.stderr)
        for problem in all_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"\n{len(skill_dirs)} skill(s) validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
