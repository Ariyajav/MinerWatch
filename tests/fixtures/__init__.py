"""Static configuration fixtures for the test suite."""

from pathlib import Path

#: The config the schedule and config-loading assertions are written against.
#: Resolved from this file so the tests pass from any working directory.
MINERS_YAML = str(Path(__file__).parent / "miners.yaml")
