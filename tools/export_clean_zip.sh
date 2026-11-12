#!/usr/bin/env bash
# Make a shareable zip that contains exactly what git would publish — code only.
# Never zips runtime data, settings, tokens or the links that point to them.
# Usage: tools/export_clean_zip.sh [output.zip]
set -euo pipefail
cd "$(dirname "$0")/.."
python3 tools/privacy_check.py --all
out="${1:-../zeus-options-terminal.zip}"
rm -f "$out"
git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' f; do
  [ -L "$f" ] && continue      # never store or follow links
  [ -f "$f" ] && printf '%s\0' "$f"
done | xargs -0 zip -q -y "$out"
echo "clean archive → $out ($(unzip -l "$out" | tail -1 | awk '{print $2}') files)"
