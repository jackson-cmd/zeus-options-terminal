#!/usr/bin/env bash
# Install the privacy check as this clone's git pre-commit hook (local only, never pushed).
set -euo pipefail
cd "$(dirname "$0")/.."
hook=.git/hooks/pre-commit
cat > "$hook" <<'HOOK'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/tools/privacy_check.py"
HOOK
chmod +x "$hook"
echo "privacy pre-commit hook installed → $hook"
