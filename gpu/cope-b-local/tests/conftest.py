"""Put gpu/cope-b-local/ and gpu/cope-b-runpod/ (for prompt.py) on the path."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "cope-b-runpod"))
