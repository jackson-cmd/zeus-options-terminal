#!/usr/bin/env bash
# Install the privacy check as this clone's git pre-commit hook (local only, never pushed).
# Run it from the main checkout or from any worktree (git worktree add): the hook goes into
# the hooks folder they all share, found with git rev-parse --git-common-dir, because in a
# worktree .git is a file, not a folder. A pre-commit hook you already had is kept as
# pre-commit.local and still runs after the check. A checkout without tools/privacy_check.py
# (a branch from before it existed) commits with a note instead of failing.
set -euo pipefail
cd "$(dirname "$0")/.."
hooks="$(git rev-parse --git-common-dir)/hooks"
hook="$hooks/pre-commit"
mkdir -p "$hooks"
if [ -f "$hook" ] && ! grep -q privacy_check.py "$hook"; then
  mv "$hook" "$hooks/pre-commit.local"
  echo "kept your existing hook as $hooks/pre-commit.local; it still runs after the check"
fi
cat > "$hook" <<'HOOK'
#!/bin/sh
script="$(git rev-parse --show-toplevel)/tools/privacy_check.py"
if [ -f "$script" ]; then
  python3 "$script" || exit 1
else
  echo "privacy check: tools/privacy_check.py is not in this checkout; skipped"
fi
local_hook="$(git rev-parse --git-common-dir)/hooks/pre-commit.local"
if [ -x "$local_hook" ]; then exec "$local_hook" "$@"; fi
HOOK
chmod +x "$hook"
echo "privacy pre-commit hook installed → $hook"
# core.hooksPath (set by some tools, or in a global git config) makes git look elsewhere.
# Writing the hook there would run this repository's check in other repositories too.
if hooks_path="$(git config --get core.hooksPath)"; then
  echo "note: core.hooksPath is set to $hooks_path, so git runs hooks from there and will not run this one;" >&2
  echo "      unset it for this repository (git config --unset core.hooksPath) or call the check from your own hook." >&2
fi
