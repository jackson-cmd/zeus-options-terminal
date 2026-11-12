"""Test setup: every test session gets its own throw-away settings folder, so the
tests never read or write your real ~/.zeus-terminal and never touch the network
for quotes (each test that needs prices patches them)."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benzinga_digest" / "src"))
os.environ["ZEUS_HOME"] = tempfile.mkdtemp(prefix="zeus-test-")
