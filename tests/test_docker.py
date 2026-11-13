"""The Docker image: what the Dockerfile and its build context promise, the container-only code
paths (detection, the gateway address, runtime links in a read-only code folder, a local model
server on the host), and that every documented `docker run` keeps the port on 127.0.0.1.
The image itself is built and started by .github/workflows/docker.yml (tools/docker_smoke.sh)."""
import os
import re
import shutil
import stat
from pathlib import Path

import pytest

import zeus_ai
import zeus_config as zc

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "zeus" / "Dockerfile").read_text(encoding="utf-8")
IGNORE = [ln.strip() for ln in (ROOT / "zeus" / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
          if ln.strip() and not ln.startswith("#")]
WORKFLOW = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
DOCS = ["README.md", "README_zh.md", "docs/USER_GUIDE.md", "docs/USER_GUIDE_zh.md", "docs/FEATURES.md",
        "docs/FEATURES_zh.md", "zeus/Dockerfile", ".github/workflows/docker.yml", "CHANGELOG.md", "CHANGELOG_zh.md"]


def env_of(dockerfile: str) -> dict:
    block = re.search(r"^ENV (.+?)(?=^\S)", dockerfile, re.M | re.S).group(1)
    return dict(re.findall(r"(\w+)=(\S+)", block))


# ------------------------------------------------------------------ Dockerfile and context
def test_image_listens_everywhere_but_keeps_the_localhost_guard():
    """HOST only moves the listening socket; ZEUS_HOST would also switch the DNS-rebinding guard
    off (the guard reads the host setting), so the image must never set it."""
    env = env_of(DOCKERFILE)
    assert env["HOST"] == "0.0.0.0" and "ZEUS_HOST" not in env
    assert env["ZEUS_HOME"] == "/data" and env["ZEUS_PORT"] == "8765" and env["ZEUS_CONTAINER"] == "1"
    assert env["TZ"] and "tzdata" in DOCKERFILE
    assert 'VOLUME ["/data"]' in DOCKERFILE and "EXPOSE 8765" in DOCKERFILE
    assert re.search(r"^USER zeus$", DOCKERFILE, re.M) and "useradd --uid 1000" in DOCKERFILE
    assert "org.opencontainers.image.source=https://github.com/jackson-cmd/zeus-options-terminal" in DOCKERFILE
    # the runtime links are made at build time, before the app's user loses write access to /app
    assert DOCKERFILE.index("relocate_runtime_dirs()") < DOCKERFILE.index("USER zeus")
    assert 'CMD ["python", "zeus/options_analyzer/optionstrat_server.py"]' in DOCKERFILE


def test_build_context_is_code_only_and_leaves_runtime_data_out():
    assert IGNORE[0] == "*" and {"!zeus", "!requirements.txt", "!LICENSE"} <= set(IGNORE)
    assert {"zeus/Dockerfile", "zeus/Dockerfile.dockerignore"} <= set(IGNORE)
    for rel in zc.RUNTIME_DIRS:
        assert "zeus/" + rel in IGNORE, rel
    assert "zeus/benzinga/data" in IGNORE
    for pattern in ("**/*secret*", "**/.env", "**/*cookies*.json", "**/*token*.json", "**/__pycache__"):
        assert pattern in IGNORE, pattern


def test_workflow_pushes_three_tags_after_the_check():
    assert "packages: write" in WORKFLOW and "ghcr.io/${{ github.repository }}" in WORKFLOW
    assert "linux/amd64,linux/arm64" in WORKFLOW and WORKFLOW.count("file: zeus/Dockerfile") == 2
    for tag in ("value=${{ steps.v.outputs.version }}", "value=${{ steps.v.outputs.minor }}", "value=latest"):
        assert tag in WORKFLOW, tag
    assert WORKFLOW.index("tools/docker_smoke.sh zeus-smoke:ci") < WORKFLOW.index("push: ${{ steps.v.outputs.push == 'true' }}")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'gh workflow run docker.yml --repo "$GITHUB_REPOSITORY" --ref "v$VERSION"' in release
    assert "actions: write" in release
    smoke = ROOT / "tools" / "docker_smoke.sh"
    assert os.name == "nt" or smoke.stat().st_mode & stat.S_IXUSR


def test_every_documented_docker_run_publishes_on_localhost_only():
    """ZEUS has no login: a port published on all interfaces shows your positions to your network."""
    runs = 0
    for name in DOCS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for cmd in re.findall(r"docker run[^\n]*(?:\\\n[^\n]*)*", text):
            for p in re.findall(r"(?:-p|--publish)[ =](\S+)", cmd):
                assert p.startswith("127.0.0.1:"), f"{name}: {cmd}"
            runs += 1
    assert runs >= 8


# ------------------------------------------------------------------ container-only code paths
def test_container_needs_the_variable_and_a_marker(tmp_path, monkeypatch):
    marker = tmp_path / ".dockerenv"
    monkeypatch.setattr(zc, "CONTAINER_MARKERS", (str(marker),))
    monkeypatch.setenv("ZEUS_CONTAINER", "1")
    assert not zc.in_container()                 # the variable alone: a normal install
    marker.write_text("")
    assert zc.in_container()
    monkeypatch.delenv("ZEUS_CONTAINER")
    assert not zc.in_container()                 # a container that is not the ZEUS image


def test_gateway_comes_from_the_default_route(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n", encoding="ascii")
    import sys
    expected = "172.17.0.1" if sys.byteorder == "little" else "1.0.17.172"
    assert zc.container_gateway(str(route)) == expected
    route.write_text("Iface\tDestination\tGateway\tFlags\n" "eth0\t000011AC\t00000000\t0001\n", encoding="ascii")
    assert zc.container_gateway(str(route)) == ""             # no default route
    assert zc.container_gateway(str(tmp_path / "missing")) == ""


def test_runtime_links_made_at_build_time_survive_a_read_only_code_folder(tmp_path, monkeypatch):
    """The image makes the links while building; at start the app's user cannot write to /app,
    so relocating must find them in place, and must not crash when it cannot change one."""
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs POSIX permissions and a non-root user")
    code = tmp_path / "app" / "zeus"
    code.mkdir(parents=True)
    for rel in zc.RUNTIME_DIRS:
        (code / rel).parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(zc, "REPO_ROOT", code)
    monkeypatch.setenv("ZEUS_HOME", str(tmp_path / "data"))
    assert zc.relocate_runtime_dirs() == []                   # the build step
    dirs = [p for p in [code, *code.rglob("*")] if p.is_dir() and not p.is_symlink()]
    try:
        for d in dirs:
            d.chmod(0o555)
        assert zc.relocate_runtime_dirs() == []               # every start afterwards
        (code / "data" / "probe.txt").write_text("ok")        # the data still lands in ZEUS_HOME
        assert (tmp_path / "data" / "runtime" / "data" / "probe.txt").read_text() == "ok"
        shutil.rmtree(tmp_path / "data")                      # the links now dangle
        monkeypatch.setenv("ZEUS_HOME", str(tmp_path / "elsewhere"))
        left = zc.relocate_runtime_dirs()                     # ZEUS_HOME moved: reported, no crash
        assert set(left) == set(zc.RUNTIME_DIRS)
    finally:
        for d in dirs:
            d.chmod(0o755)


@pytest.mark.parametrize("url, provider", [
    ("http://host.docker.internal:11434/v1", "openai"),   # Ollama on the host, seen from the image
    ("http://localhost:11434/v1", "openai"),
    ("http://127.0.0.1:1234/v1", "openai"),
    ("https://api.openai.com/v1", "off"),                  # a cloud endpoint still needs a key
])
def test_a_model_server_on_the_host_needs_no_key(monkeypatch, url, provider):
    values = {"ai_provider": "openai", "openai_base_url": url, "openai_api_key": ""}
    monkeypatch.setattr(zeus_ai.hc, "get", lambda k, default=None: values.get(k, default))
    assert zeus_ai.provider() == provider
