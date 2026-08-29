<#
.SYNOPSIS
    Create the MinerWatch virtual environment on a Windows host.

.DESCRIPTION
    Builds .venv with the repository's Python, installs the runtime and dev
    dependencies (including tzdata, which Windows needs because it ships no
    IANA time zone database), and runs the test suite to confirm the install.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
[CmdletBinding()]
param(
    # Interpreter to build the venv from. Leave empty to auto-detect the newest
    # supported one installed; pass e.g. -Python "py" -PythonArgs "-3.12" to pin.
    [string]$Python = "",
    [string]$PythonArgs = "",
    [switch]$SkipTests,
    # Rebuild .venv even if one already exists (use after changing interpreter).
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Lowest interpreter the code runs on; keep in sync with requires-python in
# pyproject.toml and MIN_PYTHON in minerwatch/compat.py.
$MinMajor = 3
$MinMinor = 10

function Test-SupportedPython {
    param([string]$Exe, [string[]]$ExeArgs)
    try {
        $out = & $Exe @ExeArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $false }
    $parts = "$out".Trim().Split(".")
    if ($parts.Count -lt 2) { return $false }
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    return ($major -gt $MinMajor) -or ($major -eq $MinMajor -and $minor -ge $MinMinor)
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if ($Recreate -and (Test-Path (Join-Path $root ".venv"))) {
    Write-Host "Removing existing .venv ..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force (Join-Path $root ".venv")
}

# An existing venv built with an unsupported interpreter is worse than none:
# `pip install -e .` refuses it with "requires a different Python", leaves the
# dependencies uninstalled, and every later command dies on `import yaml`
# instead of naming the real cause.
if (Test-Path $venvPython) {
    if (Test-SupportedPython -Exe $venvPython -ExeArgs @()) {
        Write-Host "Reusing existing .venv" -ForegroundColor DarkGray
    } else {
        $found = & $venvPython -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
        throw ("The existing .venv runs Python $found, but MinerWatch needs " +
               "$MinMajor.$MinMinor or newer. Rebuild it with:`n" +
               "    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Recreate")
    }
} else {
    # Candidates newest-first. The py launcher is preferred because it finds
    # interpreters that are not on PATH.
    $candidates = @()
    if ($Python) {
        $candidates += ,@{ Exe = $Python; Args = @($PythonArgs | Where-Object { $_ }) }
    } else {
        foreach ($v in @("3.13", "3.12", "3.11", "3.10")) {
            $candidates += ,@{ Exe = "py"; Args = @("-$v") }
        }
        $candidates += ,@{ Exe = "python"; Args = @() }
        $candidates += ,@{ Exe = "python3"; Args = @() }
    }

    $chosen = $null
    foreach ($c in $candidates) {
        if (Test-SupportedPython -Exe $c.Exe -ExeArgs $c.Args) { $chosen = $c; break }
    }
    if (-not $chosen) {
        throw ("No Python $MinMajor.$MinMinor or newer found. Install one from " +
               "https://www.python.org/downloads/ and re-run this script, or " +
               "point at it explicitly:`n" +
               "    scripts\setup.ps1 -Python `"C:\Path\To\python.exe`"")
    }

    $label = (@($chosen.Exe) + $chosen.Args) -join " "
    Write-Host "Creating virtual environment in .venv using $label ..." -ForegroundColor Cyan
    & $chosen.Exe @($chosen.Args) -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}

Write-Host "Installing dependencies ..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

# Prove the time zone database is actually present: without it every schedule
# lookup fails at runtime, and the failure would otherwise only show up on the
# first poll.
& $venvPython -c "from zoneinfo import ZoneInfo; ZoneInfo('UTC'); print('tzdata OK')"
if ($LASTEXITCODE -ne 0) { throw "Time zone database missing; 'pip install tzdata' did not take effect." }

if (-not $SkipTests) {
    Write-Host "Running tests ..." -ForegroundColor Cyan
    & $venvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Test suite failed." }
}

# Prove the CLI is importable and runnable through the interpreter, rather than
# assuming it. `-m minerwatch` is the canonical invocation and does not depend
# on entry points being written.
& $venvPython -m minerwatch --help *> $null
if ($LASTEXITCODE -ne 0) { throw "The minerwatch CLI could not be started; the install did not complete." }

Write-Host ""
Write-Host "Ready. Next steps:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python.exe -m minerwatch -c miners.yaml status"
Write-Host "  .\.venv\Scripts\python.exe -m minerwatch -c miners.yaml run"

# Only advertise the console script if pip actually wrote it. Editable installs
# do not always produce one, and pointing at an .exe that is not there is worse
# than not mentioning it.
$consoleScript = Join-Path $root ".venv\Scripts\minerwatch.exe"
if (Test-Path $consoleScript) {
    Write-Host ""
    Write-Host "Shorthand (console script found):" -ForegroundColor DarkGray
    Write-Host "  .\.venv\Scripts\minerwatch.exe -c miners.yaml status"
}
