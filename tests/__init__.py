"""Offline tests. Run from the repository root:

    python -B -X utf8 -m unittest discover -s tests -t . -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
# Modules import each other as top-level names, as the runtime does.
for folder in (Path(__file__).resolve().parent, SRC):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
CONFIG = ROOT / "config"
