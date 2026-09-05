"""Keep local-only fixture modules importable across integration tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
