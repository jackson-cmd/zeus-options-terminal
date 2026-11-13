"""tools/release_notes.py, which .github/workflows/release.yml runs when a merge raises the
version: the version it reads is the app's, both changelogs open with that version, and the
notes carry both languages with links that work on a release page."""
import importlib.util
import re
from pathlib import Path

import pytest

import zeus_config

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/jackson-cmd/zeus-options-terminal"

_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools" / "release_notes.py")
rn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rn)


def test_version_is_the_app_version():
    assert rn.app_version() == zeus_config.APP_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", rn.app_version())


def test_both_changelogs_open_with_the_current_version():
    """A raised APP_VERSION without changelog sections would publish a release without notes."""
    en, zh = rn.versions(ROOT / "CHANGELOG.md"), rn.versions(ROOT / "CHANGELOG_zh.md")
    assert en[0] == rn.app_version() and en == zh
    assert en == sorted(en, key=rn._key, reverse=True) and len(set(en)) == len(en)


def test_notes_have_both_languages_and_full_links():
    v = rn.app_version()
    text = rn.notes(v)
    assert "\n---\n" in text and re.search(r"[一-鿿]", text.split("\n---\n")[1])
    assert not re.findall(r"\]\((?!https?://|#)", text), "relative link left in the notes"
    assert not re.findall(r'src="(?!https?://)', text)
    older = [x for x in rn.versions(ROOT / "CHANGELOG.md") if rn._key(x) < rn._key(v)]
    if older:
        assert f"{REPO}/compare/v{max(older, key=rn._key)}...v{v}" in text


def test_section_reads_one_version(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    log.write_text("# Changelog\n\nIntro [zh](CHANGELOG_zh.md)\n\n## 1.1.0 — 2026-01-02\n\n### Fixed\n\n"
                   "- New [page](docs/X.md#a) <img src=\"docs/b.png\"> (#3)\n\n## 1.0.0 —— 2026-01-01\n\n- First\n",
                   encoding="utf-8")
    assert rn.versions(log) == ["1.1.0", "1.0.0"]
    assert rn.section(log, "1.1.0") == "### Fixed\n\n- New [page](docs/X.md#a) <img src=\"docs/b.png\"> (#3)"
    assert rn.section(log, "1.0.0") == "- First"
    with pytest.raises(SystemExit):
        rn.section(log, "0.9.0")
    linked = rn.absolute_links(rn.section(log, "1.1.0"), "v1.1.0")
    assert f"]({REPO}/blob/v1.1.0/docs/X.md#a)" in linked and f'src="{REPO}/raw/v1.1.0/docs/b.png"' in linked
