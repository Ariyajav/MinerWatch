"""Make the simulators runnable as plain scripts.

``python sim/miner_sim.py`` puts ``sim/`` on ``sys.path``, not the repository
root, so ``import minerwatch`` would fail. Importing this module first puts the
repository root on the path when — and only when — the package is not already
importable, so both invocation styles work:

    python sim/miner_sim.py --port 4101
    python -m sim.miner_sim --port 4101
"""

import os
import sys

try:  # pragma: no cover - depends on how the script was launched
    import minerwatch  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
