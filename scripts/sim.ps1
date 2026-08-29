<#
.SYNOPSIS
    Start the miner simulators on Windows for a hardware-free test run.

.DESCRIPTION
    Launches one TCP simulator per port (matching the ports in miners.yaml) and,
    with -WithHttp, a Bitmain web-UI simulator for the bitmain_http backend.
    Each runs in its own window; close them or press Ctrl+C to stop.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\sim.ps1
    powershell -ExecutionPolicy Bypass -File scripts\sim.ps1 -Ports 4101,4102 -WithHttp
#>
[CmdletBinding()]
param(
    [int[]]$Ports = @(4101, 4102, 4103),
    [switch]$WithHttp,
    [int]$HttpPort = 8080
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:PYTHONUTF8 = "1"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "No virtual environment found. Run scripts\setup.ps1 first."
}

foreach ($port in $Ports) {
    Write-Host "Starting miner simulator on port $port" -ForegroundColor Cyan
    Start-Process -FilePath $venvPython `
        -ArgumentList @("-m", "sim.miner_sim", "--port", $port) `
        -WorkingDirectory $root
}

if ($WithHttp) {
    # Link the web UI to the last TCP simulator, so setting miner-mode there
    # actually stops that miner's hashrate — as it would on a real S19, where
    # the web UI and the cgminer API are two faces of one machine. Without the
    # link a bitmain_http demo looks like a failure: MinerWatch sets the mode,
    # keeps seeing full hashrate, and concludes the sleep never took effect.
    $linked = $Ports[-1]
    Write-Host "Starting Bitmain web-UI simulator on port $HttpPort (driving miner on $linked)" -ForegroundColor Cyan
    Start-Process -FilePath $venvPython `
        -ArgumentList @("-m", "sim.bitmain_http_sim", "--port", $HttpPort,
                        "--linked-port", $linked, "--verbose") `
        -WorkingDirectory $root
}

Write-Host ""
Write-Host "Simulators running. Try:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python.exe -m minerwatch -c miners.yaml status"
Write-Host "  .\.venv\Scripts\python.exe -m minerwatch -c miners.yaml sleep miner-01 --live"
