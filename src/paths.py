"""Installation layout.

ROOT is the installation (repository) root. Runtime state, logs and every
per-installation identity (auth profile, scheduled task, lifecycle mutex)
stay keyed to ROOT, so reorganizing source folders never orphans them.
"""
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
CONFIG = ROOT / 'config'
SCRIPTS = ROOT / 'scripts'
