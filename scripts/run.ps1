<#
.SYNOPSIS
    Start the MinerWatch supervision loop on Windows.

.DESCRIPTION
    Wrapper around `python -m minerwatch run`. Handles the two things a Task
    Scheduler entry gets wrong on its own: the working directory (tasks start
    in C:\Windows\System32) and the console code page (legacy by default, which
    makes non-ASCII log output raise UnicodeEncodeError).

    Everything is a rehearsal unless -Live, -LiveWatchdog, or -LiveSleep is
    passed: no restart and no sleep command reaches a miner.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run.ps1

.EXAMPLE
    # Real sleep/wake, but still rehearse watchdog restarts.
    powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -LiveSleep

.NOTES
    Task Scheduler action:
      Program:   powershell.exe
      Arguments: -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\minerwatch\scripts\run.ps1" -LiveSleep
      Start in:  C:\path\to\minerwatch
    Choose "Run whether user is logged on or not" and, under Settings,
    "If the task is already running: Do not start a new instance".
#>
[CmdletBinding()]
param(
    [string]$Config = "miners.yaml",
    [switch]$Live,
    [switch]$LiveWatchdog,
    [switch]$LiveSleep,
    # Force a rehearsal even where miners.yaml sets sleep.dry_run: false.
    # Without this, "no live flags" means "whatever the config says", which is
    # not the same thing as rehearsing.
    [switch]$DryRun,
    [switch]$DebugLogging,
    # Append to a rotating log file as well as the console. A scheduled task
    # has no console at all, so this is how an unattended run stays diagnosable.
    [string]$LogFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Force UTF-8 so log records containing non-ASCII text do not kill the process.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "No virtual environment found. Run scripts\setup.ps1 first."
}

$argv = @("-m", "minerwatch", "-c", $Config)
if ($DebugLogging) { $argv += "--verbose" }
if ($LogFile)      { $argv += @("--log-file", $LogFile) }
$argv += "run"
if ($DryRun)       { $argv += "--dry-run" }
if ($Live)         { $argv += "--live" }
if ($LiveWatchdog) { $argv += "--live-watchdog" }
if ($LiveSleep)    { $argv += "--live-sleep" }

& $venvPython @argv
exit $LASTEXITCODE
