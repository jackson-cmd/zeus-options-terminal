"""pip / pipx install (pyproject.toml): the version comes from APP_VERSION, the dependencies are the
ones in requirements.txt, the wheel holds the zeus/ folder, and the `zeus` command starts the same
server as start.sh. CI builds the wheel, installs it in a clean environment and starts it."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import zeus_config
import zeus_cli

ROOT = Path(__file__).resolve().parent.parent
tomllib = pytest.importorskip("tomllib")          # Python 3.11+; the 3.12 and 3.13 jobs run these
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def requirement_lines(name):
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [re.sub(r"\s+#.*$", "", l).strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]


def test_version_comes_from_app_version():
    v = PYPROJECT["tool"]["hatch"]["version"]
    assert v["path"] == "zeus/zeus_config.py"
    text = (ROOT / v["path"]).read_text(encoding="utf-8")
    assert re.search(v["pattern"], text, re.M).group("version") == zeus_config.APP_VERSION


def test_dependencies_match_requirements_txt():
    """start.sh and the Docker image install requirements.txt; the wheel must ask for the same."""
    assert PYPROJECT["project"]["dependencies"] == requirement_lines("requirements.txt")
    extras = sorted(sum(PYPROJECT["project"]["optional-dependencies"].values(), []))
    assert extras == sorted(requirement_lines("requirements-optional.txt"))


def test_the_command_and_the_wheel():
    p = PYPROJECT["project"]
    assert p["name"] == "zeus-options-terminal" and p["requires-python"] == ">=3.10"
    assert p["scripts"] == {"zeus": "zeus.zeus_cli:main", "zeus-terminal": "zeus.zeus_cli:main"}
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["zeus"]
    assert (ROOT / "LICENSE").exists() and p["license-files"] == ["LICENSE"]


def test_zeus_starts_the_server_like_start_sh(monkeypatch):
    started = {}

    class Proc:
        def __init__(self, cmd, cwd=None, env=None):
            started.update(cmd=cmd, cwd=cwd, env=env)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(zeus_cli.subprocess, "Popen", Proc)
    monkeypatch.setattr(zeus_cli.threading, "Thread", lambda **kw: pytest.fail("--no-browser opened a browser"))
    assert zeus_cli.main(["--port", "8799", "--no-browser"]) == 0
    assert started["cmd"] == [sys.executable, str(ROOT / "zeus" / "options_analyzer" / "optionstrat_server.py")]
    assert started["cwd"] == str(ROOT)                          # the folder that holds zeus/, as in start.sh
    assert started["env"]["PORT"] == "8799" and started["env"]["ZEUS_PYTHON"] == sys.executable


def test_zeus_version_and_help():
    out = subprocess.run([sys.executable, str(ROOT / "zeus" / "zeus_cli.py"), "--version"],
                         capture_output=True, encoding="utf-8")
    assert out.returncode == 0 and out.stdout.strip() == f"ZEUS Terminal {zeus_config.APP_VERSION}"
    # a Windows pipe writes cp1252, which has no 宙斯: zeus must still print its help
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    out = subprocess.run([sys.executable, str(ROOT / "zeus" / "zeus_cli.py"), "--help"],
                         capture_output=True, encoding="utf-8", env=env)
    assert out.returncode == 0 and "--no-browser" in out.stdout and "宙斯期权终端" in out.stdout
