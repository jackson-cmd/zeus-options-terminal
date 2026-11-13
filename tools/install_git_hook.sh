#!/usr/bin/env bash
# Install the privacy check as this clone's git pre-commit hook (local only, never pushed).
# Run it from the main checkout or from any worktree (git worktree add): the hook goes into
# the hooks folder they all share, found with git rev-parse --git-common-dir, because in a
# worktree .git is a file, not a folder.
set -euo pipefail
cd "$(dirname "$0")/.."
hook="$(git rev-parse --git-common-dir)/hooks/pre-commit"
mkdir -p "$(dirname "$hook")"
cat > "$hook" <<'HOOK'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/tools/privacy_check.py"
HOOK
chmod +x "$hook"
echo "privacy pre-commit hook installed → $hook"
# core.hooksPath (set by some tools, or in a global git config) makes git look elsewhere.
# Writing the hook there would run this repository's check in other repositories too.
if hooks_path="$(git config --get core.hooksPath)"; then
  echo "note: core.hooksPath is set to $hooks_path, so git runs hooks from there and will not run this one;" >&2
  echo "      unset it for this repository (git config --unset core.hooksPath) or call the check from your own hook." >&2
fi
