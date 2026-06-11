"""Test bootstrap.

`app.py` does work at import time: it requires VAULT_ROOT and SIGNAL_NUMBER in the
environment and resolves CLAUDE_BIN to a real executable (raising if it can't find
one). Set harmless values before any test imports `app` so the pure functions can be
exercised without a real Claude install or vault.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Make `app` and `intent_dispatcher` importable.
sys.path.insert(0, str(_REPO_ROOT))

# CLAUDE_BIN just needs to be an absolute path that exists — the test python is fine.
os.environ.setdefault("CLAUDE_BIN", sys.executable)
os.environ.setdefault("VAULT_ROOT", str(_REPO_ROOT))
os.environ.setdefault("SIGNAL_NUMBER", "+358000000000")
