#!/usr/bin/env python3
"""Refuse to commit anything personal.

Run automatically as a git pre-commit hook (see tools/install_git_hook.sh) or by
hand:  python tools/privacy_check.py --all

It blocks a commit when a staged file
  • is a symlink, a runtime-data path, a credential-looking file, or larger than 3 MB;
  • contains something shaped like a secret (API keys, tokens, JWTs, private keys,
    webhook URLs, Bearer headers);
  • contains YOUR identity as this computer knows it: your home-folder path, your
    login name, the name / e-mail in your global git config.
Nothing personal is stored in this file — identities are read at run time.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 3 * 1024 * 1024
RUNTIME_PREFIXES = (
    "data/", "options_analyzer/data/", "trade_journal/data/", "pre_trade_checkup/data/", "stock_picker/data/",
    "eod_crawler/data_eod/", "eod_crawler/logs/", "barchart/downloads/", "barchart/snapshots/",
    "benzinga_digest/digests/", "benzinga_digest/ai_summary/", "benzinga_digest/pivot_summary/",
    "trading_assistant/data/", "trading_assistant/reports/", "winrate_scanner/data/", "benzinga/data/",
)
BAD_NAMES = re.compile(r"(^|/)(\.env(\..*)?|keys\.env|trades_raw\.json|.*cookies?.*\.(json|txt)|.*token.*\.(json|txt)|"
                       r".*secret.*|password.*|client_(id|secret)\.txt|refresh_token\.txt|data_apikey\.txt|.*\.(pem|key|p12|parquet|sqlite|db))$",
                       re.I)
SECRET_PATTERNS = {
    "AWS key": r"AKIA[0-9A-Z]{16}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "OpenAI/Anthropic key": r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "Slack token": r"xox[baprs]-[A-Za-z0-9-]{10,}",
    "JWT / OAuth token": r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "webhook URL": r"hooks\.slack\.com/services/|discord(?:app)?\.com/api/webhooks/",
    "Bearer header": r"Bearer [A-Za-z0-9._-]{24,}",
}
SKIP_CONTENT = ("package-lock.json",)


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout
    except Exception:
        return ""


def identity_markers() -> dict[str, str]:
    marks = {}
    home = str(Path.home())
    if len(home) > 3:
        marks["your home folder path"] = home
    try:
        import getpass
        user = getpass.getuser()
        if len(user) >= 4:
            marks["your login name"] = user
    except Exception:
        pass
    for key, label in (("user.email", "your global git e-mail"), ("user.name", "your global git name")):
        v = _git("config", "--global", "--get", key).strip()
        if len(v) >= 4 and "noreply" not in v:
            marks[label] = v
    return marks


def staged_files() -> list[tuple[str, str]]:
    """[(mode, path)] for added / changed files in the index."""
    out = []
    for line in _git("diff", "--cached", "--raw", "--no-renames", "--diff-filter=ACM").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 2:
            out.append((parts[1], path))
    return out


def all_files() -> list[tuple[str, str]]:
    out = []
    for line in _git("ls-files", "-s").splitlines():
        meta, _, path = line.partition("\t")
        out.append((meta.split()[0], path))
    for line in _git("ls-files", "--others", "--exclude-standard").splitlines():
        out.append(("100644", line))
    return out


def check(files: list[tuple[str, str]], staged: bool) -> list[str]:
    problems = []
    marks = identity_markers()
    for mode, path in files:
        if mode == "120000":
            problems.append(f"{path}: is a symlink (runtime links must never be committed)")
            continue
        if path.startswith(RUNTIME_PREFIXES):
            problems.append(f"{path}: runtime data folder")
            continue
        if BAD_NAMES.search(path):
            problems.append(f"{path}: looks like a credential or data file")
            continue
        if staged:
            blob = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True).stdout
        else:
            p = ROOT / path
            blob = p.read_bytes() if p.is_file() else b""
        if len(blob) > MAX_BYTES:
            problems.append(f"{path}: larger than 3 MB")
        if path.endswith(SKIP_CONTENT):
            continue
        text = blob.decode("utf-8", "ignore")
        for label, pat in SECRET_PATTERNS.items():
            if re.search(pat, text):
                problems.append(f"{path}: contains a {label}")
        for label, value in marks.items():
            seen = text
            if label == "your global git name":
                # a GitHub handle inside the repository's own URL is public anyway
                seen = seen.replace(f"github.com/{value}/", "").replace(f"{value}.github.io", "")
            if value and value in seen:
                problems.append(f"{path}: contains {label}")
    return problems


def main() -> int:
    every = "--all" in sys.argv
    files = all_files() if every else staged_files()
    problems = check(files, staged=not every)
    if problems:
        print("✋ privacy check failed — nothing was committed:")
        for p in problems:
            print("   •", p)
        print("Fix or unstage these files. (Bypass only if you are sure: git commit --no-verify)")
        return 1
    print(f"✓ privacy check passed ({len(files)} file(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
