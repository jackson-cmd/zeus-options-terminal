#!/usr/bin/env python3
"""Version and release notes for .github/workflows/release.yml.

When a merge to main raises APP_VERSION in zeus/zeus_config.py, the workflow tags that
commit v<version> and publishes a GitHub release. Its notes are that version's sections of
CHANGELOG.md and CHANGELOG_zh.md, with relative links turned into links that work on the
release page.

    python tools/release_notes.py version    # the version, for example 0.5.1
    python tools/release_notes.py notes      # the release notes, in Markdown
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/jackson-cmd/zeus-options-terminal"
SITE = "https://jackson-cmd.github.io/zeus-options-terminal/"
HEADING = re.compile(r"^## +(\d+\.\d+\.\d+)\b")


def app_version() -> str:
    text = (ROOT / "zeus" / "zeus_config.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', text, re.M)
    if not m:
        sys.exit('zeus/zeus_config.py has no APP_VERSION = "x.y.z"')
    return m.group(1)


def versions(path: Path) -> list[str]:
    """Every version heading of a changelog, in file order (newest first)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [m.group(1) for m in map(HEADING.match, lines) if m]


def section(path: Path, version: str) -> str:
    """The text under '## <version> …', up to the next '## ' heading."""
    out = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if out is not None:
                break
            m = HEADING.match(line)
            if m and m.group(1) == version:
                out = []
            continue
        if out is not None:
            out.append(line)
    if not out or not "".join(out).strip():
        sys.exit(f"{path.name} has no notes under '## {version}'")
    return "\n".join(out).strip()


def absolute_links(text: str, tag: str) -> str:
    """A changelog links to files in the repository; a release page needs full URLs."""
    text = re.sub(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)", lambda m: f"]({REPO}/blob/{tag}/{m.group(1)})", text)
    return re.sub(r'src="(?!https?://)([^"]+)"', lambda m: f'src="{REPO}/raw/{tag}/{m.group(1)}"', text)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def notes(version: str) -> str:
    tag = f"v{version}"
    older = [v for v in versions(ROOT / "CHANGELOG.md") if _key(v) < _key(version)]
    links = [f"[Website]({SITE})", f"[User manual]({REPO}/blob/{tag}/docs/USER_GUIDE.md)",
             f"[使用手册]({REPO}/blob/{tag}/docs/USER_GUIDE_zh.md)"]
    if older:
        prev = max(older, key=_key)
        links.append(f"[All changes since v{prev}]({REPO}/compare/v{prev}...{tag})")
    en = absolute_links(section(ROOT / "CHANGELOG.md", version), tag)
    zh = absolute_links(section(ROOT / "CHANGELOG_zh.md", version), tag)
    return f"{' · '.join(links)}\n\n{en}\n\n---\n\n{zh}\n"


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[1] not in ("version", "notes"):
        sys.exit(__doc__)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    version = app_version()
    sys.stdout.write(f"{version}\n" if argv[1] == "version" else notes(version))


if __name__ == "__main__":
    main(sys.argv)
