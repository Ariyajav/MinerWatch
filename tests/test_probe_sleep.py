"""The installer's safety report depends on this probe being correct.

`install-task.ps1` prints which miners a mode will send real commands to, and
asks for confirmation before registering. That sentence is the last checkpoint
before hardware is actuated on a schedule, so the thing that produces it gets
tested like production code.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "probe_sleep.py"
INSTALLER = REPO / "scripts" / "install-task.ps1"

CONFIG = """
poll_interval_seconds: 15
timezone: UTC
schedule:
  days: [mon, tue, wed, thu, fri, sat, sun]
  hours: ["21:00-18:00"]
groups:
  watched:
    sleep:
      enabled: false
  scheduled:
    sleep:
      enabled: true
      dry_run: true
      backend: bitmain_http
miners:
  - id: rehearsing
    host: 10.0.0.1
    group: scheduled
  - id: live-one
    host: 10.0.0.2
    group: scheduled
    sleep:
      dry_run: false
  - id: monitor-only
    host: 10.0.0.3
    group: watched
"""


def _run_probe(config_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROBE), str(config_path)],
        capture_output=True,
        text=True,
    )


def _parse(stdout: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        if ":" in line:
            key, _, rest = line.partition(":")
            out[key] = [part for part in rest.split(",") if part]
    return out


def test_probe_separates_enabled_from_live(tmp_path):
    config = tmp_path / "miners.yaml"
    config.write_text(CONFIG, encoding="utf-8")

    result = _run_probe(config)

    assert result.returncode == 0, result.stderr
    parsed = _parse(result.stdout)
    # Both miners in the scheduled group have power control switched on...
    assert sorted(parsed["ENABLED"]) == ["live-one", "rehearsing"]
    # ...but only the one that opted out of dry-run gets commands sent to it.
    assert parsed["LIVE"] == ["live-one"]


def test_probe_prints_both_lines_when_nothing_is_enabled(tmp_path):
    """An empty fleet must still print both keys.

    The installer treats a missing line as a hard error, because "no line" and
    "no miners" mean opposite things and confusing them is how a live config
    gets reported as a rehearsal.
    """
    config = tmp_path / "miners.yaml"
    config.write_text(
        "poll_interval_seconds: 15\n"
        "timezone: UTC\n"
        "schedule:\n"
        "  days: [mon]\n"
        '  hours: ["09:00-17:00"]\n'
        "miners:\n"
        "  - id: only\n"
        "    host: 10.0.0.9\n",
        encoding="utf-8",
    )

    result = _run_probe(config)

    assert result.returncode == 0, result.stderr
    assert "ENABLED:" in result.stdout
    assert "LIVE:" in result.stdout
    parsed = _parse(result.stdout)
    assert parsed["ENABLED"] == []
    assert parsed["LIVE"] == []


def test_probe_fails_loudly_on_a_bad_config(tmp_path):
    """A config the loader rejects must be a non-zero exit, not empty output.

    The installer keys its refusal off the exit code; a probe that failed
    quietly is what let a broken read masquerade as "actuates nothing".
    """
    config = tmp_path / "miners.yaml"
    config.write_text("miners: [oh dear: : :\n", encoding="utf-8")

    result = _run_probe(config)

    assert result.returncode != 0
    assert "ENABLED:" not in result.stdout


def test_probe_fails_loudly_on_a_missing_config(tmp_path):
    result = _run_probe(tmp_path / "not-here.yaml")

    assert result.returncode != 0
    assert "ENABLED:" not in result.stdout


def test_probe_runs_from_an_unrelated_working_directory(tmp_path):
    """Task Scheduler and the installer both invoke this by absolute path."""
    config = tmp_path / "miners.yaml"
    config.write_text(CONFIG, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PROBE), str(config)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert _parse(result.stdout)["LIVE"] == ["live-one"]


def test_installer_does_not_inline_python_source():
    """Guard against the SyntaxError coming back.

    PowerShell strips double quotes out of a string on its way to a native
    executable, so any inline `python -c` snippet containing them arrives
    mangled. The probe must stay in a file.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    assert "probe_sleep.py" in text
    assert "-c $probe" not in text
