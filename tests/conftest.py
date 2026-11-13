"""Test setup: every test session gets its own throw-away settings folder, so the
tests never read or write your real ~/.zeus-terminal and never touch the network
for quotes (each test that needs prices patches them)."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "zeus"                      # all the code lives here
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "benzinga_digest" / "src"))
os.environ["ZEUS_HOME"] = tempfile.mkdtemp(prefix="zeus-test-")
