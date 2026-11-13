"""ZEUS in the official MCP Registry (registry.modelcontextprotocol.io). server.json says how an
MCP client starts ZEUS from PyPI or from the image and where its MCP endpoint answers. The
registry accepts a version only when the PyPI description (README.md) carries "mcp-name: <name>"
and the image carries the io.modelcontextprotocol.server.name label; .github/workflows/docker.yml
publishes a release after its image is pushed."""
import json
import subprocess
import sys
from pathlib import Path

import zeus_config

ROOT = Path(__file__).resolve().parent.parent
NAME = "io.github.jackson-cmd/zeus-options-terminal"
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
PYPI, OCI = SERVER["packages"]
WORKFLOW = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")


def test_server_json_names_this_repository_and_the_app_version():
    v = zeus_config.APP_VERSION
    assert SERVER["$schema"] == "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    assert SERVER["name"] == NAME and 0 < len(SERVER["description"]) <= 100
    assert SERVER["repository"] == {"url": "https://github.com/jackson-cmd/zeus-options-terminal", "source": "github"}
    assert (PYPI["registryType"], PYPI["identifier"], OCI["registryType"]) == ("pypi", "zeus-options-terminal", "oci")
    assert SERVER["version"] == PYPI["version"] == v
    assert OCI["identifier"] == f"ghcr.io/jackson-cmd/zeus-options-terminal:{v}"


def test_the_registry_can_tell_both_packages_are_ours():
    assert (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0] == f"<!-- mcp-name: {NAME} -->"
    assert f'io.modelcontextprotocol.server.name="{NAME}"' in (ROOT / "zeus" / "Dockerfile").read_text(encoding="utf-8")


def test_uvx_starts_zeus_with_the_listed_arguments_and_the_url_follows_the_port():
    """uvx runs the command named like the package (pyproject.toml), with these arguments."""
    args = []
    for a in PYPI["packageArguments"]:
        args += [a["value"]] if a["type"] == "positional" else [a["name"], a["default"]]
    assert args == ["--no-browser", "--port", "8765"]
    out = subprocess.run([sys.executable, str(ROOT / "zeus" / "zeus_cli.py"), *args, "--version"],
                         capture_output=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr                     # zeus accepts them
    assert PYPI["runtimeHint"] == "uvx"
    assert PYPI["transport"] == {"type": "streamable-http", "url": "http://localhost:{--port}/mcp"}


def test_the_image_entry_stays_on_this_computer_and_opens_mcp_over_http():
    runtime = {a["name"]: a["value"] for a in OCI["runtimeArguments"]}
    assert runtime == {"-p": "127.0.0.1:8765:8765", "-v": "zeus-data:/data", "-e": "ZEUS_MCP_FROM_HOST=1"}
    assert OCI["transport"] == {"type": "streamable-http", "url": "http://localhost:8765/mcp"}


def test_docker_workflow_publishes_after_the_push_with_a_verified_publisher():
    job = WORKFLOW.split("\n  mcp-registry:\n", 1)[1]
    assert "needs: image" in job and "id-token: write" in job
    assert "needs.image.outputs.push == 'true' && needs.image.outputs.newest == 'true'" in job
    assert "MCP_PUBLISHER_VERSION: v1." in job
    assert "cosign verify-blob" in job and "registry/.github/workflows/release.yml@refs/tags/$MCP_PUBLISHER_VERSION" in job
    assert "registry.modelcontextprotocol.io/v0.1/servers/" in job                # skip a version it has
    assert "login github-oidc" in job and "publish server.json" in job
    for key in ("version:", "push:", "newest:"):
        assert f"      {key} ${{{{ steps.v.outputs." in WORKFLOW
