"""Root conftest.py — adds the repo root to sys.path.

This ensures ``import cortex_python`` works in all test modules without
requiring a proper editable install of the package.

The cortex_python/pyproject.toml has a package-find issue (where=["."]
looks inside cortex_python/ rather than at the repo root) that makes
``pip install -e cortex_python/`` produce an empty package.  That is a
pre-existing Item 1 drift tracked for TARS — this conftest is the interim
workaround for pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root = this file's directory.
_REPO_ROOT = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
