#!/usr/bin/env bash
# run_optvol.sh — 日终刷新 Options 量稳定性数据。
# 先爬 options-history → history.parquet,再 build_summary → summary.json。
# 日志追加到 data/options_vol/optvol.log。
set -uo pipefail

PY="${ZEUS_PYTHON:-$(command -v python3)}"
# 本脚本在 options_analyzer/options_vol/ 下;cd 到 options_analyzer/ 才能 -m options_vol.*
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANALYZER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$ANALYZER_DIR/.." && pwd)"
LOG="$REPO_ROOT/data/options_vol/optvol.log"
mkdir -p "$(dirname "$LOG")"

cd "$ANALYZER_DIR"

{
  echo "==================================================================="
  echo "[optvol] start $(date '+%Y-%m-%d %H:%M:%S')"

  echo "[optvol] 1/2 scrape_history …"
  "$PY" -m options_vol.scrape_history
  rc_scrape=$?
  echo "[optvol] scrape_history rc=$rc_scrape"

  # build_summary 由另一 agent 实现;存在才跑,不存在不算失败(爬取才是本单元关键)
  echo "[optvol] 2/2 build_summary …"
  "$PY" -m options_vol.build_summary
  rc_build=$?
  echo "[optvol] build_summary rc=$rc_build"

  echo "[optvol] done $(date '+%Y-%m-%d %H:%M:%S')"
} 2>&1 | tee -a "$LOG"
