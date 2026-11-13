"""tools/install_git_hook.sh puts the privacy check where git runs it: from the main checkout
and from a worktree (git worktree add), whose .git is a file rather than a folder. It never
writes into a hooks folder set by core.hooksPath, which other repositories may share."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash") or not shutil.which("git"),
                                reason="the installer is a shell script for macOS and Linux")


def run(cwd, *cmd, check=True):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                          check=check)


@pytest.fixture
def repo(tmp_path):
    """A small repository holding the two tools, and a worktree of it on its own branch."""
    main = tmp_path / "main"
    (main / "tools").mkdir(parents=True)
    for name in ("install_git_hook.sh", "privacy_check.py"):
        shutil.copy2(ROOT / "tools" / name, main / "tools" / name)
    (main / "README.md").write_text("hello\n", encoding="utf-8")
    run(main, "git", "init", "-q")
    run(main, "git", "add", "-A")
    run(main, "git", "commit", "-q", "-m", "start")
    run(main, "git", "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    return main, tmp_path / "wt"


def test_installs_from_a_worktree_and_blocks_a_key_file_there(repo):
    main, wt = repo
    run(wt, "bash", "tools/install_git_hook.sh")
    hook = main / ".git" / "hooks" / "pre-commit"            # the folder every worktree shares
    assert hook.is_file() and os.access(hook, os.X_OK)
    assert "tools/privacy_check.py" in hook.read_text(encoding="utf-8")
    (wt / "keys.env").write_text("TOKEN=x\n", encoding="utf-8")
    run(wt, "git", "add", "keys.env")
    commit = run(wt, "git", "commit", "-q", "-m", "oops", check=False)
    assert commit.returncode != 0
    assert "privacy check failed" in commit.stdout + commit.stderr


def test_installs_from_the_main_checkout(repo):
    main, _ = repo
    run(main, "bash", "tools/install_git_hook.sh")
    assert (main / ".git" / "hooks" / "pre-commit").is_file()


def test_leaves_a_core_hooks_path_folder_alone_and_says_so(repo, tmp_path):
    main, _ = repo
    shared = tmp_path / "shared-hooks"
    run(main, "git", "config", "core.hooksPath", str(shared))
    out = run(main, "bash", "tools/install_git_hook.sh")
    assert not shared.exists()
    assert (main / ".git" / "hooks" / "pre-commit").is_file()
    assert "core.hooksPath" in out.stderr
