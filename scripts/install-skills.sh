#!/usr/bin/env bash
#
# install-skills.sh — install the Rockfish skills into an agent's skills directory.
#
# By default this symlinks each skill under ./skills/ into your personal
# Claude skills directory (~/.claude/skills, or $CLAUDE_CONFIG_DIR/skills).
# Symlinks mean upgrades are free: `git pull` in this repo updates the
# installed skills in place.
#
# Usage:
#   scripts/install-skills.sh                 # symlink into ~/.claude/skills
#   scripts/install-skills.sh --project DIR   # symlink into DIR/.claude/skills
#   scripts/install-skills.sh --copy          # copy instead of symlink (no auto-upgrade)
#   scripts/install-skills.sh --uninstall     # remove skills this repo installed
#   scripts/install-skills.sh --list          # show which skills would be installed
#   scripts/install-skills.sh --help
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_SRC="$REPO_ROOT/skills"

mode="symlink"       # symlink | copy | uninstall | list
target_base=""       # resolved below

usage() {
  # Print the leading comment block (lines 2.. up to the first non-comment line).
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --project)
      [ $# -ge 2 ] || { echo "error: --project needs a directory" >&2; exit 2; }
      target_base="$(cd "$2" && pwd)/.claude/skills"
      shift 2
      ;;
    --copy)      mode="copy";      shift ;;
    --uninstall) mode="uninstall"; shift ;;
    --list)      mode="list";      shift ;;
    -h|--help)   usage 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage 2 ;;
  esac
done

# Default target: personal Claude skills dir (honor CLAUDE_CONFIG_DIR).
if [ -z "$target_base" ]; then
  config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  target_base="$config_dir/skills"
fi

if [ ! -d "$SKILLS_SRC" ]; then
  echo "error: no skills/ directory found at $SKILLS_SRC" >&2
  exit 1
fi

# Collect skill directories (those containing a SKILL.md).
skills=()
for dir in "$SKILLS_SRC"/*/; do
  [ -f "${dir}SKILL.md" ] || continue
  skills+=("$(basename "$dir")")
done

if [ "${#skills[@]}" -eq 0 ]; then
  echo "error: no skills (directories with a SKILL.md) found under $SKILLS_SRC" >&2
  exit 1
fi

if [ "$mode" = "list" ]; then
  echo "Skills in this repo (would install to $target_base):"
  for name in "${skills[@]}"; do echo "  - $name"; done
  exit 0
fi

mkdir -p "$target_base"

for name in "${skills[@]}"; do
  src="$SKILLS_SRC/$name"
  dst="$target_base/$name"

  case "$mode" in
    uninstall)
      # Only remove our own symlink, or a copy we can identify by SKILL.md.
      if [ -L "$dst" ]; then
        rm "$dst"; echo "removed symlink  $dst"
      elif [ -d "$dst" ] && [ -f "$dst/SKILL.md" ]; then
        rm -rf "$dst"; echo "removed copy     $dst"
      else
        echo "skip (absent)    $dst"
      fi
      ;;
    symlink)
      if [ -e "$dst" ] || [ -L "$dst" ]; then rm -rf "$dst"; fi
      ln -s "$src" "$dst"
      echo "linked           $dst -> $src"
      ;;
    copy)
      if [ -e "$dst" ] || [ -L "$dst" ]; then rm -rf "$dst"; fi
      cp -R "$src" "$dst"
      echo "copied           $src -> $dst"
      ;;
  esac
done

echo
case "$mode" in
  uninstall) echo "Done. Restart your agent (or start a new session) to drop the skills." ;;
  *)         echo "Done. Restart your agent (or start a new session) to pick up the skills." ;;
esac
