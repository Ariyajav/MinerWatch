"""``python -m minerwatch`` entry point.

Delegates to :mod:`minerwatch.cli`. Kept as a thin shim so that the historic
invocation ``python -m minerwatch miners.yaml`` keeps working alongside the
subcommand form ``python -m minerwatch -c miners.yaml run``.

Both guards below exist because the failure they replace points at the wrong
problem. An interpreter that is too old, or one that simply is not the venv's,
surfaces as a 12-line traceback ending in ``No module named 'yaml'`` — which
reads as a missing package and sends people off to run pip, where they either
get the same error again or a message about a Python version they had no reason
to suspect.
"""

import sys

from minerwatch.compat import python_too_old, python_version_message

if python_too_old():
    print(python_version_message(), file=sys.stderr)
    sys.exit(2)

try:
    from minerwatch.cli import main
except ModuleNotFoundError as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(2)

if __name__ == "__main__":
    sys.exit(main())
