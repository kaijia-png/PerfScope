import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "server"))
os.environ.setdefault("PERFSCOPE_HOME", tempfile.mkdtemp())
