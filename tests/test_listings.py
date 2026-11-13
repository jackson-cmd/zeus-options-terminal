"""Files that directories read from the top of the repository: glama.json lets Glama's MCP
directory confirm the maintainer, and CITATION.cff gives GitHub's "Cite this repository" and
Zenodo's archive of each release their details, naming the project rather than a person.
(server.json, for the MCP Registry, is in tests/test_mcp_registry.py.)"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_glama_claim_file_names_the_repository_owner():
    owner = re.search(r"github\.com/([\w-]+)/zeus-options-terminal", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))[1]
    data = json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))
    assert data == {"$schema": "https://glama.ai/mcp/schemas/server.json", "maintainers": [owner]}


def test_citation_names_the_project_not_a_person():
    text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    assert "cff-version: 1.2.0" in text and "type: software" in text
    assert '  - name: "ZEUS Terminal contributors"' in text
    for key in ("given-names:", "family-names:", "email:", "orcid:", "affiliation:"):
        assert key not in text, key
    license_ = re.search(r'^license = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert f"license: {license_}" in text
    assert 'repository-code: "https://github.com/jackson-cmd/zeus-options-terminal"' in text
