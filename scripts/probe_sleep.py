"""Report which miners a config would actuate, for install-task.ps1.

This lives in a file rather than inline in the PowerShell script on purpose.
PowerShell hands a string to a native executable by re-splitting it, and any
double quotes inside are consumed on the way — so an inline `python -c` snippet
arrived at the interpreter as `print(ENABLED: + ,.join(enabled))` and died with
a SyntaxError. The installer then read no miners at all and cheerfully reported
"this task will NOT send anything to any miner", which is the single sentence it
exists to get right.

Output is two lines, both always printed even when empty:

    ENABLED:<comma-separated ids>
    LIVE:<comma-separated ids>

ENABLED is every miner with software power control switched on. LIVE is the
subset that has opted out of dry-run and would therefore have commands sent to
it under `-Mode as-configured`.
"""

import sys
from pathlib import Path

# Allow running as a plain script from anywhere: Task Scheduler and the
# installer both invoke this by path, with no guarantee the repo root is on
# sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minerwatch.config import load_config  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: probe_sleep.py <config.yaml>", file=sys.stderr)
        return 2
    _, _, _, miners = load_config(argv[1])
    enabled = [m.id for m in miners.values() if m.sleep.enabled]
    live = [m.id for m in miners.values() if m.sleep.enabled and not m.sleep.dry_run]
    print("ENABLED:" + ",".join(enabled))
    print("LIVE:" + ",".join(live))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
